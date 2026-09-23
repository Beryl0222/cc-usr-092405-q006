"""住宿权益账本、唯一房态、撤回、调剂与人工例外测试。"""

import threading
from datetime import date, datetime

from station.errors import (BookingConflictError, DomainError, QuotaError,
                           ReviewRequiredError, RoomUnavailableError,
                           ValidationError)
from station.models import (APP_ELIGIBLE, APP_IN_REVIEW,
                            NIGHT_AWAY, NIGHT_HELD, NIGHT_NO_SHOW, NIGHT_STAYED,
                            TICKET_APPROVED, TICKET_OPEN, TICKET_REJECTED)
from station.timeutil import CST
from tests.world import WorldTest, event


class EntitlementAndUniquenessTest(WorldTest):
    def test_application_freezes_policy_in_effect_at_submission(self):
        # 今天 2026-09-22，有效政策为 P2026
        actor, app, _ = self.apply()
        self.assertEqual(app.status, APP_ELIGIBLE)
        self.assertEqual(app.policy_code, "P2026")
        self.assertEqual(app.eligibility["policy_code"], "P2026")

    def test_old_policy_applicant_kept_when_policy_changes(self):
        # 2025 版大专可过；即使后来切到 P2026（大专被移除），已确认资格不受影响
        self.clock.set(datetime(2025, 6, 1, 9, 0, tzinfo=CST))
        from tests.world import applicant
        actor, material = applicant(self.system, "u_old", "老李", degree="大专",
                                    graduation_date="2024-06-30")
        app = self.system.submit_application(actor, material)
        self.assertEqual(app.policy_code, "P2025")
        alloc = self.system.confirm_booking(actor, app.id, "H01", "2025-06-01", "2025-06-10")
        self.assertEqual(alloc.policy_code, "P2025")
        # 夜线锁定的是 P2025 单价
        self.assertEqual(alloc.nights["2025-06-01"].subsidy_locked, 80.0)

    def test_cross_hotel_overlap_rejected(self):
        actor, app, _ = self.apply()
        self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-25")
        with self.assertRaises(BookingConflictError):
            self.system.confirm_booking(actor, app.id, "H02", "2026-09-24", "2026-09-26")

    def test_entitlement_is_shared_across_transfers(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-10-05")
        self.assertEqual(self.system.entitlement(actor.id)["used_nights"], 14)
        self.check_in(self.front1, a1)
        tr = self.system.transfer(actor, a1.id, "H02", "2026-09-25")
        a2 = tr["new_allocation"]
        self.assertEqual(a2.chain_id, a1.chain_id)
        # 9/22-24（3 夜）在 H01，9/25-10/05（11 夜）在 H02，合计仍 14
        ent = self.system.entitlement(actor.id)
        self.assertEqual(ent["used_nights"], 14)
        self.assertEqual(ent["remaining_nights"], 16)

    def test_withdraw_releases_room_immediately(self):
        actor, app, _ = self.apply()
        alloc = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-30")
        bed = alloc.bed_key
        self.system.withdraw_dates(actor, alloc.id, ["2026-09-26", "2026-09-27"])
        # 床位夜索引立即释放：另一申请人可以订这两晚
        actor2, app2, _ = self.apply(uid="u_wang", name="王五")
        a2 = self.system.confirm_booking(actor2, app2.id, "H01", "2026-09-26",
                                         "2026-09-27", bed_key=bed)
        self.assertEqual(a2.bed_key, bed)
        # 第一人权益相应减少
        self.assertEqual(self.system.entitlement(actor.id)["used_nights"], 7)

    def test_withdraw_used_night_rejected(self):
        actor, app, _ = self.apply()
        alloc = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-30")
        self.check_in(self.front1, alloc)
        with self.assertRaises(ValidationError):
            self.system.withdraw_dates(actor, alloc.id, ["2026-09-22"])

    def test_quota_capped_at_thirty_nights(self):
        actor, app, _ = self.apply()
        with self.assertRaises(QuotaError):
            self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-10-25")

    def test_quota_accumulates_then_releases_on_cancel(self):
        actor, app, _ = self.apply()
        alloc = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-10-01")
        self.assertEqual(self.system.entitlement(actor.id)["remaining_nights"], 20)
        self.system.cancel_booking(actor, alloc.id)
        self.assertEqual(self.system.entitlement(actor.id)["remaining_nights"], 30)


class TransferTest(WorldTest):
    def test_transfer_failure_rolls_back_completely(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-24")
        self.check_in(self.front1, a1)
        # H02 唯一床位 9/24 已被别人占走 → 调剂失败
        other, app2, _ = self.apply(uid="u_other", name="赵六")
        self.system.confirm_booking(other, app2.id, "H02", "2026-09-24", "2026-09-24")
        with self.assertRaises(RoomUnavailableError):
            self.system.transfer(actor, a1.id, "H02", "2026-09-24")
        # 原单完好：夜态、索引、结束日都没被破坏
        self.assertEqual(a1.nights["2026-09-24"].state, NIGHT_HELD)
        self.assertEqual(
            self.system._bed_night[("H01", a1.bed_key, "2026-09-24")], a1.id)
        self.assertEqual(a1.end, "2026-09-24")

    def test_transfer_does_not_revive_withdrawn_nights(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-30")
        self.check_in(self.front1, a1)
        self.system.withdraw_dates(actor, a1.id, ["2026-09-29", "2026-09-30"])
        tr = self.system.transfer(actor, a1.id, "H02", "2026-09-25")
        new = tr["new_allocation"]
        self.assertNotIn("2026-09-29", new.nights)
        self.assertNotIn("2026-09-30", new.nights)
        self.assertEqual(new.end, "2026-09-28")

    def test_transfer_blocked_with_open_ticket(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-24")
        # 爽约工单
        scan = self.system.run_daily_review_scan(self.verifier, "2026-09-23")
        self.assertTrue(scan["tickets_opened"])
        with self.assertRaises(ValidationError):
            self.system.transfer(actor, a1.id, "H02", "2026-09-23")


class DailyReviewTest(WorldTest):
    def test_no_show_freezes_then_manual_approve_releases(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-24")
        scan = self.system.run_daily_review_scan(self.verifier, "2026-09-23")
        self.assertEqual(len(scan["tickets_opened"]), 1)
        tid = scan["tickets_opened"][0]
        ticket = self.system.tickets[tid]
        self.assertEqual(ticket.owner_id, "u_duty")  # 责任人为值班长
        # 冻结但不释放：床仍不可被他人订走
        self.assertIn(("H01", a1.bed_key, "2026-09-23"), self.system._bed_night)
        # 值班长认定爽约
        self.system.decide_ticket(self.duty, tid, TICKET_APPROVED, "未到店且无联系")
        self.assertEqual(a1.nights["2026-09-22"].state, NIGHT_NO_SHOW)
        self.assertNotIn(("H01", a1.bed_key, "2026-09-22"), self.system._bed_night)
        self.assertEqual(self.system.entitlement(actor.id)["used_nights"], 0)

    def test_no_show_rejected_treats_as_stayed(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-23")
        scan = self.system.run_daily_review_scan(self.verifier, "2026-09-23")
        tid = scan["tickets_opened"][0]
        # 客人其实是深夜到店并补刷了门锁
        self.system.ingest_event(self.front1, event(
            "ci_late", "door_lock", "check_in", "H01", a1.bed_key,
            "2026-09-22T23:30:00+08:00"))
        self.system.decide_ticket(self.duty, tid, TICKET_REJECTED,
                                  "客人因列车晚点深夜到店，门锁记录可证", billable=True)
        self.assertEqual(a1.nights["2026-09-22"].state, NIGHT_STAYED)
        self.assertTrue(a1.nights["2026-09-22"].billable)

    def test_overstay_blocks_bed_and_requires_review(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-23")
        self.check_in(self.front1, a1)
        scan = self.system.run_daily_review_scan(self.verifier, "2026-09-25")
        tid = [t for t in scan["tickets_opened"]
               if self.system.tickets[t].type == "overstay"][0]
        # 超期夜占位：他人订不到同床
        other, app2, _ = self.apply(uid="u_o2", name="钱七")
        with self.assertRaises(RoomUnavailableError):
            self.system.confirm_booking(other, app2.id, "H01", "2026-09-24",
                                        "2026-09-24", bed_key=a1.bed_key)
        # 批准紧急延住且计补贴
        self.system.decide_ticket(self.duty, tid, TICKET_APPROVED,
                                  "台风停运，同意延住", billable=True)
        self.assertEqual(a1.nights["2026-09-24"].state, NIGHT_STAYED)
        self.assertTrue(a1.nights["2026-09-24"].billable)
        self.assertTrue(a1.nights["2026-09-24"].compliant)

    def test_emergency_extension_is_never_auto_approved(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-23")
        self.check_in(self.front1, a1)
        with self.assertRaises(ReviewRequiredError) as cm:
            self.system.request_emergency_extension(actor, a1.id, 2, "面试推迟")
        self.assertEqual(cm.exception.review_type, "emergency_extension")
        tid = cm.exception.ticket_id
        self.assertEqual(self.system.tickets[tid].status, TICKET_OPEN)
        # 批准后追加 2 夜
        self.system.decide_ticket(self.duty, tid, TICKET_APPROVED, "情况属实", billable=True)
        self.assertEqual(a1.end, "2026-09-25")
        self.assertTrue(a1.nights["2026-09-24"].billable)

    def test_emergency_extension_consumes_scanned_overstay_nights(self):
        """扫描已占位的超期夜，在延住批准时转为合规而不是重复追加。"""
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-23")
        self.check_in(self.front1, a1)
        scan = self.system.run_daily_review_scan(self.verifier, "2026-09-25")
        over_tid = [t for t in scan["tickets_opened"]
                    if self.system.tickets[t].type == "overstay"][0]
        # 超期 9/24 夜已存在；客人就这两夜发起紧急延住
        with self.assertRaises(ReviewRequiredError) as cm:
            self.system.request_emergency_extension(actor, a1.id, 2, "台风停运")
        ext_tid = cm.exception.ticket_id
        # 先驳回超期工单之外的另一张超期单不存在——批准延住应消化 9/24
        self.system.decide_ticket(self.duty, ext_tid, TICKET_APPROVED,
                                  "同意延住", billable=True)
        self.assertEqual(a1.nights["2026-09-24"].state, NIGHT_STAYED)
        self.assertTrue(a1.nights["2026-09-24"].billable)
        # 延住 2 夜 = 消化 9/24 + 追加 9/25
        self.assertIn("2026-09-25", a1.nights)
        self.assertEqual(a1.end, "2026-09-25")

    def test_emergency_extension_over_policy_limit_rejected_upfront(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-23")
        with self.assertRaises(ValidationError):
            self.system.request_emergency_extension(actor, a1.id, 5, "想多住几天")

    def test_blocked_night_still_consumes_entitlement(self):
        """爽约冻结期间权益不归还，防止争议未决又拿额度订房。"""
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-24")
        before = self.system.entitlement(actor.id)["remaining_nights"]
        self.system.run_daily_review_scan(self.verifier, "2026-09-25")
        during = self.system.entitlement(actor.id)
        self.assertEqual(during["remaining_nights"], before)
        self.assertTrue(during["blocked_nights"])

    def test_eligibility_appeal_flow(self):
        # 超过毕业年限 10 天 → 临界，进入人工复核
        from tests.world import applicant
        actor, material = applicant(self.system, "u_edge", "临界生",
                                    graduation_date="2023-09-12")
        with self.assertRaises(ReviewRequiredError) as cm:
            self.system.submit_application(actor, material)
        tid = cm.exception.ticket_id
        app = next(a for a in self.system.applications.values()
                   if a.applicant_id == "u_edge")
        self.assertEqual(app.status, APP_IN_REVIEW)
        self.system.decide_ticket(self.duty, tid, TICKET_APPROVED, "证明日期有误")
        self.assertEqual(app.status, APP_ELIGIBLE)
        # 之后可正常占房
        alloc = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-23")
        self.assertTrue(alloc.nights)

    def test_eligibility_hard_deny_has_no_ticket(self):
        from tests.world import applicant
        actor, material = applicant(self.system, "u_bad", "不符合", degree="高中")
        from station.errors import PolicyError
        with self.assertRaises(PolicyError):
            self.system.submit_application(actor, material)
        self.assertFalse(any(
            t.applicant_id == "u_bad" for t in self.system.tickets.values()))


class TempLeaveTest(WorldTest):
    def _staying(self):
        actor, app, _ = self.apply()
        a1 = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-26")
        self.check_in(self.front1, a1)
        return actor, a1

    def test_leave_keeps_room_but_no_subsidy(self):
        actor, a1 = self._staying()
        self.system.ingest_event(self.front1, event(
            "lv", "front_desk", "temp_leave", "H01", a1.bed_key,
            "2026-09-24T08:00:00+08:00", expected_return="2026-09-26"))
        self.assertEqual(a1.nights["2026-09-24"].state, NIGHT_AWAY)
        self.assertEqual(a1.nights["2026-09-25"].state, NIGHT_AWAY)
        self.assertFalse(a1.nights["2026-09-24"].billable)
        # 房间仍保留，他人不可订
        other, app2, _ = self.apply(uid="u_x", name="孙八")
        with self.assertRaises(RoomUnavailableError):
            self.system.confirm_booking(other, app2.id, "H01", "2026-09-24",
                                        "2026-09-24", bed_key=a1.bed_key)

    def test_unclosed_leave_scanned_to_review(self):
        actor, a1 = self._staying()
        self.system.ingest_event(self.front1, event(
            "lv", "front_desk", "temp_leave", "H01", a1.bed_key,
            "2026-09-23T08:00:00+08:00", expected_return="2026-09-24"))
        scan = self.system.run_daily_review_scan(self.verifier, "2026-09-26")
        self.assertTrue(any(
            self.system.tickets[t].type == "unclosed_leave"
            for t in scan["tickets_opened"]))


class ConcurrencyTest(WorldTest):
    def test_two_applicants_racing_last_bed_has_unique_winner(self):
        """两家酒店/两位申请人争抢同一最后床位：恰好一个赢。"""
        a1, app1, _ = self.apply(uid="u_race1", name="甲")
        a2, app2, _ = self.apply(uid="u_race2", name="乙")
        results = {}

        def race(who, app, ev):
            try:
                alloc = self.system.confirm_booking(
                    who, app.id, "H02", "2026-10-01", "2026-10-03")  # H02 只有 1 床
                results[ev] = ("ok", alloc.id)
            except DomainError as exc:
                results[ev] = (exc.code, None)

        t1 = threading.Thread(target=race, args=(a1, app1, "t1"))
        t2 = threading.Thread(target=race, args=(a2, app2, "t2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = [results["t1"][0], results["t2"][0]]
        self.assertEqual(sorted(statuses), ["ROOM_UNAVAILABLE", "ok"])
        # 唯一房态：索引只有一个占用人
        owners = {self.system._bed_night[("H02", "301-01", f"2026-10-0{i}")]
                  for i in (1, 2, 3)}
        self.assertEqual(len(owners), 1)

    def test_offline_events_racing_same_bed_still_unique(self):
        """离线补传与实时确认并发：无论谁先落，房态仍唯一、事件可去重。"""
        a1, app1, _ = self.apply(uid="u_r1", name="离线客")
        alloc = self.system.confirm_booking(a1, app1.id, "H01", "2026-09-22", "2026-09-24")
        # 同一入住被门锁与前台各补一次（不同 event_id，时间窗内语义重复）
        p1 = event("off1", "door_lock", "check_in", "H01", alloc.bed_key,
                   "2026-09-22T13:55:00+08:00", recorded_at="2026-09-22T18:00:00+08:00",
                   offline=True)
        p2 = event("off2", "front_desk", "check_in", "H01", alloc.bed_key,
                   "2026-09-22T14:00:00+08:00", recorded_at="2026-09-22T18:05:00+08:00",
                   offline=True)
        r1 = self.system.ingest_event(self.front1, p1)["result"]["status"]
        r2 = self.system.ingest_event(self.front1, p2)["result"]["status"]
        self.assertEqual(sorted([r1, r2]), ["applied", "duplicate"])
        self.assertEqual(alloc.nights["2026-09-22"].state, NIGHT_STAYED)
        # 再次用完全相同的 event_id 补传：幂等
        r3 = self.system.ingest_event(self.front1, p1)["result"]["status"]
        self.assertEqual(r3, "duplicate")


if __name__ == "__main__":
    unittest.main()
