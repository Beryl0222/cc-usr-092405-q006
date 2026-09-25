"""候补队列领域测试：可解释排序、晋位、确认/超时/拒绝的原子释放、
离线重复确认幂等、冲突闸门，以及跨日截止点 + 重启后的顺序与保留期限不变。"""

import threading
from datetime import datetime

from station.models import Actor
from station.persistence import restore_system, snapshot_system
from station.catalog import HotelDirectory
from station.system import LodgingSystem
from station.timeutil import CST
from tests.world import WorldTest, applicant


def fill_hotel(system, actor_uid, hotel, start, end):
    actor, material = applicant(system, actor_uid, actor_uid)
    app = system.submit_application(actor, material)
    alloc = system.confirm_booking(actor, app.id, hotel, start, end)
    return actor, app, alloc


# 一个足够远的"最晚确认时间"，使 offer 的实际保留期仅由系统保留时长决定
LATE_DEADLINE = "2026-10-30T00:00:00+08:00"


class WaitlistRegistrationTest(WorldTest):
    def test_requires_eligible_application(self):
        actor = Actor("u_bad", "applicant", "不合格")
        self.system.register_user(actor)
        # 学历不满足且不可申诉 → denied
        from station.errors import PolicyError
        material = dict(applicant(self.system, "u_bad", "不合格")[1])
        material["degree"] = "中专/高中"
        with self.assertRaises(PolicyError):
            self.system.submit_application(actor, material)
        # denied 申请不能候补
        app = next(iter(self.system.applications.values()))
        from station.errors import WaitlistConflictError
        with self.assertRaises(WaitlistConflictError):
            self.system.register_waitlist(
                actor, app.id, "2026-09-23", "2026-09-25", ["H01"])

    def test_validates_dates_hotels_and_deadline(self):
        actor, app, _ = self.apply(uid="u_v", name="V")
        from station.errors import ValidationError, NotFoundError
        with self.assertRaises(ValidationError):
            self.system.register_waitlist(actor, app.id, "2026-09-25",
                                          "2026-09-23", ["H01"])
        with self.assertRaises(ValidationError):
            self.system.register_waitlist(actor, app.id, "2026-09-23",
                                          "2026-09-25", [])
        with self.assertRaises(NotFoundError):
            self.system.register_waitlist(actor, app.id, "2026-09-23",
                                          "2026-09-25", ["HXX"])
        with self.assertRaises(ValidationError):
            self.system.register_waitlist(
                actor, app.id, "2026-09-23", "2026-09-25", ["H01"],
                latest_confirm_at="2026-09-20T09:00:00+08:00")

    def test_conflicts_with_existing_lodging(self):
        actor, app, alloc = self.apply(uid="u_have", name="有房")
        self.book(actor, app.id, hotel="H01", start="2026-09-23", end="2026-09-25")
        from station.errors import WaitlistConflictError
        with self.assertRaises(WaitlistConflictError):
            self.system.register_waitlist(
                actor, app.id, "2026-09-24", "2026-09-26", ["H02"])

    def test_conflicts_with_overlapping_waitlist(self):
        actor, app, _ = self.apply(uid="u_dup", name="重复")
        self.system.register_waitlist(
            actor, app.id, "2026-10-01", "2026-10-05", ["H01"])
        from station.errors import WaitlistConflictError
        with self.assertRaises(WaitlistConflictError):
            # 日期部分重叠的第二条活跃候补不允许，防止重复占补贴天数
            self.system.register_waitlist(
                actor, app.id, "2026-10-04", "2026-10-08", ["H02"])

    def test_quota_checked_at_registration(self):
        actor, app, _ = self.apply(uid="u_quota", name="额度")
        from station.errors import QuotaError
        with self.assertRaises(QuotaError):
            self.system.register_waitlist(
                actor, app.id, "2026-10-01", "2026-11-15", ["H01"])  # >30 夜


