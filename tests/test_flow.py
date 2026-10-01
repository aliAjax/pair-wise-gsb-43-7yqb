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
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
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

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(2, len(opened["bids"]))
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][1]["id"], {"报价": 700000, "质量": 80})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
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

    def _opened_bids(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        by_vendor = {b["vendor_id"]: b for b in opened["bids"]}
        return [by_vendor[self.vendor1["id"]], by_vendor[self.vendor2["id"]]]

    def _awardable(self):
        bids = self._opened_bids()
        self.service.evaluate_bid("eval1", "evaluator", bids[0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bids[0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bids[1]["id"], {"报价": 700000, "质量": 80})
        self.service.evaluate_bid("eval2", "evaluator", bids[1]["id"], {"报价": 700000, "质量": 80})
        return bids

    def test_complaint_acceptance_invalidates_old_result_immediately(self):
        """投诉受理后旧评分和排名立即失效，重新确认前拒绝授标。"""
        bids = self._awardable()
        # 评审结果先经确认（产生排名快照，成为授标版本）
        before = self.service.get_tender("proc1", "procurement", self.tender["id"])
        confirmed = self.service.confirm_evaluations("proc1", "procurement", self.tender["id"], before["tender"]["version"])
        tender_before = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("confirmed", tender_before["current_round"]["status"])
        stale_version = tender_before["tender"]["version"]
        complaint = self.service.submit_complaint("vendor2", "vendor", self.tender["id"], "第一名评分有误")
        view = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        # 旧轮立即失效，但仍可追溯
        self.assertEqual("invalidated", view["evaluation_rounds"][0]["status"])
        self.assertEqual(complaint["id"], view["evaluation_rounds"][0]["complaint_id"])
        # 旧排名快照还在
        self.assertIsNotNone(view["evaluation_rounds"][0]["ranking_snapshot"])
        self.assertEqual(confirmed["winner"]["bid_id"], view["evaluation_rounds"][0]["ranking_snapshot"]["winner"]["bid_id"])
        # 旧版本号上授标：版本冲突
        with self.assertRaises(DomainError) as stale:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], stale_version)
        self.assertEqual(409, stale.exception.status)
        # 刷新后授标：明确提示旧结果失效
        with self.assertRaises(DomainError) as blocked:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], view["tender"]["version"])
        self.assertEqual(409, blocked.exception.status)
        self.assertIn("重新确认", str(blocked.exception))
        # 投诉未处理完也不允许确认
        with self.assertRaises(DomainError):
            self.service.confirm_evaluations("proc1", "procurement", self.tender["id"], view["tender"]["version"])

    def test_rejected_complaint_requires_explicit_reconfirm_before_award(self):
        """驳回不会让旧排名复活，必须重新确认。"""
        self._awardable()
        complaint = self.service.submit_complaint("vendor2", "vendor", self.tender["id"], "对评分有异议")
        view = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        resolved = self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "rejected", "投诉不成立")
        self.assertEqual("rejected", resolved["status"])
        view = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("invalidated", view["current_round"]["status"])
        with self.assertRaises(DomainError) as blocked:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], view["tender"]["version"])
        self.assertIn("重新确认", str(blocked.exception))
        # 重新确认后恢复授标，排名沿用保留下来的旧评分
        confirmed = self.service.confirm_evaluations("proc1", "procurement", self.tender["id"], view["tender"]["version"])
        self.assertEqual("confirmed", self.service.get_tender("sup1", "supervisor", self.tender["id"])["current_round"]["status"])
        fresh = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], fresh["tender"]["version"])
        self.assertEqual(1, award["round_no"])
        self.assertEqual(confirmed["winner"]["bid_id"], award["award"]["winner"]["bid_id"])

    def test_old_results_remain_traceable_during_reevaluation(self):
        """重评期间原结果仍可追溯：旧轮次、旧评分、旧快照都保留。"""
        bids = self._awardable()
        before = self.service.get_tender("proc1", "procurement", self.tender["id"])
        first_confirmation = self.service.confirm_evaluations("proc1", "procurement", self.tender["id"], before["tender"]["version"])
        complaint = self.service.submit_complaint("vendor2", "vendor", self.tender["id"], "评分有误")
        self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "改判重评")
        view = self.service.get_tender("auditor1", "auditor", self.tender["id"])
        self.assertEqual(2, view["tender"]["evaluation_round"])
        self.assertEqual([1, 2], [r["round_no"] for r in view["evaluation_rounds"]])
        self.assertEqual("invalidated", view["evaluation_rounds"][0]["status"])
        self.assertIsNotNone(view["evaluation_rounds"][0]["ranking_snapshot"])
        self.assertEqual("in_progress", view["evaluation_rounds"][1]["status"])
        # 第 1 轮的评分记录原样保留：2 投标 × 2 评审 × 2 评分项
        round1 = [e for e in view["evaluations"] if e["evaluation_round"] == 1]
        self.assertEqual(8, len(round1))
        # 重评后授标用的是第 2 轮
        self.service.evaluate_bid("eval1", "evaluator", bids[0]["id"], {"报价": 800000, "质量": 70})
        self.service.evaluate_bid("eval2", "evaluator", bids[0]["id"], {"报价": 800000, "质量": 70})
        self.service.evaluate_bid("eval1", "evaluator", bids[1]["id"], {"报价": 700000, "质量": 95})
        self.service.evaluate_bid("eval2", "evaluator", bids[1]["id"], {"报价": 700000, "质量": 95})
        fresh = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], fresh["tender"]["version"])
        self.assertEqual(2, award["round_no"])
        self.assertEqual(bids[1]["id"], award["award"]["winner"]["bid_id"])
        # 旧轮依旧可追溯
        after = self.service.get_tender("auditor1", "auditor", self.tender["id"])
        self.assertIsNotNone(after["evaluation_rounds"][0]["ranking_snapshot"])

    def test_cannot_award_when_scores_incomplete(self):
        """评分没补齐时不能确认、不能授标。"""
        bids = self._opened_bids()
        self.service.evaluate_bid("eval1", "evaluator", bids[0]["id"], {"报价": 800000, "质量": 90})
        view = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError) as blocked:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], view["tender"]["version"])
        self.assertEqual(409, blocked.exception.status)
        self.assertIn("尚未完成全部评分", str(blocked.exception))
        with self.assertRaises(DomainError):
            self.service.confirm_evaluations("sup1", "supervisor", self.tender["id"], view["tender"]["version"])

    def test_concurrent_evaluations_only_first_version_accepted(self):
        """两名评审同时提交同一投标：只接收先到版本，后到者被要求重新确认。"""
        bids = self._opened_bids()
        bid_id = bids[0]["id"]
        view = self.service.get_tender("proc1", "procurement", self.tender["id"])
        version = view["tender"]["version"]
        results = {}

        def submit(evaluator):
            try:
                results[evaluator] = self.service.evaluate_bid(
                    evaluator, "evaluator", bid_id, {"报价": 800000, "质量": 90},
                    expected_version=version,
                )
            except DomainError as exc:
                results[evaluator] = exc

        t1 = threading.Thread(target=submit, args=("eval1",))
        t2 = threading.Thread(target=submit, args=("eval2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        accepted = [k for k, v in results.items() if not isinstance(v, DomainError)]
        rejected = [k for k, v in results.items() if isinstance(v, DomainError)]
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))
        self.assertEqual(409, results[rejected[0]].status)
        self.assertIn("重新确认", str(results[rejected[0]]))
        # 后到者用最新版本重新确认后可以提交
        fresh = self.service.get_tender("proc1", "procurement", self.tender["id"])
        again = self.service.evaluate_bid(
            rejected[0], "evaluator", bid_id, {"报价": 800000, "质量": 90},
            expected_version=fresh["tender"]["version"],
        )
        self.assertEqual(1, again["round"])

    def test_award_failure_restores_latest_round_and_keeps_complaints(self):
        """授标失败后恢复最近评审结果，投诉处理记录不丢。"""
        bids = self._awardable()
        complaint = self.service.submit_complaint("vendor2", "vendor", self.tender["id"], "评分有误")
        self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "改判重评")
        self.service.evaluate_bid("eval1", "evaluator", bids[0]["id"], {"报价": 800000, "质量": 95})
        self.service.evaluate_bid("eval2", "evaluator", bids[0]["id"], {"报价": 800000, "质量": 95})
        self.service.evaluate_bid("eval1", "evaluator", bids[1]["id"], {"报价": 700000, "质量": 70})
        self.service.evaluate_bid("eval2", "evaluator", bids[1]["id"], {"报价": 700000, "质量": 70})
        before = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], before["tender"]["version"])
        winner = award["award"]["winner"]["bid_id"]
        self.assertEqual("awarded", award["tender"]["status"])
        # 供应商资格后审不通过等原因：授标失败，恢复最近评审结果
        restored = self.service.fail_award("sup1", "supervisor", self.tender["id"], "中标供应商资格后审不通过")
        self.assertEqual(2, restored["restored_round_no"])
        self.assertTrue(restored["complaints_preserved"])
        view = self.service.get_tender("auditor1", "auditor", self.tender["id"])
        self.assertIn(view["tender"]["status"], {"opened", "reevaluation"})
        self.assertIsNone(view["tender"]["awarded_bid_id"])
        # 中标投标退回可评审状态，评分保留
        winner_bid = next(b for b in view["bids"] if b["id"] == winner)
        self.assertIn(winner_bid["status"], {"opened", "qualified"})
        round2_scores = [e for e in view["evaluations"] if e["evaluation_round"] == 2]
        # 第2轮评分保留且不重复：2 投标 × 2 评审 × 2 评分项
        self.assertEqual(8, len(round2_scores))
        self.assertEqual(8, len({(e["bid_id"], e["evaluator"], e["criterion"]) for e in round2_scores}))
        # 最近评审结果仍是已确认状态，可以再次授标
        self.assertEqual("confirmed", view["current_round"]["status"])
        # 投诉处理记录完整保留
        self.assertEqual("accepted", self.service.state("auditor1", "auditor")["complaints"][0]["status"])
        attempts = view["award_attempts"]
        self.assertIn(attempts[0]["status"], {"succeeded", "reverted"})
        self.assertEqual("reverted", attempts[-1]["status"])

    def test_failed_award_attempt_rolls_back_and_still_audited(self):
        """评分没补齐触发的授标失败：状态不残留，但失败留痕保留。"""
        bids = self._opened_bids()
        self.service.evaluate_bid("eval1", "evaluator", bids[0]["id"], {"报价": 800000, "质量": 90})
        view = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], view["tender"]["version"])
        after = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", after["tender"]["status"])
        self.assertIsNone(after["tender"]["awarded_bid_id"])
        self.assertEqual("failed", after["award_attempts"][0]["status"])


if __name__ == "__main__":
    unittest.main()
