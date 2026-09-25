"""候补队列领域测试。

覆盖：可解释排序、房态恢复唯一保留、确认/超时/拒绝原子释放推进、
离线重复确认幂等不扣权益、既有住宿/申诉冻结/工单/额度冲突拦截、
跨日截止点、并发退订下的晋位顺序，以及快照恢复（重启）后顺序与期限不变。
"""

import threading
from datetime import datetime, timedelta

from station.errors import (PermissionError, QuotaError, ReviewRequiredError,
                           RoomUnavailableError, ValidationError,
                           WaitlistConflictError)
from station.models import NIGHT_BLOCKED, NIGHT_HELD
from station.timeutil import CST
from tests.world import NOW, WorldTest, applicant


def _wait(view, **kw):
    """在队列视图里按条件找条目。"""
    for item in view["queue"]:
        if all(item[k] == v for k, v in kw.items()):
            return item
    return None


class WaitlistTestBase(WorldTest):
    def occupy(self, uid, hotel, start, end, name=None):
        actor, material = applicant(self.system, uid, name or uid)
        app = self.system.submit_application(actor, material)
        alloc = self.system.confirm_booking(actor, app.id, hotel, start, end)
        return actor, app, alloc

    def fill_hotel(self, hotel, start, end):
        """占满某酒店全部床位（用于制造真正的满房候补），返回占房列表。"""
        beds = self.directory.get(hotel).beds
        out = []
        for i, bed in enumerate(beds):
            actor, _, alloc = self.occupy(
                f"u_fill_{hotel}_{i}", hotel, start, end, f"占位{i}")
            assert alloc.bed_key == bed.key
            out.append((actor, alloc))
        return out

    def waiter(self, uid, name=None, **kw):
        actor, material = applicant(self.system, uid, name or uid)
        app = self.system.submit_application(actor, material)
        entry = self.system.register_waitlist(
            actor, app.id, kw.get("start", "2026-09-23"),
            kw.get("end", "2026-09-24"),
            kw.get("hotels", ["H02"]),
            urgency=kw.get("urgency", "normal"),
            respond_by=kw.get("respond_by"))
        return actor, app, entry

    def entry(self, entry_id):
        return self.system.waitlist[entry_id]


class RegistrationAndRankTest(WaitlistTestBase):
    def test_register_explains_frozen_rank(self):
        self.fill_hotel("H02", "2026-09-23", "2026-09-24")  # 满房才排队
        actor, _, entry = self.waiter("u_a")
        self.assertEqual(entry.status, "queued")
        self.assertEqual(entry.rank_key, ["2026-09-22", 0, entry.register_seq])
        view = self.system.waitlist_entry_view(actor, entry.id)
        self.assertEqual(view["rank_reasons"]["decided_on"], "2026-09-22")
        self.assertEqual(view["rank_reasons"]["urgency_weight"], 0)
        self.assertEqual(view["rank_reasons"]["register_seq"], entry.register_seq)
        self.assertIn("资格决定时间", view["rank_reasons"]["rule"])
        # 尚无保留方案，不暴露 offer
        self.assertIsNone(view["offer"])
        # 排队期间不消耗权益
        self.assertEqual(self.system.entitlement("u_a")["used_nights"], 0)

    def test_rank_decided_on_then_urgency_then_sequence(self):
        # 甲的资格决定于 9/20（更早），乙 9/22 普通，丙 9/22 当日抵城紧急
        self.fill_hotel("H02", "2026-09-23", "2026-09-24")  # 满房才排队
        self.clock.set(datetime(2026, 9, 20, 9, 0, tzinfo=CST))
        a, _, ea = self.waiter("u_rank_a", "早资格")
        self.clock.set(NOW)
        _, _, eb = self.waiter("u_rank_b", "普通乙")
        _, _, ec = self.waiter("u_rank_c", "紧急丙", urgency="arriving_today")

        queue = self.system.waitlist_queue_view(self.verifier)["queue"]
        ids = [q["id"] for q in queue]
        self.assertEqual(ids, [ea.id, ec.id, eb.id])  # 早资格 > 紧急 > 普通同序
        self.assertEqual([q["queue_position"] for q in queue], [1, 2, 3])

        # 更早的资格即使普通也压过晚资格的紧急
        self.assertEqual(
            self.system.waitlist_entry_view(a, ea.id)["queue_position"], 1)

    def test_ineligible_application_cannot_register(self):
        # 资格临界转人工复核中的申请不能登记候补
        actor, material = applicant(self.system, "u_edge", "临界",
                                    graduation_date="2023-09-12")
        with self.assertRaises(ReviewRequiredError):
            self.system.submit_application(actor, material)
        app = self.system.applications[next(iter(self.system.applications))]
        # 取最新一条（即临界申请）
        app = list(self.system.applications.values())[-1]
        with self.assertRaises(ValidationError):
            self.system.register_waitlist(
                actor, app.id, "2026-09-23", "2026-09-24", ["H02"])

    def test_duplicate_overlapping_wait_rejected(self):
        actor, _, entry = self.waiter("u_dup")
        with self.assertRaises(WaitlistConflictError) as ctx:
            self.system.register_waitlist(
                actor, self.system.applications[entry.app_id].id,
                "2026-09-24", "2026-09-25", ["H02"])
        self.assertEqual(ctx.exception.context["waitlist_id"], entry.id)

    def test_quota_shortfall_rejected_at_registration(self):
        actor, material = applicant(self.system, "u_big", "大段候补")
        app = self.system.submit_application(actor, material)
        with self.assertRaises(QuotaError) as ctx:
            self.system.register_waitlist(
                actor, app.id, "2026-09-23", "2026-11-01", ["H02"])
        self.assertEqual(ctx.exception.context["remaining_nights"], 30)