class WaitlistRankingTest(WorldTest):
    def _register(self, uid, start, end, hotels=("H02",)):
        actor, app, _ = self.apply(uid=uid, name=uid)
        entry = self.system.register_waitlist(
            actor, app.id, start, end, list(hotels), hold_minutes=300)
        return actor, app, entry

    def test_urgency_outranks_earlier_seq(self):
        # 先登记一个常规（4 天后入住），再登记一个紧急（明天入住）
        _, _, normal = self._register("u_normal", "2026-09-26", "2026-09-28")
        _, _, urgent = self._register("u_urgent", "2026-09-23", "2026-09-24")
        queue = self.system.waitlist_queue(self.verifier)
        ids = [e["id"] for e in queue["entries"]]
        self.assertEqual(ids[0], urgent.id)
        self.assertEqual(ids[1], normal.id)
        head = queue["entries"][0]
        self.assertEqual(head["rank_basis"]["urgency"], "urgent")
        self.assertEqual(head["position"], 1)
        self.assertEqual(queue["order_rule"],
                         ["urgency_score desc", "decided_on asc", "seq asc"])

    def test_same_urgency_fifo_by_seq(self):
        _, _, e1 = self._register("u_one", "2026-09-23", "2026-09-24")
        _, _, e2 = self._register("u_two", "2026-09-23", "2026-09-24")
        queue = self.system.waitlist_queue(self.verifier)
        self.assertEqual([e["id"] for e in queue["entries"]], [e1.id, e2.id])
        self.assertLess(e1.rank_basis["seq"], e2.rank_basis["seq"])


