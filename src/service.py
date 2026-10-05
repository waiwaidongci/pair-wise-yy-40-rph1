from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ValidationError, ensure_role, normalize_severity,
                     require_int, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 批次入库：工单、测量、加固方案串成同一批次 ----

    def ingest_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        # 先校验全部输入，再触库；校验失败不产生任何写入
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        item_id = require_int(payload.get("item_id"), "item_id")
        density = require_number(payload.get("density", 0), "density")
        raw_measurements = payload.get("measurements", [])
        raw_plans = payload.get("reinforcement_plans", [])
        if not isinstance(raw_measurements, list):
            raise ValidationError("measurements必须是数组")
        if not isinstance(raw_plans, list):
            raise ValidationError("reinforcement_plans必须是数组")
        measurements = []
        for m in raw_measurements:
            if not isinstance(m, dict):
                raise ValidationError("measurement必须是对象")
            measurements.append({
                "component": require_text(m.get("component"), "component", 100),
                "material_version": require_text(
                    m.get("material_version"), "material_version", 100),
                "quantity": require_number(m.get("quantity"), "quantity"),
            })
        plans = []
        for p in raw_plans:
            if not isinstance(p, dict):
                raise ValidationError("plan必须是对象")
            plans.append({
                "component": require_text(p.get("component"), "component", 100),
                "material_version": require_text(
                    p.get("material_version"), "material_version", 100),
                "plan": require_text(p.get("plan"), "plan"),
            })
        # 工单必须存在
        self.repository.get_item(item_id)
        # 批次号幂等：同批次号重传只入库一次；committed直接回读
        batch = self.repository.get_or_create_batch(batch_no, item_id, density, actor)
        if batch["status"] == "committed":
            return self._batch_view(batch)
        # 写入失败：原批次保留（failed），可用同批次号重试
        try:
            result = self.repository.write_batch_data(
                batch["id"], item_id, measurements, plans, density, actor)
        except Exception:
            self.repository.mark_batch_status(batch["id"], "failed")
            raise
        self.repository.append_audit("ingest", "batch", batch["id"], actor, {
            "batch_no": batch_no, "item_id": item_id,
            "measurements": len(result["measurements"]),
            "plans": len(result["plans"]),
            "priority": result["conclusion"]["priority"],
        })
        return self._batch_view(self.repository.get_batch(batch["id"]))

    def get_batch_view(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._batch_view(self.repository.get_batch(batch_id))

    def list_batches_for_item(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return [self._batch_view(b) for b in self.repository.list_batches(item_id)]

    def get_item_conclusion(self, item_id: int, role: str) -> Optional[Dict[str, Any]]:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.get_latest_conclusion(item_id)

    def update_item(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        fields: Dict[str, Any] = {}
        if "title" in payload:
            fields["title"] = require_text(payload["title"], "title", 200)
        if "description" in payload:
            fields["description"] = require_text(payload["description"], "description")
        if "severity" in payload:
            fields["severity"] = normalize_severity(payload["severity"])
        if "quantity" in payload:
            fields["quantity"] = require_number(payload["quantity"], "quantity")
        if "threshold" in payload:
            fields["threshold"] = require_number(
                payload["threshold"], "threshold", 0.000001)
        if "density" in payload:
            fields["density"] = require_number(payload["density"], "density")
        if not fields:
            return self.get_item(item_id, role)
        updated = self.repository.update_item_fields(item_id, fields)
        self.repository.append_audit("update", ENTITY, item_id, actor, {
            "fields": sorted(fields.keys()),
        })
        return self.enrich(updated)

    def _batch_view(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "batch": batch,
            "measurements": self.repository.list_measurements(batch["id"]),
            "reinforcement_plans": self.repository.list_reinforcement_plans(batch["id"]),
            "conclusion": self.repository.get_conclusion_for_batch(batch["id"]),
        }

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        density = item.get("density")
        if density is None:
            density = 0.0
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"], density=density)
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"], density=density)
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"], density=density)
        return result
