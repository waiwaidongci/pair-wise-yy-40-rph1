from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import canonical_hash, make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (ID_PREFIX, STATES, VERSION_STATUSES, SCHEME_STATUSES,
                    expected_strength, measurement_ratio)


def _hash_content(payload: dict) -> str:
    return canonical_hash(payload)


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

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        vstatus = ",".join("'" + s + "'" for s in VERSION_STATUSES)
        sstatus = ",".join("'" + s + "'" for s in SCHEME_STATUSES)
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
                CREATE TABLE IF NOT EXISTS components (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, code)
                );
                CREATE TABLE IF NOT EXISTS component_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
                    batch_no TEXT NOT NULL,
                    material TEXT NOT NULL,
                    measured_value REAL,
                    expected_value REAL,
                    source TEXT NOT NULL DEFAULT 'offline_survey',
                    status TEXT NOT NULL CHECK(status IN ({vstatus})),
                    content_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_component_effective
                    ON component_versions(component_id) WHERE status='effective';
                CREATE INDEX IF NOT EXISTS ix_component_versions_batch
                    ON component_versions(batch_no);
                CREATE TABLE IF NOT EXISTS schemes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    batch_no TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 1,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({sstatus})),
                    content_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT,
                    invalidated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_scheme_effective
                    ON schemes(item_id) WHERE status='effective';
                CREATE TABLE IF NOT EXISTS conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('priority','review')),
                    status TEXT NOT NULL CHECK(status IN ('active','pending','invalidated','rejected')),
                    priority_score INTEGER,
                    basis_hash TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    batch_no TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_by TEXT,
                    decided_at TEXT,
                    invalidated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_priority_active
                    ON conclusions(item_id) WHERE kind='priority' AND status='active';
                CREATE INDEX IF NOT EXISTS ix_conclusions_item ON conclusions(item_id);
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
        # 旧库迁移：旧工单缺新字段，补列后读回时按默认值兜底
        self._migrate_columns("items", {
            "occupant_density": "REAL",
            "current_batch_no": "TEXT",
        })

    def _migrate_columns(self, table: str, columns: Dict[str, str]) -> None:
        with self._lock, self.conn:
            existing = {row["name"] for row in self.conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            for name, decl in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result.setdefault("occupant_density", None)
        result.setdefault("current_batch_no", None)
        return result

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, occupant_density: Optional[float] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       occupant_density, current_batch_no)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, occupant_density, None),
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

    # ---------------- 震后复评批次 ----------------

    def get_batch(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result["result"])
        return result

    def record_batch_failure(self, batch_no: str, error: str) -> None:
        # 失败尝试独立成事务：主批次事务已回滚，失败痕迹不能再把它写活
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    "INSERT INTO batch_attempts(batch_no, error, created_at) VALUES(?,?,?)",
                    (batch_no, str(error)[:1000], utc_now()),
                )
        except sqlite3.Error:
            pass

    def list_batch_attempts(self, batch_no: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batch_attempts WHERE batch_no=? ORDER BY id", (batch_no,)
            ).fetchall()
        return [dict(row) for row in rows]

    def receive_batch(self, batch_no: str, content_hash: str, payload: Dict[str, Any],
                      actor: str) -> Dict[str, Any]:
        """工单修订、测量版本与加固方案在同一事务、同一批次号下提交。

        - 同批次号重传：内容一致只入库一次（幂等返回）；不一致拒绝。
        - 同构件同材料并发提交：先写入的占生效位，后到的转 pending_review。
        - 测量值/人员密度变化：旧优先级与审核结论失效，优先级重算、审核回到待复核。
        - 事务内任何一步失败：整批回滚，批次号不被占用，可原样重试。
        """
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT id, content_hash, result FROM batches WHERE batch_no=?",
                (batch_no,)).fetchone()
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    # 同号不同内容属于请求错误，不应作为可重试失败记录
                    raise ConflictError("批次号已用于不同内容，请使用新批次号",
                                        retryable=False)
                stored = json.loads(existing["result"])
                stored["duplicate"] = True
                return stored

            item_id = payload["item_id"]
            item = self.get_item(item_id)
            now = utc_now()
            expected_version = payload.get("expected_version")
            if expected_version is not None:
                cur = self.conn.execute(
                    """UPDATE items SET version=version+1, updated_at=?, current_batch_no=?
                       WHERE id=? AND version=?""",
                    (now, batch_no, item_id, expected_version),
                )
                if cur.rowcount == 0:
                    # 乐观锁失败必须让整批回滚，不允许测量数据半批落库
                    if self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
                        raise NotFoundError("项目不存在")
                    raise ConflictError("工单版本冲突，请刷新后整批重试")
            else:
                self.conn.execute(
                    "UPDATE items SET updated_at=?, current_batch_no=? WHERE id=?",
                    (now, batch_no, item_id))

            density = payload.get("occupant_density")
            if density is not None:
                self.conn.execute(
                    "UPDATE items SET occupant_density=? WHERE id=?", (density, item_id))
            item = self.get_item(item_id)

            measurement_versions = []
            for measurement in payload.get("measurements", []):
                component_id = self._upsert_component_locked(item_id, measurement["component_code"],
                                                            measurement.get("component_name", ""), now)
                expected_value = expected_strength(measurement["material"], measurement.get("expected_value"))
                version_hash = _hash_content({
                    "component_code": measurement["component_code"],
                    "material": measurement["material"],
                    "measured_value": measurement.get("measured_value"),
                    "expected_value": expected_value,
                })
                effective_row = self.conn.execute(
                    "SELECT id FROM component_versions WHERE component_id=? AND status='effective'",
                    (component_id,)).fetchone()
                if effective_row is None:
                    status = "effective"
                else:
                    # 同一批次内/并发的后到同构件版本，不覆盖生效版本，转待复核
                    status = "pending_review"
                cur = self.conn.execute(
                    """INSERT INTO component_versions(component_id, batch_no, material,
                       measured_value, expected_value, source, status, content_hash,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (component_id, batch_no, measurement["material"],
                     measurement.get("measured_value"), expected_value,
                     measurement.get("source", "offline_survey"), status, version_hash,
                     actor, now),
                )
                version_id = int(cur.lastrowid)
                measurement_versions.append({"id": version_id, "component_id": component_id,
                                             "component_code": measurement["component_code"],
                                             "status": status})
                self._append_audit_locked("measurement_received", "测量版本", version_id, actor, {
                    "batch_no": batch_no, "item_id": item_id,
                    "component_code": measurement["component_code"],
                    "material": measurement["material"], "status": status,
                })

            scheme = None
            if payload.get("scheme_content"):
                seq_row = self.conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS seq FROM schemes WHERE item_id=?",
                    (item_id,)).fetchone()
                scheme_hash = _hash_content({"content": payload["scheme_content"]})
                self.conn.execute(
                    """UPDATE schemes SET status='superseded', superseded_at=?
                       WHERE item_id=? AND status='effective'""",
                    (now, item_id))
                cur = self.conn.execute(
                    """INSERT INTO schemes(item_id, batch_no, sequence, content, status,
                       content_hash, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, batch_no, int(seq_row["seq"]), payload["scheme_content"],
                     "effective", scheme_hash, actor, now),
                )
                scheme = {"id": int(cur.lastrowid), "sequence": int(seq_row["seq"]),
                          "status": "effective", "content_hash": scheme_hash}

            conclusions = self._reconcile_conclusions_locked(
                item_id, batch_no, actor, now,
                force_priority=bool(payload.get("measurements") or density is not None),
                scheme_changed=scheme is not None)

            priority = conclusions["priority_score"]
            result = {
                "batch_no": batch_no, "item_id": item_id,
                "item_version": self.get_item(item_id)["version"],
                "work_order": {"updated": expected_version is not None or density is not None,
                               "occupant_density": self.get_item(item_id)["occupant_density"]},
                "measurements": measurement_versions,
                "scheme": scheme,
                "conclusions": conclusions["conclusions"],
                "priority": priority,
                "duplicate": False,
            }
            self.conn.execute(
                """INSERT INTO batches(batch_no, item_id, content_hash, result, created_by, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (batch_no, item_id, content_hash,
                 json.dumps(result, ensure_ascii=False, sort_keys=True), actor, now))
            self._append_audit_locked("batch_received", "复评批次", item_id, actor, {
                "batch_no": batch_no, "priority": priority,
                "measurements": len(measurement_versions),
                "scheme_changed": scheme is not None,
                "work_order_updated": result["work_order"]["updated"],
            })
            return result

    def _upsert_component_locked(self, item_id: int, code: str, name: str, now: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM components WHERE item_id=? AND code=?", (item_id, code)).fetchone()
        if row is not None:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO components(item_id, code, name, created_at) VALUES(?,?,?,?)",
            (item_id, code, name, now))
        return int(cur.lastrowid)

    def _effective_measurements_locked(self, item_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT cv.material, cv.measured_value, cv.expected_value
               FROM component_versions cv
               JOIN components c ON c.id=cv.component_id
               WHERE c.item_id=? AND cv.status='effective'
               ORDER BY cv.id""",
            (item_id,)).fetchall()
        result = []
        for row in rows:
            result.append({"material": row["material"], "measured_value": row["measured_value"],
                           "expected_value": row["expected_value"]})
        return result

    def _effective_scheme_hash_locked(self, item_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT content_hash FROM schemes WHERE item_id=? AND status='effective'",
            (item_id,)).fetchone()
        return row["content_hash"] if row else None

    def _priority_basis_locked(self, item: Dict[str, Any]) -> Dict[str, Any]:
        measurements = self._effective_measurements_locked(item["id"])
        ratio = measurement_ratio(measurements)
        open_records = self.open_record_count(item["id"])
        density = item.get("occupant_density")
        return {
            "severity": item["severity"], "quantity": item["quantity"],
            "threshold": item["threshold"], "open_records": open_records,
            "occupant_density": density if density is not None else 0.0,
            "measurement_ratio": round(ratio, 6),
            "measurement_hash": _hash_content([
                {"material": m["material"],
                 "measured_value": m["measured_value"],
                 "expected_value": m["expected_value"]} for m in measurements]),
        }

    def _reconcile_conclusions_locked(self, item_id: int, batch_no: str, actor: str,
                                      now: str, force_priority: bool,
                                      scheme_changed: bool) -> Dict[str, Any]:
        """测量/密度/方案变化后：旧结论失效、优先级重算、审核重置为待复核。"""
        from .rules import priority_score
        item = self.get_item(item_id)
        basis = self._priority_basis_locked(item)
        basis_hash = _hash_content(basis)
        priority = priority_score(item["severity"], item["quantity"], item["threshold"],
                                  basis["open_records"], basis["occupant_density"],
                                  basis["measurement_ratio"])

        active_priority = self.conn.execute(
            "SELECT * FROM conclusions WHERE item_id=? AND kind='priority' AND status='active'",
            (item_id,)).fetchone()
        priority_changed = active_priority is None or active_priority["basis_hash"] != basis_hash
        if force_priority and active_priority is not None and priority_changed:
            self.conn.execute(
                """UPDATE conclusions SET status='invalidated', invalidated_at=?
                   WHERE item_id=? AND kind='priority' AND status='active'""",
                (now, item_id))
            active_priority = None
            self._append_audit_locked("conclusion_invalidated", "鉴定结论", item_id, actor, {
                "batch_no": batch_no, "kind": "priority", "reason": "测量或人员密度变化",
            })
        if active_priority is None:
            self.conn.execute(
                """INSERT INTO conclusions(item_id, kind, status, priority_score, basis_hash,
                   detail, batch_no, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (item_id, "priority", "active", priority, basis_hash,
                 json.dumps({"basis": basis, "score": priority}, ensure_ascii=False, sort_keys=True),
                 batch_no, actor, now))

        scheme_hash = self._effective_scheme_hash_locked(item_id)
        review_rows = self.conn.execute(
            """SELECT * FROM conclusions WHERE item_id=? AND kind='review'
               AND status IN ('active','pending') ORDER BY id""", (item_id,)).fetchall()
        if scheme_changed:
            self.conn.execute(
                """UPDATE conclusions SET status='invalidated', invalidated_at=?
                   WHERE item_id=? AND kind='review' AND status IN ('active','pending')""",
                (now, item_id))
            if review_rows:
                self._append_audit_locked("conclusion_invalidated", "鉴定结论", item_id, actor, {
                    "batch_no": batch_no, "kind": "review", "reason": "加固方案换版",
                })
            review_rows = []
        elif force_priority and priority_changed:
            self.conn.execute(
                """UPDATE conclusions SET status='invalidated', invalidated_at=?
                   WHERE item_id=? AND kind='review' AND status IN ('active','pending')""",
                (now, item_id))
            if review_rows:
                self._append_audit_locked("conclusion_invalidated", "鉴定结论", item_id, actor, {
                    "batch_no": batch_no, "kind": "review", "reason": "优先级重算",
                })
            review_rows = []

        if not review_rows and scheme_hash is not None:
            review_detail = json.dumps(
                {"scheme_hash": scheme_hash, "priority": priority},
                ensure_ascii=False, sort_keys=True)
            cur = self.conn.execute(
                """INSERT INTO conclusions(item_id, kind, status, priority_score, basis_hash,
                   detail, batch_no, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (item_id, "review", "pending", priority, basis_hash, review_detail,
                 batch_no, actor, now))
            self._append_audit_locked("review_reset", "鉴定结论", int(cur.lastrowid), actor, {
                "batch_no": batch_no, "item_id": item_id, "status": "pending",
            })

        rows = self.conn.execute(
            "SELECT * FROM conclusions WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        conclusions = []
        for row in rows:
            entry = dict(row)
            entry["detail"] = json.loads(entry["detail"])
            conclusions.append(entry)
        return {"conclusions": conclusions, "priority_score": priority}

    def list_components(self, item_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        sql = """SELECT cv.id, c.code AS component_code, c.name AS component_name,
                        cv.batch_no, cv.material, cv.measured_value, cv.expected_value,
                        cv.source, cv.status, cv.created_by, cv.created_at
                 FROM component_versions cv JOIN components c ON c.id=cv.component_id
                 WHERE c.item_id=?"""
        params: list = [item_id]
        if status:
            sql += " AND cv.status=?"
            params.append(status)
        sql += " ORDER BY cv.id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_component_version(self, version_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT cv.*, c.item_id AS item_id, c.code AS component_code
                   FROM component_versions cv JOIN components c ON c.id=cv.component_id
                   WHERE cv.id=?""", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("构件版本不存在")
        return dict(row)

    def promote_component_version(self, version_id: int, actor: str) -> Dict[str, Any]:
        """待复核版本被人工提为生效：原生效版作废，并触发结论失效重算。"""
        now = utc_now()
        with self._lock, self.conn:
            version = self.get_component_version(version_id)
            if version["status"] != "pending_review":
                raise ConflictError("仅待复核版本可提为生效")
            self.conn.execute(
                """UPDATE component_versions SET status='superseded', superseded_at=?
                   WHERE component_id=? AND status='effective'""",
                (now, version["component_id"]))
            self.conn.execute(
                "UPDATE component_versions SET status='effective' WHERE id=?", (version_id,))
            self._append_audit_locked("measurement_promoted", "测量版本", version_id, actor, {
                "item_id": version["item_id"], "component_code": version["component_code"],
            })
            conclusions = self._reconcile_conclusions_locked(
                version["item_id"], f"promote:{version_id}", actor, now,
                force_priority=True, scheme_changed=False)
            return {"version": self.get_component_version(version_id),
                    "conclusions": conclusions["conclusions"],
                    "priority": conclusions["priority_score"]}

    def list_schemes(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM schemes WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_conclusions(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM conclusions WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["detail"] = json.loads(entry["detail"])
            result.append(entry)
        return result

    def active_priority(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM conclusions WHERE item_id=? AND kind='priority' AND status='active'",
                (item_id,)).fetchone()
        if row is None:
            return None
        entry = dict(row)
        entry["detail"] = json.loads(entry["detail"])
        return entry

    def decide_review(self, item_id: int, decision: str, actor: str,
                      note: str) -> Dict[str, Any]:
        now = utc_now()
        if decision not in ("active", "rejected"):
            raise ConflictError("decision必须是active或rejected")
        with self._lock, self.conn:
            row = self.conn.execute(
                """SELECT * FROM conclusions WHERE item_id=? AND kind='review'
                   AND status='pending' ORDER BY id DESC LIMIT 1""",
                (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("没有待复核的审核结论")
            self.conn.execute(
                """UPDATE conclusions SET status=?, decided_by=?, decided_at=?,
                   detail=? WHERE id=?""",
                (decision, actor, now,
                 json.dumps({**json.loads(row["detail"]), "note": note},
                            ensure_ascii=False, sort_keys=True),
                 row["id"]))
            self._append_audit_locked("review_decided", "鉴定结论", int(row["id"]), actor, {
                "item_id": item_id, "decision": decision, "note": note,
            })
            updated = self.conn.execute(
                "SELECT * FROM conclusions WHERE id=?", (row["id"],)).fetchone()
            entry = dict(updated)
            entry["detail"] = json.loads(entry["detail"])
            return entry

    def backfill_item(self, item_id: int, occupant_density: Optional[float],
                      actor: str) -> Dict[str, Any]:
        """旧工单缺字段补全：持久化写入新字段，并按补全后的输入重算结论。"""
        now = utc_now()
        with self._lock, self.conn:
            item = self.get_item(item_id)
            self.conn.execute(
                "UPDATE items SET occupant_density=?, updated_at=? WHERE id=?",
                (occupant_density, now, item_id))
            conclusions = self._reconcile_conclusions_locked(
                item_id, "backfill", actor, now, force_priority=True, scheme_changed=False)
            item = self.get_item(item_id)
            return {"item": item, "conclusions": conclusions["conclusions"],
                    "priority": conclusions["priority_score"]}

    def _append_audit_locked(self, action: str, entity_type: str, entity_id: int,
                             actor: str, detail: dict) -> Dict[str, Any]:
        """在调用方事务内追加审计事件，保证与业务写入同提交同回滚。"""
        row = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        cur = self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]),
        )
        event["id"] = int(cur.lastrowid)
        return event

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            return self._append_audit_locked(action, entity_type, entity_id, actor, detail)

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