class WaitlistAdvanceTest(WorldTest):
    def _two_waiters_on_full_h02(self):
        occ_actor, occ_app, occ_alloc = fill_hotel(
            self.system, "u_occ", "H02", "2026-09-23", "2026-09-25")
        a1, app1, _ = self.apply(uid="u_a", name="甲")
        e1 = self.system.register_waitlist(
            a1, app1.id, "2026-09-23", "2026-09-25", ["H02"], latest_confirm_at=LATE_DEADLINE)
        a2, app2, _ = self.apply(uid="u_b", name="乙")
        e2 = self.system.register_waitlist(
            a2, app2.id, "2026-09-23", "2026-09-25", ["H02"], latest_confirm_at=LATE_DEADLINE)
        return occ_actor, occ_alloc, (a1, e1), (a2, e2)

    def test_release_offers_one_provisional_match(self):
        occ, occ_alloc, (a1, e1), (a2, e2) = self._two_waiters_on_full_h02()
        # 释放前无房可匹配
        self.assertEqual(self.system.advance_waitlist(self.verifier)["offers"], [])
        # 退订 → 房态恢复，自动晋位恰好一位
        self.system.withdraw_dates(
            occ, occ_alloc.id, ["2026-09-23", "2026-09-24", "2026-09-25"])
        self.assertEqual(e1.status, "offered")
        self.assertEqual(e2.status, "waiting")  # 只保留一个方案
        provisional = self.system.allocations[e1.allocation_id]
        self.assertTrue(provisional.provisional)
        # 暂时性占房已经进入唯一房态索引，第二名订不到该床
        board = self.system.room_board(self.verifier, "H02", "2026-09-23")
        occupied = [b for b in board["beds"] if b["allocation_id"]]
        self.assertEqual(len(occupied), 1)
        self.assertEqual(occupied[0]["allocation_id"], provisional.id)

    def test_confirm_flips_provisional_and_is_idempotent(self):
        occ, occ_alloc, (a1, e1), _ = self._two_waiters_on_full_h02()
        self.system.cancel_booking(occ, occ_alloc.id)
        result = self.system.confirm_waitlist_offer(
            a1, entry_id=e1.id, request_id="client-token-1")
        self.assertFalse(result["idempotent"])
        alloc = result["allocation"]
        self.assertFalse(alloc.provisional)
        self.assertEqual(self.system.entitlement(a1.id)["used_nights"], 3)
        # 离线重复确认（同一 request_id）：返回同一方案，不二次扣权益
        again = self.system.confirm_waitlist_offer(
            a1, entry_id=e1.id, request_id="client-token-1")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["allocation"].id, alloc.id)
        self.assertEqual(self.system.entitlement(a1.id)["used_nights"], 3)
        # 无令牌的重复确认同样幂等
        again2 = self.system.confirm_waitlist_offer(a1, entry_id=e1.id)
        self.assertTrue(again2["idempotent"])
        # 正式占房现在可入住
        self.system.ingest_event(self.front2, {
            "event_id": "ci1", "source": "front_desk", "event_type": "check_in",
            "hotel_code": "H02", "bed_key": alloc.bed_key,
            "occurred_at": "2026-09-23T14:00:00+08:00"})

    def test_provisional_blocks_events_and_settlement(self):
        occ, occ_alloc, (a1, e1), _ = self._two_waiters_on_full_h02()
        self.system.cancel_booking(occ, occ_alloc.id)
        res = self.system.ingest_event(self.front2, {
            "event_id": "ci_early", "source": "front_desk", "event_type": "check_in",
            "hotel_code": "H02", "bed_key": self.system.allocations[e1.allocation_id].bed_key,
            "occurred_at": "2026-09-23T14:00:00+08:00"})
        self.assertEqual(res["status"], "pending_review")
        from station.errors import WaitlistStateError
        with self.assertRaises(WaitlistStateError):
            self.system.settle(self.finance, e1.allocation_id)

    def test_expire_releases_and_advances_next(self):
        occ, occ_alloc, (a1, e1), (a2, e2) = self._two_waiters_on_full_h02()
        self.system.cancel_booking(occ, occ_alloc.id)
        first_deadline = e1.offer_expires_at
        # 跨日推进超过保留期限
        self.clock.advance(days=1)
        out = self.system.expire_waitlist(self.verifier)
        self.assertEqual([o["entry_id"] for o in out["expired"]], [e1.id])
        self.assertEqual(e1.status, "expired")
        # 床位原子释放并顺延第二位；新方案从释放时刻重新计时（+保留时长）
        self.assertEqual(e2.status, "offered")
        from station.timeutil import parse_dt
        from datetime import timedelta
        self.assertEqual(parse_dt(e2.offer_expires_at),
                         self.clock() + timedelta(minutes=120))
        # 第一位的占房已释放（旧 allocation 不再占床），同床由第二位原子接手
        rel = self.system.allocations[e1.allocation_id]
        self.assertTrue(all(l.state == "released" for l in rel.nights.values()))
        self.assertNotEqual(
            self.system._bed_night.get(("H02", rel.bed_key, "2026-09-23")),
            rel.id)
        self.assertEqual(
            self.system._bed_night.get(("H02", rel.bed_key, "2026-09-23")),
            e2.allocation_id)
        # 第一位保留期限是绝对时刻，与第二位新期限不同
        self.assertNotEqual(first_deadline, e2.offer_expires_at)

    def test_reject_releases_and_advances_next(self):
        occ, occ_alloc, (a1, e1), (a2, e2) = self._two_waiters_on_full_h02()
        self.system.cancel_booking(occ, occ_alloc.id)
        out = self.system.reject_waitlist_offer(a1, entry_id=e1.id, note="不去了")
        self.assertEqual(e1.status, "rejected")
        self.assertEqual(e2.status, "offered")
        self.assertEqual(len(out["advanced"]["offers"]), 1)

    def test_cancel_waiting_entry(self):
        occ, occ_alloc, (a1, e1), (a2, e2) = self._two_waiters_on_full_h02()
        self.system.cancel_waitlist(a1, e1.id)
        self.assertEqual(e1.status, "cancelled")
        # 释放未发生（本来满房），第二位仍 waiting
        self.assertEqual(e2.status, "waiting")
        self.system.cancel_booking(occ, occ_alloc.id)
        self.assertEqual(e2.status, "offered")

    def test_late_confirm_after_expiry_rejected(self):
        occ, occ_alloc, (a1, e1), _ = self._two_waiters_on_full_h02()
        self.system.cancel_booking(occ, occ_alloc.id)
        self.clock.advance(days=1)
        from station.errors import WaitlistStateError
        with self.assertRaises(WaitlistStateError):
            # 懒超时：确认到达时已过保留期，床位已顺延，不能再确认
            self.system.confirm_waitlist_offer(a1, entry_id=e1.id)


