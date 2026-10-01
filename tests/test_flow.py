import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ProcurementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc2", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def bid(self, vendor, actor, number, price, quality):
        return self.service.submit_bid(actor, "vendor", self.tender["id"], vendor["id"], {"报价": price, "质量": quality}, price)

    def open(self):
        time.sleep(2.1)
        return self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])

    def evaluate_all(self, bids, quality, version, actor="eval1"):
        for bid in bids:
            result = self.service.evaluate_bid(actor, "evaluator", bid["id"], {"报价": bid["price"], "质量": quality}, version)
            version = result["tender_version"]
        return version

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        opened = self.open()
        self.assertEqual(2, len(opened["bids"]))
        version = opened["tender"]["version"]
        version = self.evaluate_all([opened["bids"][0]], 90, version, "eval1")
        version = self.evaluate_all([opened["bids"][0]], 90, version, "eval2")
        version = self.evaluate_all([opened["bids"][1]], 80, version, "eval1")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        self.assertEqual("pending", current["tender"]["results_status"])
        confirmed = self.service.confirm_results("proc1", "procurement", self.tender["id"], current["tender"]["version"])
        self.assertEqual("confirmed", confirmed["results"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], confirmed["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(confirmed["results"]["id"], award["award"]["results_id"])

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        opened = self.open()
        version = opened["tender"]["version"]
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90}, version)
        self.assertEqual(403, ctx.exception.status)
        result = self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90}, version)
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90}, result["tender_version"])
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        opened = self.open()
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90}, opened["tender"]["version"])
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分标准理解有误")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        resolved = self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "按新规则重评")
        self.assertEqual("accepted", resolved["status"])
        updated = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.assertEqual("reevaluation", updated["status"])
        self.assertEqual(2, updated["evaluation_round"])
        with self.assertRaises(DomainError) as ctx:
            self.service.open_bids("vendor1", "vendor", self.tender["id"], updated["version"])
        self.assertEqual(403, ctx.exception.status)

    def test_concurrent_evaluation_accepts_first_and_prompts_reconfirm(self):
        bid = self.bid(self.vendor1, "vendor1", "B5", 800000, 90)
        opened = self.open()
        version = opened["tender"]["version"]
        barrier = threading.Barrier(2)
        outcomes = []

        def submit(evaluator):
            try:
                barrier.wait(timeout=5)
                result = self.service.evaluate_bid(evaluator, "evaluator", bid["id"], {"报价": 800000, "质量": 90}, version)
                outcomes.append(("ok", evaluator, result))
            except DomainError as exc:
                outcomes.append(("error", evaluator, exc))

        threads = [threading.Thread(target=submit, args=(name,)) for name in ("eval1", "eval2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        accepted = [item for item in outcomes if item[0] == "ok"]
        rejected = [item for item in outcomes if item[0] == "error"]
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))
        self.assertEqual(409, rejected[0][2].status)
        self.assertIn("重新确认", str(rejected[0][2]))
        fresh = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        retry = self.service.evaluate_bid(rejected[0][1], "evaluator", bid["id"], {"报价": 800000, "质量": 90}, fresh["version"])
        self.assertEqual(fresh["version"] + 1, retry["tender_version"])

    def test_complaint_invalidates_results_and_requires_reconfirm_before_award(self):
        self.bid(self.vendor1, "vendor1", "B6", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B7", 700000, 80)
        opened = self.open()
        version = self.evaluate_all(opened["bids"], 90, opened["tender"]["version"])
        confirmed = self.service.confirm_results("proc1", "procurement", self.tender["id"], version)
        self.assertEqual(1, confirmed["results"]["evaluation_round"])
        complaint = self.service.submit_complaint("vendor2", "vendor", self.tender["id"], "评分有误")
        self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "重新评审")
        view = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("reevaluation", view["tender"]["status"])
        self.assertEqual("pending", view["tender"]["results_status"])
        invalidated = [row for row in view["results"] if row["status"] == "invalidated"]
        self.assertEqual(1, len(invalidated))
        self.assertEqual(1, invalidated[0]["evaluation_round"])
        self.assertTrue(any(row["evaluation_round"] == 1 for row in view["evaluations"]))
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], view["tender"]["version"])
        self.assertEqual(409, ctx.exception.status)
        version = self.evaluate_all(opened["bids"], 95, view["tender"]["version"])
        with self.assertRaises(DomainError) as ctx2:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], version)
        self.assertEqual(409, ctx2.exception.status)
        reconfirmed = self.service.confirm_results("sup1", "supervisor", self.tender["id"], version)
        self.assertEqual(2, reconfirmed["results"]["evaluation_round"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], reconfirmed["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][1]["id"], award["award"]["winner"]["bid_id"])
        history = self.service.get_tender("sup1", "supervisor", self.tender["id"])["results"]
        self.assertEqual({"invalidated", "awarded"}, {row["status"] for row in history})

    def test_incomplete_scores_block_confirm_and_award(self):
        self.bid(self.vendor1, "vendor1", "B8", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B9", 700000, 80)
        opened = self.open()
        result = self.service.evaluate_bid(
            "eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90}, opened["tender"]["version"]
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_results("proc1", "procurement", self.tender["id"], result["tender_version"])
        self.assertEqual(409, ctx.exception.status)
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        with self.assertRaises(DomainError) as ctx2:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])
        self.assertEqual(409, ctx2.exception.status)

    def test_new_evaluation_after_confirm_supersedes_results(self):
        bid = self.bid(self.vendor1, "vendor1", "B10", 800000, 90)
        opened = self.open()
        version = self.evaluate_all([bid], 90, opened["tender"]["version"], "eval1")
        confirmed = self.service.confirm_results("proc1", "procurement", self.tender["id"], version)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 88}, confirmed["tender"]["version"])
        view = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("pending", view["tender"]["results_status"])
        self.assertEqual("superseded", view["results"][0]["status"])
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], view["tender"]["version"])
        self.assertEqual(409, ctx.exception.status)

    def test_failed_award_restores_results_and_keeps_complaint_records(self):
        bid = self.bid(self.vendor1, "vendor1", "B11", 800000, 90)
        opened = self.open()
        version = self.evaluate_all([bid], 90, opened["tender"]["version"])
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "程序疑问")
        self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "rejected", "投诉不成立")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        with self.assertRaises(DomainError) as ctx:
            self.service.fail_award("sup1", "supervisor", self.tender["id"], "尚未授标", current["version"])
        self.assertEqual(409, ctx.exception.status)
        confirmed = self.service.confirm_results("proc1", "procurement", self.tender["id"], current["version"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], confirmed["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        failed = self.service.fail_award("sup1", "supervisor", self.tender["id"], "中标人放弃履约", award["tender"]["version"])
        self.assertEqual("opened", failed["tender"]["status"])
        self.assertIsNone(failed["tender"]["awarded_bid_id"])
        view = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("confirmed", view["tender"]["results_status"])
        self.assertEqual(1, len(view["complaints"]))
        self.assertEqual("rejected", view["complaints"][0]["status"])
        self.assertEqual(2, len(view["evaluations"]))
        restored_bid = [row for row in view["bids"] if row["id"] == bid["id"]][0]
        self.assertEqual("opened", restored_bid["status"])
        reaward = self.service.award_tender("sup1", "supervisor", self.tender["id"], failed["tender"]["version"])
        self.assertEqual("awarded", reaward["tender"]["status"])
        self.assertEqual(bid["id"], reaward["award"]["winner"]["bid_id"])


if __name__ == "__main__":
    unittest.main()
