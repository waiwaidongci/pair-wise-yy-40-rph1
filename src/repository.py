from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (ID_PREFIX, STATES, escalation_required, priority_score,
                     response_deadline_hours)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        cols = [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)
            # 旧库迁移：工单缺字段（如人员密度）补全后照常读回
            self._ensure_column("items", "density", "REAL")
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','committed','failed')),
                    density REAL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    component TEXT NOT NULL,
                    material_version TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_review'
                        CHECK(status IN ('effective','pending_review')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                -- 同一构件同一材料版本只保留一个生效版本；并发提交时后到的转待复核
                CREATE UNIQUE INDEX IF NOT EXISTS ux_measurements_effective
                    ON measurements(item_id, component, material_version)
                    WHERE status='effective';
                CREATE INDEX IF NOT EXISTS ix_measurements_batch ON measurements(batch_id);
                CREATE TABLE IF NOT EXISTS reinforcement_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    component TEXT NOT NULL,
                    material_version TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_review'
                        CHECK(status IN ('effective','pending_review')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_plans_effective
                    ON reinforcement_plans(item_id, component, material_version)
                    WHERE status='effective';
                CREATE INDEX IF NOT EXISTS ix_plans_batch ON reinforcement_plans(batch_id);
                CREATE TABLE IF NOT EXISTS conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    batch_id INTEGER REFERENCES batches(id) ON DELETE SET NULL,
                    priority INTEGER NOT NULL,
                    deadline_hours INTEGER NOT NULL,
                    escalation_required INTEGER NOT NULL,
                    density REAL,
                    valid INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0,1)),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_conclusions_item ON conclusions(item_id, valid);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 批次：工单、测量、加固方案串成同一批次，批次号幂等 ----

    def get_or_create_batch(self, batch_no: str, item_id: int, density: float,
                            actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
            if row is not None:
                return dict(row)
            try:
                with self.conn:
                    cur = self.conn.execute(
                        """INSERT INTO batches(batch_no, item_id, status, density,
                           created_by, created_at, updated_at)
                           VALUES(?,?, 'pending', ?, ?, ?, ?)""",
                        (batch_no, item_id, density, actor, now, now),
                    )
                    batch_id = int(cur.lastrowid)
            except sqlite3.IntegrityError:
                row = self.conn.execute(
                    "SELECT * FROM batches WHERE batch_no=?", (batch_no,)
                ).fetchone()
                return dict(row)
            return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return dict(row)

    def get_batch_by_no(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_batches(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batches WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_batch_status(self, batch_id: int, status: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE batches SET status=?, updated_at=? WHERE id=?",
                (status, now, batch_id),
            )

    def write_batch_data(self, batch_id: int, item_id: int,
                         measurements: List[Dict[str, Any]],
                         plans: List[Dict[str, Any]], density: float,
                         actor: str) -> Dict[str, Any]:
        """原子写入批次下的测量与加固方案，重算优先级/审核并失效旧结论，最后置批次为committed。

        同一构件同一材料版本只保留一个生效版本：首个提交为effective，
        并发下后到的因部分唯一索引冲突转pending_review（待复核）。
        """
        now = utc_now()
        written_measurements: List[Dict[str, Any]] = []
        written_plans: List[Dict[str, Any]] = []
        with self._lock, self.conn:
            for m in measurements:
                mid = self._insert_versioned(
                    "measurements",
                    "(batch_id, item_id, component, material_version, quantity, status,"
                    " created_by, created_at) VALUES(?,?,?,?,?, 'effective', ?, ?)",
                    "(batch_id, item_id, component, material_version, quantity, status,"
                    " created_by, created_at) VALUES(?,?,?,?,?, 'pending_review', ?, ?)",
                    (batch_id, item_id, m["component"], m["material_version"],
                     m["quantity"], actor, now),
                    item_id, m["component"], m["material_version"],
                )
                written_measurements.append(dict(self.conn.execute(
                    "SELECT * FROM measurements WHERE id=?", (mid,)).fetchone()))
            for p in plans:
                pid = self._insert_versioned(
                    "reinforcement_plans",
                    "(batch_id, item_id, component, material_version, plan, status,"
                    " created_by, created_at) VALUES(?,?,?,?,?, 'effective', ?, ?)",
                    "(batch_id, item_id, component, material_version, plan, status,"
                    " created_by, created_at) VALUES(?,?,?,?,?, 'pending_review', ?, ?)",
                    (batch_id, item_id, p["component"], p["material_version"],
                     p["plan"], actor, now),
                    item_id, p["component"], p["material_version"],
                )
                written_plans.append(dict(self.conn.execute(
                    "SELECT * FROM reinforcement_plans WHERE id=?", (pid,)).fetchone()))
            # 测量、人员密度变化后：失效旧结论，并用生效测量的控制量重算优先级与审核
            governing = self.conn.execute(
                """SELECT MAX(quantity) AS q FROM measurements
                   WHERE item_id=? AND status='effective'""", (item_id,)
            ).fetchone()
            quantity = governing["q"] if governing["q"] is not None else None
            if quantity is not None:
                self.conn.execute(
                    "UPDATE items SET quantity=?, density=?, updated_at=? WHERE id=?",
                    (quantity, density, now, item_id),
                )
            else:
                self.conn.execute(
                    "UPDATE items SET density=?, updated_at=? WHERE id=?",
                    (density, now, item_id),
                )
            self.conn.execute(
                "UPDATE conclusions SET valid=0 WHERE item_id=? AND valid=1", (item_id,)
            )
            item = dict(self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
            open_records = int(self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,)).fetchone()["n"])
            priority = priority_score(
                item["severity"], item["quantity"], item["threshold"],
                open_records, density)
            deadline = response_deadline_hours(
                item["severity"], item["quantity"], item["threshold"], density)
            escalation = escalation_required(
                item["severity"], item["quantity"], item["threshold"], density)
            cur = self.conn.execute(
                """INSERT INTO conclusions(item_id, batch_id, priority, deadline_hours,
                   escalation_required, density, valid, created_at)
                   VALUES(?,?,?,?,?,?,1,?)""",
                (item_id, batch_id, priority, deadline,
                 1 if escalation else 0, density, now),
            )
            conclusion_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE batches SET status='committed', updated_at=? WHERE id=?",
                (now, batch_id),
            )
        return {
            "measurements": written_measurements,
            "plans": written_plans,
            "conclusion": dict(self.conn.execute(
                "SELECT * FROM conclusions WHERE id=?", (conclusion_id,)).fetchone()),
            "item": dict(self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()),
        }

    def _insert_versioned(self, table: str, effective_sql: str, pending_sql: str,
                          params: tuple, item_id: int, component: str,
                          material_version: str) -> int:
        try:
            cur = self.conn.execute(
                f"INSERT INTO {table} {effective_sql}", params)
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            existing = self.conn.execute(
                f"SELECT 1 FROM {table} WHERE item_id=? AND component=? "
                "AND material_version=? AND status='effective'",
                (item_id, component, material_version),
            ).fetchone()
            if existing is None:
                raise
            cur = self.conn.execute(
                f"INSERT INTO {table} {pending_sql}", params)
            return int(cur.lastrowid)

    def list_measurements(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM measurements WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_effective_measurements(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM measurements WHERE item_id=? AND status='effective' "
                "ORDER BY id", (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_reinforcement_plans(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reinforcement_plans WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_effective_plans(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reinforcement_plans WHERE item_id=? AND status='effective' "
                "ORDER BY id", (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_conclusion(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM conclusions WHERE item_id=? AND valid=1 "
                "ORDER BY id DESC LIMIT 1", (item_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_conclusion_for_batch(self, batch_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM conclusions WHERE batch_id=? ORDER BY id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_item_fields(self, item_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now()
        allowed = {"title", "description", "severity", "quantity", "threshold", "density"}
        sets: List[str] = []
        params: List[Any] = []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                params.append(value)
        if sets:
            sets.append("updated_at=?")
            params.append(now)
            params.append(item_id)
            with self._lock, self.conn:
                self.conn.execute(
                    f"UPDATE items SET {', '.join(sets)} WHERE id=?", params)
        return self.get_item(item_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