class WaitlistConcurrencyTest(WorldTest):
    def test_concurrent_cancellations_advance_in_fixed_order(self):
        # H01 共 3 床，占满，登记 3 名候补，并发退订 2 间 → 恰好前两位晋位
        occ_allocs = []
        for i in range(3):
            _, _, alloc = fill_hotel(
                self.system, f"u_occ{i}", "H01", "2026-09-23", "2026-09-25")
            occ_allocs.append(alloc)
        waiters = []
        for i in range(3):
            actor, app, _ = self.apply(uid=f"u_w{i}", name=f"候补{i}")
            entry = self.system.register_waitlist(
                actor, app.id, "2026-09-23", "2026-09-25", ["H01"],
                latest_confirm_at=LATE_DEADLINE)
            waiters.append((actor, entry))

        barrier = threading.Barrier(2)

        def cancel(alloc):
            barrier.wait()
            self.system.cancel_booking(
                self.system.users[f"u_occ{occ_allocs.index(alloc)}"], alloc.id)

        t1 = threading.Thread(target=cancel, args=(occ_allocs[0],))
        t2 = threading.Thread(target=cancel, args=(occ_allocs[1],))
        t1.start(); t2.start(); t1.join(); t2.join()

        offered = [e for _, e in waiters if e.status == "offered"]
        waiting = [e for _, e in waiters if e.status == "waiting"]
        self.assertEqual(len(offered), 2)
        self.assertEqual(len(waiting), 1)
        # 晋位顺序严格等于队列顺序（前两位）
        expected = [waiters[0][1].id, waiters[1][1].id]
        self.assertEqual(sorted(e.id for e in offered), sorted(expected))
        # 唯一房态：两张释放的床各被一个不同候补占据，无重复
        owners = {self.system._bed_night.get(("H01", self.system.allocations[e.allocation_id].bed_key, "2026-09-23"))
                  for e in offered}
        self.assertEqual(len(owners), 2)


class WaitlistViewTest(WorldTest):
    def test_applicant_sees_own_position_and_countdown(self):
        _, occ_alloc = None, None
        occ_actor, _, occ_alloc = fill_hotel(
            self.system, "u_occv", "H02", "2026-09-23", "2026-09-25")
        a1, app1, _ = self.apply(uid="u_v1", name="甲")
        e1 = self.system.register_waitlist(
            a1, app1.id, "2026-09-23", "2026-09-25", ["H02"], latest_confirm_at=LATE_DEADLINE)
        view = self.system.applicant_waitlist(a1)
        self.assertEqual(view["entries"][0]["position"], 1)
        self.assertIsNone(view["entries"][0]["offer"])
        # 工作人员视图含解释字段
        staff = self.system.waitlist_queue(self.verifier)
        self.assertEqual(staff["entries"][0]["rank_basis"]["urgency"], "urgent")
        # 非本人不能查看他人候补
        a_other, _ = applicant(self.system, "u_stranger", "路人")
        from station.errors import PermissionError
        with self.assertRaises(PermissionError):
            self.system.applicant_waitlist(a_other, applicant_id="u_v1")