class ConflictGuardsTest(WaitlistTestBase):
    def test_existing_stay_blocks_registration(self):
        actor, _, _ = self.occupy("u_stay", "H03", "2026-09-23", "2026-09-25")
        # 与既有占房重叠的候补直接拒绝
        app = list(self.system.applications.values())[-1]
        with self.assertRaises(WaitlistConflictError) as ctx:
            self.system.register_waitlist(
                actor, app.id, "2026-09-24", "2026-09-26", ["H02"])
        self.assertEqual(ctx.exception.context["reason"], "existing_stay")

    def test_open_ticket_blocks_registration(self):
        # 紧急延住工单未裁决：任何候补都不允许，防止绕过人工流程
        actor, _, alloc = self.occupy("u_tkt", "H03", "2026-09-22", "2026-09-23")
        with self.assertRaises(ReviewRequiredError):
            self.system.request_emergency_extension(actor, alloc.id, 2, "活动延期")
        app = list(self.system.applications.values())[-1]
        with self.assertRaises(WaitlistConflictError) as ctx:
            self.system.register_waitlist(
                actor, app.id, "2026-10-01", "2026-10-02", ["H02"])
        self.assertEqual(ctx.exception.context["reason"], "open_ticket")

    def test_frozen_night_blocks_registration(self):
        # 爽约扫描后夜被冻结且工单未裁决：候补不得越过申诉冻结
        actor, _, alloc = self.occupy("u_frz", "H03", "2026-09-22", "2026-09-24")
        self.clock.set(datetime(2026, 9, 23, 9, 0, tzinfo=CST))
        self.system.run_daily_review_scan(self.verifier, "2026-09-23")
        self.assertTrue(any(l.state == NIGHT_BLOCKED
                            for l in alloc.nights.values()))
        app = list(self.system.applications.values())[-1]
        with self.assertRaises(WaitlistConflictError) as ctx:
            self.system.register_waitlist(
                actor, app.id, "2026-10-01", "2026-10-02", ["H02"])
        self.assertEqual(ctx.exception.context["reason"], "frozen_night")

    def test_waiting_entry_blocked_then_recovers_with_original_rank(self):
        fill = self.fill_hotel("H02", "2026-09-23", "2026-09-24")
        _, _, ea = self.waiter("u_b1")
        _, _, eb = self.waiter("u_b2")
        # 甲排队期间又在 H03 订了重叠日期 → 下次晋位时转 blocked，乙先上
        a_actor = self.system.users["u_b1"]
        self.system.confirm_booking(
            a_actor, self.system.applications[ea.app_id].id,
            "H03", "2026-09-23", "2026-09-24")
        # 满房释放触发晋位：甲 blocked，乙拿到唯一保留
        fill_actor, fill_alloc = fill[0]
        self.system.cancel_booking(fill_actor, fill_alloc.id)
        self.assertEqual(self.entry(ea.id).status, "blocked")
        self.assertEqual(self.entry(ea.id).blocked_reason, "existing_stay")
        self.assertEqual(self.entry(eb.id).status, "offered")

        # 丙此时排队（乙持有保留，无空床）
        _, _, ec = self.waiter("u_b3")
        # 冲突消除（甲撤回 H03 占房）：甲原位归队，rank_key 不变
        a_alloc = next(a for a in self.system.allocations.values()
                       if a.applicant_id == "u_b1")
        self.system.cancel_booking(a_actor, a_alloc.id)
        self.assertEqual(self.entry(ea.id).status, "queued")
        self.assertEqual(self.entry(ea.id).rank_key,
                         ["2026-09-22", 0, ea.register_seq])
        queue = self.system.waitlist_queue_view(self.verifier)["queue"]
        active = [q["id"] for q in queue if q["status"] in ("queued", "offered")]
        # 乙仍持保留；甲按原 seq 排在丙前面
        self.assertEqual(active, [ea.id, eb.id, ec.id])


