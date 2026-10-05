from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import canonical_hash
from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_id, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BACKFILL_ROLES, BATCH_ROLES, BATCH_ENTITY,
                    CREATE_ROLES, PROMOTE_ROLES, RECORD_ROLES,
                    REVIEW_DECIDE_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, expected_strength, measurement_ratio,
                    occupant_factor, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


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
        density = None
        if payload.get("occupant_density") is not None:
            density = require_number(payload.get("occupant_density"), "occupant_density")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, density)
        self.repository.append_audit("create", "抗震鉴定", item["id"], actor, {
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
        self.repository.append_audit("record", "抗震鉴定", item_id, actor, {
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
        self.repository.append_audit("transition", "抗震鉴定", item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    # ---------------- 震后复评批次 ----------------

    def _normalize_measurements(self, raw: Any) -> list:
        if not isinstance(raw, list):
            from .domain import ValidationError
            raise ValidationError("measurements必须是数组")
        measurements = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                from .domain import ValidationError
                raise ValidationError(f"measurements[{index}]必须是对象")
            code = require_text(entry.get("component_code"), f"measurements[{index}].component_code", 100)
            material = require_text(entry.get("material"), f"measurements[{index}].material", 100)
            normalized = {"component_code": code, "material": material,
                          "component_name": entry.get("component_name", "") or "",
                          "source": require_text(entry.get("source", "offline_survey"),
                                                 f"measurements[{index}].source", 50)}
            if entry.get("measured_value") is not None:
                normalized["measured_value"] = require_number(
                    entry["measured_value"], f"measurements[{index}].measured_value")
            else:
                normalized["measured_value"] = None
            if entry.get("expected_value") is not None:
                normalized["expected_value"] = require_number(
                    entry["expected_value"], f"measurements[{index}].expected_value", 0.000001)
            measurements.append(normalized)
        return measurements

    def submit_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        """同一批次号串起工单修订、离线测量和加固方案，整批原子提交。"""
        ensure_role(role, BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        item_id = require_id(payload.get("item_id"), "item_id")
        normalized: Dict[str, Any] = {"item_id": item_id}

        expected_version = payload.get("expected_version")
        if expected_version is not None:
            normalized["expected_version"] = require_id(expected_version, "expected_version")

        if payload.get("occupant_density") is not None:
            normalized["occupant_density"] = require_number(
                payload["occupant_density"], "occupant_density")

        normalized["measurements"] = self._normalize_measurements(payload.get("measurements", []))

        scheme_content = payload.get("scheme_content")
        if scheme_content is not None:
            normalized["scheme_content"] = require_text(scheme_content, "scheme_content")

        if (normalized.get("expected_version") is None
                and normalized.get("occupant_density") is None
                and not normalized["measurements"]
                and "scheme_content" not in normalized):
            from .domain import ValidationError
            raise ValidationError(
                "批次至少包含工单修订、测量数据或加固方案中的一项")

        # 幂等键：同批次号 + 同规范内容。重传不重复入库；批次号与内容冲突拒绝。
        content_hash = canonical_hash({
            "item_id": normalized["item_id"],
            "expected_version": normalized.get("expected_version"),
            "occupant_density": normalized.get("occupant_density"),
            "measurements": normalized["measurements"],
            "scheme_content": normalized.get("scheme_content"),
        })
        try:
            return self.repository.receive_batch(batch_no, content_hash, normalized, actor)
        except Exception as exc:
            # 写入失败：整批已回滚、原工单保留；记录可重试失败，调用方可用同批次号重试
            if getattr(exc, "retryable", True):
                self.repository.record_batch_failure(batch_no, exc)
            raise

    def get_batch(self, batch_no: str, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.repository.get_batch(batch_no)
        if batch is None:
            from .domain import NotFoundError
            raise NotFoundError("批次不存在")
        return batch

    def list_components(self, item_id: int, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_components(item_id, status)

    def promote_component_version(self, version_id: int, actor: str, role: str) -> dict:
        ensure_role(role, PROMOTE_ROLES)
        actor = require_text(actor, "actor", 100)
        return self.repository.promote_component_version(version_id, actor)

    def list_schemes(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_schemes(item_id)

    def list_conclusions(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_conclusions(item_id)

    def decide_review(self, item_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> dict:
        ensure_role(role, REVIEW_DECIDE_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = payload.get("decision")
        if decision not in ("active", "rejected"):
            from .domain import ValidationError
            raise ValidationError("decision必须是active或rejected")
        note = require_text(payload.get("note", ""), "note") if payload.get("note") else ""
        return self.repository.decide_review(item_id, decision, actor, note)

    def backfill_item(self, item_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> dict:
        """旧工单缺字段补全，补全后照常读回并按新输入重算优先级与审核。"""
        ensure_role(role, BACKFILL_ROLES)
        actor = require_text(actor, "actor", 100)
        density = None
        if payload.get("occupant_density") is not None:
            density = require_number(payload.get("occupant_density"), "occupant_density")
        result = self.repository.backfill_item(item_id, density, actor)
        result["item"] = self.enrich(result["item"])
        return result

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

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        density = item.get("occupant_density") or 0.0
        # 读回旧工单时新字段为空，按默认值兜底，保证排序与期限不缺项
        result["occupant_density"] = item.get("occupant_density")
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"], 0, density)
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