class WaitlistRestartTest(WorldTest):
    def _fresh_system(self):
        system = LodgingSystem(self.policies, HotelDirectory(), self.rates,
                               self.clock)
        # 复用同一目录实例（同床实体）
        system.directory = self.directory
        return system

    def test_order_deadline_and_idempotency_survive_restart(self):
        occ_actor, _, occ_alloc = fill_hotel(
            self.system, "u_ocr", "H02", "2026-09-23", "2026-09-25")
        a1, app1, _ = self.apply(uid="u_r1", name="R1")
        e1 = self.system.register_waitlist(
            a1, app1.id, "2026-09-23", "2026-09-25", ["H02"], latest_confirm_at=LATE_DEADLINE)
        a2, app2, _ = self.apply(uid="u_r2", name="R2")
        e2 = self.system.register_waitlist(
            a2, app2.id, "2026-09-23", "2026-09-25", ["H02"], latest_confirm_at=LATE_DEADLINE)
        self.system.cancel_booking(occ_actor, occ_alloc.id)  # e1 晋位
        deadline_before = e1.offer_expires_at
        order_before = [e["id"] for e in
                        self.system.waitlist_queue(self.verifier)["entries"]]

        # 推进到跨日截止点之前，拍快照并"重启"到新系统
        self.clock.advance(hours=1)
        data = snapshot_system(self.system)
        fresh = self._fresh_system()
        restore_system(fresh, data)

        # 晋位顺序与保留期限（绝对时刻）保持不变
        order_after = [e["id"] for e in
                       fresh.waitlist_queue(fresh.users["u_verifier"])["entries"]]
        self.assertEqual(order_after, order_before)
        restored_offer = fresh.offers[e1.offer_id]
        self.assertEqual(restored_offer.expires_at, deadline_before)
        self.assertEqual(restored_offer.status, "open")
        # 唯一索引重建：暂时性占房仍占着该床
        prov = fresh.allocations[e1.allocation_id]
        self.assertTrue(prov.provisional)
        self.assertEqual(fresh._bed_night[("H02", prov.bed_key, "2026-09-23")],
                         prov.id)

        # 离线重复确认：重启后用同一 request_id 首确认 → 只扣一次权益
        res = fresh.confirm_waitlist_offer(
            fresh.users["u_r1"], entry_id=e1.id, request_id="net-token")
        self.assertFalse(res["idempotent"])
        again = fresh.confirm_waitlist_offer(
            fresh.users["u_r1"], entry_id=e1.id, request_id="net-token")
        self.assertTrue(again["idempotent"])
        self.assertEqual(fresh.entitlement("u_r1")["used_nights"], 3)

        # 再重启：已消费令牌不丢，重复确认仍然幂等
        data2 = snapshot_system(fresh)
        fresh2 = self._fresh_system()
        restore_system(fresh2, data2)
        again2 = fresh2.confirm_waitlist_offer(
            fresh2.users["u_r1"], entry_id=e1.id, request_id="net-token")
        self.assertTrue(again2["idempotent"])
        self.assertEqual(fresh2.entitlement("u_r1")["used_nights"], 3)
        # 第二位仍 waiting（确认并未释放床位）
        self.assertEqual(fresh2.waitlist[e2.id].status, "waiting")

    def test_offer_expires_after_crossday_restart(self):
        occ_actor, _, occ_alloc = fill_hotel(
            self.system, "u_occx", "H02", "2026-09-23", "2026-09-25")
        a1, app1, _ = self.apply(uid="u_x1", name="X1")
        e1 = self.system.register_waitlist(
            a1, app1.id, "2026-09-23", "2026-09-25", ["H02"], latest_confirm_at=LATE_DEADLINE)
        self.system.cancel_booking(occ_actor, occ_alloc.id)
        deadline = e1.offer_expires_at

        # 跨日重启：时钟越过截止点
        self.clock.set(datetime(2026, 9, 23, 12, 0, tzinfo=CST))
        data = snapshot_system(self.system)
        fresh = self._fresh_system()
        restore_system(fresh, data)
        self.assertTrue(self.clock() > datetime.fromisoformat(deadline))
        out = fresh.expire_waitlist()
        self.assertEqual(len(out["expired"]), 1)
        self.assertEqual(fresh.waitlist[e1.id].status, "expired")
