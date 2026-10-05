import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, NotFoundError
from src.repository import Repository
from src.service import Service


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "复评楼栋", "description": "震后复评", "severity": "low",
            "quantity": 1, "threshold": 10, "external_ref": "RE-1",
        }, "creator", "assessor")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _batch(self, batch_no, expected_version, measurements=None, scheme=None,
               density=None, actor="survey", role="assessor"):
        payload = {"batch_no": batch_no, "item_id": self.item["id"],
                   "expected_version": expected_version}
        if measurements is not None:
            payload["measurements"] = measurements
        if scheme is not None:
            payload["scheme_content"] = scheme
        if density is not None:
            payload["occupant_density"] = density
        return self.service.submit_batch(payload, actor, role)

    def test_batch_links_work_order_measurement_and_scheme(self):
        result = self._batch(
            "B-001", 1,
            measurements=[{"component_code": "Z1", "material": "C30",
                           "measured_value": 15}],
            scheme="增设抗震墙", density=1.0)
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["item_version"], 2)
        self.assertEqual(result["measurements"][0]["status"], "effective")
        self.assertEqual(result["scheme"]["status"], "effective")
        # 低严重度+人员密度1.0(+3)+强度比0.5(罚2)：优先级从重算前的1抬到6
        self.assertEqual(result["priority"], 6)
        conclusions = self.service.list_conclusions(self.item["id"], "viewer")
        kinds = {(c["kind"], c["status"]) for c in conclusions}
        self.assertIn(("priority", "active"), kinds)
        self.assertIn(("review", "pending"), kinds)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["current_batch_no"], "B-001")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_concurrent_same_component_keeps_one_effective(self):
        self._batch("B-001", 1,
                    measurements=[{"component_code": "Z1", "material": "C30",
                                   "measured_value": 30}])
        self._batch("B-002", 2,
                    measurements=[{"component_code": "Z1", "material": "C30",
                                   "measured_value": 12}])
        versions = self.service.list_components(self.item["id"], "viewer")
        self.assertEqual(len(versions), 2)
        self.assertEqual(sum(v["status"] == "effective" for v in versions), 1)
        pending = [v for v in versions if v["status"] == "pending_review"]
        self.assertEqual(len(pending), 1)
        # 后到的待复核版本经人工提为生效：原生效版作废，结论按新测量失效重算
        promoted = self.service.promote_component_version(
            pending[0]["id"], "engineer", "structural_engineer")
        self.assertEqual(promoted["version"]["status"], "effective")
        statuses = {v["id"]: v["status"]
                    for v in self.service.list_components(self.item["id"], "viewer")}
        self.assertEqual([s for s in statuses.values()].count("effective"), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_concurrent_submissions_in_threads(self):
        barrier = threading.Barrier(2)

        def submit(batch_no, measured):
            barrier.wait()
            try:
                self._batch(batch_no, {"B-T1": 1, "B-T2": 2}[batch_no],
                            measurements=[{"component_code": "Z9", "material": "C30",
                                           "measured_value": measured}])
            except ConflictError:
                pass

        t1 = threading.Thread(target=submit, args=("B-T1", 30))
        t2 = threading.Thread(target=submit, args=("B-T2", 12))
        t1.start(); t2.start(); t1.join(); t2.join()
        versions = self.service.list_components(self.item["id"], "viewer",
                                                status="effective")
        self.assertEqual(len(versions), 1)

    def test_same_batch_no_stored_once(self):
        payload = {"batch_no": "B-IDEM", "item_id": self.item["id"],
                   "expected_version": 1,
                   "measurements": [{"component_code": "Z2", "material": "C30",
                                     "measured_value": 30}]}
        first = self.service.submit_batch(dict(payload), "survey", "assessor")
        second = self.service.submit_batch(dict(payload), "survey", "assessor")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        # 批次表只有一行，工单版本只推进一次
        stored = self.repo.get_batch("B-IDEM")["result"]
        self.assertEqual(stored["item_version"], 2)
        self.assertEqual(self.service.get_item(self.item["id"], "viewer")["version"], 2)
        # 同批次号配不同内容：拒绝，防止批次号被复用串单
        payload["measurements"][0]["measured_value"] = 1
        with self.assertRaises(ConflictError):
            self.service.submit_batch(payload, "survey", "assessor")

    def test_failed_batch_rolls_back_and_same_no_retries(self):
        good = {"batch_no": "B-FAIL", "item_id": self.item["id"],
                "expected_version": 1,
                "measurements": [{"component_code": "Z3", "material": "C30",
                                  "measured_value": 30}],
                "scheme_content": "加固一版"}
        # 工单版本过期：整批必须失败，测量/方案/结论一律不得半批落库
        stale = dict(good, expected_version=99)
        with self.assertRaises(ConflictError):
            self.service.submit_batch(stale, "survey", "assessor")
        self.assertIsNone(self.repo.get_batch("B-FAIL"))
        self.assertEqual(
            self.service.list_components(self.item["id"], "viewer"), [])
        self.assertEqual(self.service.list_schemes(self.item["id"], "viewer"), [])
        self.assertEqual(len(self.service.list_conclusions(self.item["id"], "viewer")), 0)
        self.assertGreaterEqual(
            len(self.repo.list_batch_attempts("B-FAIL")), 1)
        # 原工单未被改动，同一批次号用新版本号即可重试成功
        self.assertEqual(self.service.get_item(self.item["id"], "viewer")["version"], 1)
        retried = self.service.submit_batch(good, "survey", "assessor")
        self.assertEqual(retried["item_version"], 2)
        self.assertFalse(retried["duplicate"])

    def test_failure_after_measurements_rolls_back_everything(self):
        original = self.repo._append_audit_locked

        def boom(action, *args, **kwargs):
            if action == "review_reset":
                raise RuntimeError("simulated write failure")
            return original(action, *args, **kwargs)

        self.repo._append_audit_locked = boom
        with self.assertRaises(RuntimeError):
            self._batch("B-BOOM", 1,
                        measurements=[{"component_code": "Z4", "material": "C30",
                                       "measured_value": 30}],
                        scheme="加固一版")
        self.repo._append_audit_locked = original
        self.assertIsNone(self.repo.get_batch("B-BOOM"))
        self.assertEqual(self.service.list_schemes(self.item["id"], "viewer"), [])
        self.assertEqual(self.service.list_components(self.item["id"], "viewer"), [])
        audit_before = len(self.service.audit("viewer"))
        # 同批次号原样重试，一次入库
        retried = self._batch("B-BOOM", 1,
                              measurements=[{"component_code": "Z4", "material": "C30",
                                             "measured_value": 30}],
                              scheme="加固一版")
        self.assertFalse(retried["duplicate"])
        self.assertEqual(len(self.service.audit("viewer")), audit_before + 3)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_measurement_and_density_changes_invalidate_conclusions(self):
        self._batch("B-010", 1,
                    measurements=[{"component_code": "Z1", "material": "C30",
                                   "measured_value": 30}], scheme="加固一版")
        self.service.decide_review(self.item["id"],
                                   {"decision": "active", "note": "通过"},
                                   "board", "review_board")
        active_review = [c for c in self.service.list_conclusions(self.item["id"], "viewer")
                         if c["kind"] == "review" and c["status"] == "active"]
        self.assertEqual(len(active_review), 1)
        old_priority = [c for c in self.service.list_conclusions(self.item["id"], "viewer")
                        if c["kind"] == "priority" and c["status"] == "active"][0]["priority_score"]

        # 新构件的有效测量改变输入指纹：旧优先级/审核失效，审核回到待复核
        self._batch("B-011", 2,
                    measurements=[{"component_code": "Z2", "material": "C30",
                                   "measured_value": 9}])
        conclusions = self.service.list_conclusions(self.item["id"], "viewer")
        invalidated = [c for c in conclusions if c["status"] == "invalidated"]
        self.assertGreaterEqual(len(invalidated), 2)
        self.assertFalse(any(c["kind"] == "review" and c["status"] == "active"
                             for c in conclusions))
        self.assertTrue(any(c["kind"] == "review" and c["status"] == "pending"
                            for c in conclusions))
        new_priority = [c for c in conclusions
                        if c["kind"] == "priority" and c["status"] == "active"][0]
        self.assertNotEqual(new_priority["priority_score"], old_priority)

        # 人员密度变化同样触发重算
        self._batch("B-012", 3, density=1.0)
        newest = [c for c in self.service.list_conclusions(self.item["id"], "viewer")
                  if c["kind"] == "priority" and c["status"] == "active"][0]
        self.assertGreaterEqual(newest["priority_score"], new_priority["priority_score"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_legacy_item_missing_fields_reads_and_backfills(self):
        # 模拟旧工单：新字段为 NULL
        with self.repo.conn:
            self.repo.conn.execute(
                "UPDATE items SET occupant_density=NULL, current_batch_no=NULL WHERE id=?",
                (self.item["id"],))
        # 缺字段照常读回
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertIsNone(item["occupant_density"])
        self.assertIn("priority", item)
        # 补全后照常读回，且按补全输入重算结论
        result = self.service.backfill_item(
            self.item["id"], {"occupant_density": 1.0}, "surveyor", "assessor")
        self.assertEqual(result["item"]["occupant_density"], 1.0)
        self.assertTrue(any(c["kind"] == "priority" and c["status"] == "active"
                            for c in result["conclusions"]))
        again = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(again["occupant_density"], 1.0)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_scheme_revision_invalidates_review_only(self):
        self._batch("B-020", 1,
                    measurements=[{"component_code": "Z1", "material": "C30",
                                   "measured_value": 30}], scheme="加固一版")
        priority_before = [c for c in self.service.list_conclusions(self.item["id"], "viewer")
                           if c["kind"] == "priority" and c["status"] == "active"][0]["id"]
        self._batch("B-021", 2, scheme="加固二版")
        conclusions = self.service.list_conclusions(self.item["id"], "viewer")
        # 输入未变，优先级结论沿用；方案换版，审核结论失效并重开
        self.assertTrue(any(c["id"] == priority_before and c["status"] == "active"
                            for c in conclusions))
        self.assertTrue(any(c["kind"] == "review" and c["status"] == "invalidated"
                            for c in conclusions))
        self.assertTrue(any(c["kind"] == "review" and c["status"] == "pending"
                            for c in conclusions))
        schemes = self.service.list_schemes(self.item["id"], "viewer")
        self.assertEqual([s["status"] for s in schemes], ["superseded", "effective"])


if __name__ == "__main__":
    unittest.main()
