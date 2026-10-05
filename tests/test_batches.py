import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ValidationError
from src.repository import Repository
from src.service import Service


def _item_payload():
    return {
        "title": "震后复评工单",
        "description": "现场队离线测量回院复评",
        "severity": "medium",
        "quantity": 5,
        "threshold": 20,
        "external_ref": "WO-1",
    }


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(_item_payload(), "creator", "assessor")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _batch(self, batch_no, density=2.0, measurements=None, plans=None):
        return self.service.ingest_batch({
            "batch_no": batch_no,
            "item_id": self.item["id"],
            "density": density,
            "measurements": measurements or [],
            "reinforcement_plans": plans or [],
        }, "ingestor", "assessor")

    def test_batch_links_work_order_measurements_and_plan(self):
        result = self._batch(
            "B-001",
            measurements=[
                {"component": "C1", "material_version": "v1", "quantity": 10.0},
                {"component": "C2", "material_version": "v1", "quantity": 12.0},
            ],
            plans=[
                {"component": "C1", "material_version": "v1", "plan": "加大截面"},
            ],
        )
        batch = result["batch"]
        self.assertEqual(batch["item_id"], self.item["id"])
        self.assertEqual(batch["status"], "committed")
        self.assertEqual(len(result["measurements"]), 2)
        self.assertEqual(len(result["reinforcement_plans"]), 1)
        for m in result["measurements"]:
            self.assertEqual(m["batch_id"], batch["id"])
            self.assertEqual(m["item_id"], self.item["id"])
        for p in result["reinforcement_plans"]:
            self.assertEqual(p["batch_id"], batch["id"])
        listed = self.service.list_batches_for_item(self.item["id"], "viewer")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["batch"]["id"], batch["id"])

    def test_same_component_material_version_one_effective_later_pending(self):
        first = self._batch("B-001", measurements=[
            {"component": "C1", "material_version": "v1", "quantity": 10.0},
        ])
        second = self._batch("B-002", measurements=[
            {"component": "C1", "material_version": "v1", "quantity": 12.0},
        ])
        m1 = first["measurements"][0]
        m2 = second["measurements"][0]
        self.assertEqual(m1["status"], "effective")
        self.assertEqual(m2["status"], "pending_review")
        effective = self.repo.list_effective_measurements(self.item["id"])
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["id"], m1["id"])
        # 不同材料版本互不冲突
        third = self._batch("B-003", measurements=[
            {"component": "C1", "material_version": "v2", "quantity": 14.0},
        ])
        self.assertEqual(third["measurements"][0]["status"], "effective")

    def test_plan_effective_and_pending_review(self):
        first = self._batch("B-001", plans=[
            {"component": "C1", "material_version": "v1", "plan": "方案A"},
        ])
        second = self._batch("B-002", plans=[
            {"component": "C1", "material_version": "v1", "plan": "方案B"},
        ])
        self.assertEqual(first["reinforcement_plans"][0]["status"], "effective")
        self.assertEqual(second["reinforcement_plans"][0]["status"], "pending_review")
        effective = self.repo.list_effective_plans(self.item["id"])
        self.assertEqual(len(effective), 1)

    def test_concurrent_submissions_keep_one_effective(self):
        barrier = threading.Barrier(2)
        results = {}

        def ingest(batch_no, quantity):
            barrier.wait()
            results[batch_no] = self._batch(batch_no, measurements=[
                {"component": "C1", "material_version": "v1", "quantity": quantity},
            ])

        t1 = threading.Thread(target=ingest, args=("B-001", 10.0))
        t2 = threading.Thread(target=ingest, args=("B-002", 12.0))
        t1.start(); t2.start()
        t1.join(); t2.join()
        statuses = sorted(r["measurements"][0]["status"] for r in results.values())
        self.assertEqual(statuses, ["effective", "pending_review"])
        effective = self.repo.list_effective_measurements(self.item["id"])
        self.assertEqual(len(effective), 1)

    def test_measurement_change_recomputes_and_invalidates_conclusion(self):
        first = self._batch("B-001", density=2.0, measurements=[
            {"component": "C1", "material_version": "v1", "quantity": 10.0},
        ])
        c1 = first["conclusion"]
        self.assertEqual(c1["valid"], 1)
        second = self._batch("B-002", density=2.0, measurements=[
            {"component": "C1", "material_version": "v2", "quantity": 20.0},
        ])
        c2 = second["conclusion"]
        self.assertEqual(c2["valid"], 1)
        self.assertGreater(c2["priority"], c1["priority"])
        # 旧结论失效，且同一时刻只有一个生效结论
        old = self.repo.conn.execute(
            "SELECT valid FROM conclusions WHERE id=?", (c1["id"],)).fetchone()
        self.assertEqual(old["valid"], 0)
        valid = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM conclusions WHERE item_id=? AND valid=1",
            (self.item["id"],)).fetchone()["n"]
        self.assertEqual(valid, 1)

    def test_density_change_recomputes_priority_and_review(self):
        first = self._batch("B-001", density=2.0, measurements=[
            {"component": "C1", "material_version": "v1", "quantity": 5.0},
        ])
        second = self._batch("B-002", density=9.0, measurements=[
            {"component": "C1", "material_version": "v2", "quantity": 5.0},
        ])
        self.assertGreater(second["conclusion"]["priority"],
                           first["conclusion"]["priority"])
        # 人员密度达到阈值后触发升级/审核
        self.assertEqual(second["conclusion"]["escalation_required"], 1)
        self.assertEqual(first["conclusion"]["escalation_required"], 0)

    def test_same_batch_no_ingested_only_once(self):
        first = self._batch("B-001", measurements=[
            {"component": "C1", "material_version": "v1", "quantity": 10.0},
        ])
        second = self._batch("B-001", measurements=[
            {"component": "C1", "material_version": "v1", "quantity": 99.0},
        ])
        self.assertEqual(first["batch"]["id"], second["batch"]["id"])
        self.assertEqual(len(second["measurements"]), 1)
        self.assertEqual(second["measurements"][0]["quantity"], 10.0)
        self.assertEqual(second["batch"]["status"], "committed")
        self.assertEqual(len(self.repo.list_batches(self.item["id"])), 1)

    def test_write_failure_retains_batch_for_retry(self):
        original = self.repo.write_batch_data
        state = {"calls": 0}

        def flaky(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("simulated write failure")
            return original(*args, **kwargs)

        self.repo.write_batch_data = flaky
        with self.assertRaises(RuntimeError):
            self._batch("B-FAIL", measurements=[
                {"component": "C1", "material_version": "v1", "quantity": 10.0},
            ])
        batch = self.repo.get_batch_by_no("B-FAIL")
        self.assertIsNotNone(batch)
        self.assertEqual(batch["status"], "failed")
        # 失败事务回滚，未写入任何测量
        self.assertEqual(len(self.repo.list_measurements(batch["id"])), 0)
        # 同批次号重试成功
        self.repo.write_batch_data = original
        result = self._batch("B-FAIL", measurements=[
            {"component": "C1", "material_version": "v1", "quantity": 10.0},
        ])
        self.assertEqual(result["batch"]["status"], "committed")
        self.assertEqual(len(result["measurements"]), 1)
        self.assertEqual(result["measurements"][0]["status"], "effective")

    def test_old_work_order_missing_fields_reads_back_and_completes(self):
        # 旧工单无密度字段，读回不报错且按缺省处理
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertIsNone(item["density"])
        self.assertIn("priority", item)
        # 补全字段后照常读回
        updated = self.service.update_item(
            self.item["id"], {"density": 3.5}, "editor", "assessor")
        self.assertEqual(updated["density"], 3.5)
        reread = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(reread["density"], 3.5)

    def test_invalid_measurement_rejected_before_write(self):
        with self.assertRaises(ValidationError):
            self._batch("B-BAD", measurements=[
                {"component": "C1", "material_version": "v1", "quantity": -1.0},
            ])
        self.assertIsNone(self.repo.get_batch_by_no("B-BAD"))


if __name__ == "__main__":
    unittest.main()