class OfferAndPromotionTest(WaitlistTestBase):
    def test_cancel_recovers_bed_and_makes_single_offer(self):
        occ, _, alloc = self.occupy("u_occ", "H02", "2026-09-23", "2026-09-24")
        _, _, e1 = self.waiter("u_o1")
        _, _, e2 = self.waiter("u_o2")
        # 满房：两人都在排队，空房数为 0
        self.assertEqual(self.system.availability("H02", "2026-09-23")["free_beds"], 0)

        self.system.cancel_booking(occ, alloc.id)
        # 只有第一人拿到唯一保留
        self.assertEqual(self.entry(e1.id).status, "offered")
        self.assertEqual(self.entry(e2.id).status, "queued")
        offer = self.entry(e1.id)
        self.assertEqual(offer.offer_hotel, "H02")
        self.assertEqual(offer.offer_bed, "301-01")
        self.assertEqual(offer.offer_nights, ["2026-09-23", "2026-09-24"])
        # 保留同样占住房态：空房仍为 0，且正式占房不能抢走保留床
        self.assertEqual(self.system.availability("H02", "2026-09-23")["free_beds"], 0)
        a3, m3 = applicant(self.system, "u_o3", "抢保留")
        app3 = self.system.submit_application(a3, m3)
        with self.assertRaises(RoomUnavailableError):
            self.system.confirm_booking(a3, app3.id, "H02",
                                        "2026-09-23", "2026-09-23")
        # 工作人员房态板可见保留来源
        board = self.system.room_board(self.verifier, "H02", "2026-09-23")
        row = next(r for r in board["beds"] if r["bed_key"] == "301-01")
        self.assertEqual(row["held_by_waitlist"], e1.id)
        self.assertIsNone(row["allocation_id"])

    def test_confirm_lands_booking_and_consumes_entitlement_once(self):
        occ, _, alloc = self.occupy("u_occ2", "H02", "2026-09-23", "2026-09-24")
        actor, _, entry = self.waiter("u_cf")
        self.system.cancel_booking(occ, alloc.id)
        token = self.entry(entry.id).offer_token

        result = self.system.confirm_waitlist(actor, entry.id, token=token)
        self.assertFalse(result["idempotent"])
        booking = result["allocation"]
        self.assertEqual(booking.created_from, "waitlist")
        self.assertEqual(booking.hotel_code, "H02")
        self.assertEqual(self.system.entitlement("u_cf")["used_nights"], 2)

        # 离线重复确认：幂等返回同一占房，不再次扣权益
        again = self.system.confirm_waitlist(actor, entry.id, token=token)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["allocation"].id, booking.id)
        self.assertEqual(self.system.entitlement("u_cf")["used_nights"], 2)
        # 持有的床位已转为正式占用，持有索引清空
        self.assertFalse(any(v == entry.id for v in self.system._held_offers.values()))
        self.assertEqual(
            self.system._bed_night[("H02", "301-01", "2026-09-23")], booking.id)

    def test_stale_token_rejected(self):
        occ, _, alloc = self.occupy("u_occ3", "H02", "2026-09-23", "2026-09-24")
        actor, _, entry = self.waiter("u_tok")
        self.system.cancel_booking(occ, alloc.id)
        with self.assertRaises(WaitlistConflictError):
            self.system.confirm_waitlist(actor, entry.id, token="offer_old")

    def test_offer_revoked_when_holder_books_elsewhere(self):
        # 保留期间申请人又在 H03 订了重叠日期：确认被拦、保留原子释放、
        # 条目转 blocked，床位立即晋位给下一位
        occ, _, alloc = self.occupy("u_occ3b", "H02", "2026-09-23", "2026-09-24")
        a1, _, e1 = self.waiter("u_ob1")
        a2, _, e2 = self.waiter("u_ob2")
        self.system.cancel_booking(occ, alloc.id)
        self.assertEqual(self.entry(e1.id).status, "offered")
        # 本人另订与候补重叠的占房
        self.system.confirm_booking(
            a1, self.system.applications[e1.app_id].id,
            "H03", "2026-09-24", "2026-09-25")
        with self.assertRaises(WaitlistConflictError) as ctx:
            self.system.confirm_waitlist(a1, e1.id)
        self.assertEqual(ctx.exception.context["reason"], "existing_stay")
        self.assertEqual(self.entry(e1.id).status, "blocked")
        self.assertEqual(self.entry(e1.id).blocked_reason, "existing_stay")
        self.assertEqual(self.entry(e2.id).status, "offered")
        self.assertEqual(self.entry(e2.id).offer_hotel, "H02")
        # 释放彻底：持有索引中已无甲
        self.assertFalse(any(v == e1.id for v in self.system._held_offers.values()))

    def test_offer_revoked_when_quota_runs_out(self):
        # 甲候补 28 夜（额度 30），保留期间另订 3 夜不重叠日期：剩余 27 < 28，
        # 确认转 blocked（quota），乙晋位
        fill = self.fill_hotel("H02", "2026-10-10", "2026-11-06")
        a1, _, e1 = self.waiter("u_q1", start="2026-10-10", end="2026-11-06")
        a2, _, e2 = self.waiter("u_q2", start="2026-10-10", end="2026-11-06")
        self.system.cancel_booking(fill[0][0], fill[0][1].id)
        self.assertEqual(self.entry(e1.id).status, "offered")
        self.system.confirm_booking(
            a1, self.system.applications[e1.app_id].id,
            "H03", "2026-11-10", "2026-11-12")  # 与候补不重叠，消耗 3 夜
        with self.assertRaises(QuotaError):
            self.system.confirm_waitlist(a1, e1.id)
        self.assertEqual(self.entry(e1.id).status, "blocked")
        self.assertEqual(self.entry(e1.id).blocked_reason, "quota")
        self.assertEqual(self.entry(e2.id).status, "offered")

    def test_decline_releases_and_promotes_next_atomically(self):
        occ, _, alloc = self.occupy("u_occ4", "H02", "2026-09-23", "2026-09-24")
        a1, _, e1 = self.waiter("u_d1")
        _, _, e2 = self.waiter("u_d2")
        self.system.cancel_booking(occ, alloc.id)
        self.system.decline_waitlist(a1, e1.id, "改去别的城市")
        self.assertEqual(self.entry(e1.id).status, "declined")
        self.assertEqual(self.entry(e2.id).status, "offered")
        self.assertEqual(self.entry(e2.id).offer_bed, "301-01")
        # 拒绝重复调用幂等
        again = self.system.decline_waitlist(a1, e1.id)
        self.assertTrue(again["idempotent"])

    def test_deadline_expiry_promotes_next(self):
        occ, _, alloc = self.occupy("u_occ5", "H02", "2026-09-23", "2026-09-24")
        _, _, e1 = self.waiter("u_x1")
        a2, _, e2 = self.waiter("u_x2")
        self.system.cancel_booking(occ, alloc.id)
        deadline = self.entry(e1.id).confirm_deadline
        self.assertEqual(deadline, "2026-09-22T09:30:00+08:00")  # 默认保留 30 分钟

        # 到点前不超时
        self.clock.advance(minutes=29)
        self.system.expire_waitlists(self.verifier)
        self.assertEqual(self.entry(e1.id).status, "offered")
        # 跨过截止点：保留原子释放，乙晋位
        self.clock.advance(minutes=2)
        scan = self.system.expire_waitlists(self.verifier)
        self.assertEqual(scan["expired"], [e1.id])
        self.assertEqual(self.entry(e1.id).status, "expired")
        self.assertEqual(self.entry(e2.id).status, "offered")
        # 乙确认成功
        r = self.system.confirm_waitlist(a2, e2.id)
        self.assertEqual(r["status"], "confirmed")

    def test_custom_respond_by_is_absolute_and_survives_clock_moves(self):
        # 23:50 登记，声明最晚次日 00:10 确认：跨日截止点
        self.clock.set(datetime(2026, 9, 22, 23, 50, tzinfo=CST))
        occ, _, alloc = self.occupy("u_occ6", "H02", "2026-09-23", "2026-09-24")
        actor, _, entry = self.waiter(
            "u_mid", respond_by="2026-09-23T00:10:00+08:00")
        self.system.cancel_booking(occ, alloc.id)
        self.assertEqual(self.entry(entry.id).confirm_deadline,
                         "2026-09-23T00:10:00+08:00")
        self.clock.set(datetime(2026, 9, 23, 0, 5, tzinfo=CST))
        self.system.expire_waitlists(self.verifier)
        self.assertEqual(self.entry(entry.id).status, "offered")
        # 自动化任务以显式 at 推进跨日截止点
        scan = self.system.expire_waitlists(
            self.verifier, at="2026-09-23T00:11:00+08:00")
        self.assertEqual(scan["expired"], [entry.id])
        self.assertEqual(self.entry(entry.id).status, "expired")

    def test_passed_respond_by_skips_offer_and_promotes_next(self):
        # 甲声明的最晚确认时间很早；房态在那之后才恢复：甲不出保留直接过期，乙晋位
        self.clock.set(datetime(2026, 9, 22, 8, 0, tzinfo=CST))
        fill = self.fill_hotel("H02", "2026-09-23", "2026-09-24")
        _, _, e1 = self.waiter(
            "u_rb1", respond_by="2026-09-22T08:30:00+08:00")
        _, _, e2 = self.waiter("u_rb2")
        self.clock.set(datetime(2026, 9, 22, 9, 0, tzinfo=CST))
        self.system.cancel_booking(fill[0][0], fill[0][1].id)
        self.assertEqual(self.entry(e1.id).status, "expired")
        self.assertIsNone(self.entry(e1.id).offer_hotel)
        self.assertEqual(self.entry(e1.id).last_skip_reason["code"],
                         "respond_by_passed")
        self.assertEqual(self.entry(e2.id).status, "offered")

    def test_window_pass_expires_unmatched_wait(self):
        # 无人释放床位，候补窗口整体成为过去：条目过期
        self.occupy("u_occ7", "H02", "2026-09-23", "2026-09-24")
        _, _, entry = self.waiter("u_past", start="2026-09-23", end="2026-09-24")
        self.clock.set(datetime(2026, 9, 26, 8, 0, tzinfo=CST))
        scan = self.system.expire_waitlists(self.verifier)
        self.assertIn(entry.id, scan["expired"])
        self.assertEqual(self.entry(entry.id).status, "expired")

    def test_concurrent_cancel_calls_promote_exactly_one(self):
        # 5 个线程同时退掉同一张床：恰好第一位候补拿到保留，不多占
        occ, _, alloc = self.occupy("u_occ8", "H02", "2026-09-23", "2026-09-24")
        waiters = [self.waiter(f"u_cc{i}") for i in range(3)]
        errors = []
        barrier = threading.Barrier(5)

        def cancel():
            barrier.wait()
            try:
                self.system.cancel_booking(occ, alloc.id)
            except Exception as exc:  # 重复退订可能抛错，但不得破坏状态
                errors.append(exc)

        threads = [threading.Thread(target=cancel) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        offered = [e for _, _, e in waiters
                   if self.entry(e.id).status == "offered"]
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0].id, waiters[0][2].id)  # 晋位顺序不变
        # 恰好持有 2 个床位夜
        holds = [k for k, v in self.system._held_offers.items()
                 if v == offered[0].id]
        self.assertEqual(len(holds), 2)

    def test_concurrent_distinct_cancellations_promote_in_rank_order(self):
        # H01 三张床占满；两张床同时退订：两位候补各自拿到保留，床位不重复
        fill = self.fill_hotel("H01", "2026-10-01", "2026-10-02")
        cancelled_beds = {fill[0][1].bed_key, fill[1][1].bed_key}
        w1 = self.waiter("u_w_p1", start="2026-10-01", end="2026-10-02",
                         hotels=["H01"])[2]
        w2 = self.waiter("u_w_p2", start="2026-10-01", end="2026-10-02",
                         hotels=["H01"])[2]
        barrier = threading.Barrier(2)

        def cancel(pair):
            barrier.wait()
            self.system.cancel_booking(pair[0], pair[1].id)

        threads = [threading.Thread(target=cancel, args=(fill[0],)),
                   threading.Thread(target=cancel, args=(fill[1],))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(self.entry(w1.id).status, "offered")
        self.assertEqual(self.entry(w2.id).status, "offered")
        self.assertNotEqual(self.entry(w1.id).offer_bed,
                            self.entry(w2.id).offer_bed)
        held_beds = {self.entry(w.id).offer_bed for w in (w1, w2)}
        self.assertEqual(held_beds, cancelled_beds)


class ViewPermissionTest(WaitlistTestBase):
    def test_staff_sees_explainable_queue(self):
        self.occupy("u_v0", "H02", "2026-09-23", "2026-09-24")
        _, _, e1 = self.waiter("u_v1")
        _, _, e2 = self.waiter("u_v2", urgency="recruit")
        view = self.system.waitlist_queue_view(self.duty)
        self.assertEqual([q["id"] for q in view["queue"]], [e2.id, e1.id])
        top = view["queue"][0]
        self.assertEqual(top["applicant_name"], "u_v2")
        self.assertEqual(top["urgency"], "recruit")
        self.assertIn("rank_reasons", top)
        self.assertEqual(top["queue_position"], 1)

    def test_applicant_sees_only_own(self):
        self.occupy("u_v3", "H02", "2026-09-23", "2026-09-24")
        a1, _, e1 = self.waiter("u_v4")
        self.waiter("u_v5")
        mine = self.system.waitlist_mine(a1)
        self.assertEqual([q["id"] for q in mine["queue"]], [e1.id])
        # 不能查看他人候补
        other = next(q for q in self.system.waitlist_queue_view(self.verifier)["queue"]
                     if q["id"] != e1.id)
        with self.assertRaises(PermissionError):
            self.system.get_waitlist(a1, other["id"])

    def test_front_desk_scoped_to_own_hotel(self):
        self.waiter("u_v6", hotels=["H01"])
        self.waiter("u_v7", hotels=["H02"])
        h01 = self.system.waitlist_queue_view(self.front1)["queue"]
        hotels = {h for q in h01 for h in q["preferred_hotels"]}
        self.assertEqual(hotels, {"H01"})

    def test_applicant_cannot_open_staff_queue(self):
        actor, _ = applicant(self.system, "u_v8", "无权")
        with self.assertRaises(PermissionError):
            self.system.waitlist_queue_view(actor)

    def test_cancel_by_applicant_releases_offer(self):
        occ, _, alloc = self.occupy("u_v9", "H02", "2026-09-23", "2026-09-24")
        a1, _, e1 = self.waiter("u_v10")
        a2, _, e2 = self.waiter("u_v11")
        self.system.cancel_booking(occ, alloc.id)
        self.system.cancel_waitlist(a1, e1.id, "不要了")
        self.assertEqual(self.entry(e1.id).status, "cancelled")
        self.assertEqual(self.entry(e2.id).status, "offered")
        # 终态不能再撤回
        with self.assertRaises(WaitlistConflictError):
            self.system.cancel_waitlist(a1, e1.id)


class RestartPersistenceTest(WaitlistTestBase):
    def _clone_system(self, clock):
        from tests.world import build_world
        world = build_world(clock)
        new_system = world["system"]
        new_system.restore_state(self.system.snapshot_state())
        return world, new_system

    def test_rank_order_and_hold_deadline_survive_restart(self):
        from tests.world import build_world, make_clock
        occ, _, alloc = self.occupy("u_r0", "H02", "2026-09-23", "2026-09-24")
        # 甲当日抵城（紧急）先于普通的乙晋位
        _, _, e1 = self.waiter("u_r1", urgency="arriving_today")
        _, _, e2 = self.waiter("u_r2")
        self.system.cancel_booking(occ, alloc.id)
        assert self.entry(e1.id).status == "offered"
        offered_deadline = self.entry(e1.id).confirm_deadline
        offered_token = self.entry(e1.id).offer_token

        # 重启：注入一个停在截止点之前的新时钟
        clock2 = make_clock(datetime(2026, 9, 22, 9, 20, tzinfo=CST))
        _, s2 = self._clone_system(clock2)
        queue = s2.waitlist_queue_view(s2.users["u_verifier"])["queue"]
        ids = [q["id"] for q in queue]
        self.assertEqual(ids, [e1.id, e2.id])          # 晋位顺序不变
        first = s2.waitlist[e1.id]
        self.assertEqual(first.status, "offered")
        self.assertEqual(first.confirm_deadline, offered_deadline)  # 期限不变
        self.assertEqual(first.offer_token, offered_token)
        self.assertEqual(first.offer_nights, ["2026-09-23", "2026-09-24"])
        # 持有索引恢复：空房仍为 0
        self.assertEqual(s2.availability("H02", "2026-09-23")["free_beds"], 0)
        # 申请顺序计数器延续
        actor, material = applicant(s2, "u_r3", "重启后来者")
        app = s2.submit_application(actor, material)
        e3 = s2.register_waitlist(actor, app.id, "2026-09-23", "2026-09-24", ["H02"])
        self.assertEqual(e3.register_seq, e2.register_seq + 1)

        # 在恢复后的系统上跨过截止点：超时与晋位照常
        clock2.set(datetime(2026, 9, 22, 9, 31, tzinfo=CST))
        scan = s2.expire_waitlists(s2.users["u_verifier"])
        self.assertEqual(scan["expired"], [e1.id])
        self.assertEqual(s2.waitlist[e2.id].status, "offered")
        self.assertEqual(s2.waitlist[e2.id].confirm_deadline,
                         "2026-09-22T10:01:00+08:00")

        # 再重启一次：乙仍在保留、期限仍是同一个绝对时刻
        clock3 = make_clock(datetime(2026, 9, 22, 9, 40, tzinfo=CST))
        s3 = build_world(clock3)["system"]
        s3.restore_state(s2.snapshot_state())
        self.assertEqual(s3.waitlist[e2.id].status, "offered")
        self.assertEqual(s3.waitlist[e2.id].confirm_deadline,
                         "2026-09-22T10:01:00+08:00")
        # 乙确认落地，权益只扣 2 夜
        r = s3.confirm_waitlist(s3.users["u_r2"], e2.id)
        self.assertEqual(r["status"], "confirmed")
        self.assertEqual(s3.entitlement("u_r2")["used_nights"], 2)

    def test_queued_order_survives_restart_under_concurrent_cancel(self):
        # 重启后制造并发退订：晋位顺序仍按固化 rank_key
        from tests.world import make_clock
        o1, _, a1 = self.occupy("u_s1", "H01", "2026-11-01", "2026-11-02")
        o2, _, a2 = self.occupy("u_s2", "H01", "2026-11-01", "2026-11-02")
        w1 = self.waiter("u_sw1", start="2026-11-01", end="2026-11-02",
                         hotels=["H01"])[2]
        w2 = self.waiter("u_sw2", start="2026-11-01", end="2026-11-02",
                         hotels=["H01"], urgency="arriving_today")[2]
        data = self.system.snapshot_state()

        clock2 = make_clock(NOW)
        from tests.world import build_world
        s2 = build_world(clock2)["system"]
        s2.restore_state(data)
        barrier = threading.Barrier(2)

        def cancel(uid, alloc_id):
            barrier.wait()
            s2.cancel_booking(s2.users[uid], alloc_id)

        threads = [threading.Thread(target=cancel, args=("u_s1", a1.id)),
                   threading.Thread(target=cancel, args=("u_s2", a2.id))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 紧急的 w2 先选床、普通的 w1 后选，两张床都被保留且不撞床
        self.assertEqual(s2.waitlist[w2.id].status, "offered")
        self.assertEqual(s2.waitlist[w1.id].status, "offered")
        self.assertNotEqual(s2.waitlist[w1.id].offer_bed,
                            s2.waitlist[w2.id].offer_bed)
