"""候补队列：可解释排序、唯一暂时保留、原子释放推进。

候补是"占房之前"的流程，绝不提前扣减住宿权益：只有申请人确认保留方案时才在
同一把锁内落地正式占房并占用唯一房态索引。核心规则：

* 晋位顺序在登记时固化为 rank_key：资格决定时间（越早越优先）→ 紧急程度 →
  全局申请顺序；排序依据随条目输出（可解释队列）。
* 房态恢复（退订、撤回、拒绝、超时、裁决释放）后按 rank_key 顺序扫描，
  每位申请人至多得到一个暂时保留方案；保留写入独立的按夜持有索引，与正式
  占房共用 (酒店, 床位, 夜) 的排他性，但不消耗补贴天数。
* 确认、超时、拒绝、撤回全部原子释放持有夜并立即推动下一位；离线重复确认
  幂等返回同一占房，不会多扣权益。
* 候补全程与既有占房、争议冻结夜、未裁决复核工单互斥：登记时拦截，排队中
  新出现的冲突把条目转入 blocked，冲突消除后自动回到队尾原位（rank_key 不变）。
* 保留截止时间是登记时确定的绝对时刻并随条目持久化，跨日扫描只负责让
  "已到点"的保留失效，不重算期限。
"""

from dataclasses import asdict
from datetime import timedelta

from .catalog import BED_BLOCKED
from .errors import (BookingConflictError, NotFoundError, PermissionError,
                     QuotaError, ValidationError, WaitlistConflictError)
from .models import *
from .timeutil import daterange, iso, parse_date, parse_dt

# 申请人未声明最晚确认时间时，保留方案的默认有效时长
DEFAULT_OFFER_HOLD_MINUTES = 30

BLOCK_EXISTING_STAY = "existing_stay"
BLOCK_FROZEN = "frozen_night"
BLOCK_OPEN_TICKET = "open_ticket"
BLOCK_QUOTA = "quota"


class WaitlistMixin:
    # ------------------------------------------------------------- 登记

    def register_waitlist(self, actor, app_id, start, end, preferred_hotels,
                          urgency=URGENCY_NORMAL, respond_by=None):
        """为一段连续日期登记候补；排序键在登记瞬间固化。"""
        with self.lock:
            app = self._load_app(app_id)
            self._require_role(actor, "applicant", "verifier", "duty_manager")
            if actor.role == "applicant" and actor.id != app.applicant_id:
                raise PermissionError("只能为本人登记候补")
            if app.status != APP_ELIGIBLE:
                raise ValidationError("申请尚未通过资格核验，不能登记候补",
                                      status=app.status)

            start_d, end_d = parse_date(start), parse_date(end)
            if end_d < start_d:
                raise ValidationError("结束日期早于开始日期")
            if start_d < self._now().date():
                raise ValidationError("不能为已过去的日期候补")
            dates = list(daterange(start_d, end_d))
            if not preferred_hotels:
                raise ValidationError("至少选择一家可接受酒店")
            hotels = []
            for code in preferred_hotels:
                hotel = self.directory.get(code)  # 不存在直接 NOT_FOUND
                if code not in hotels:
                    hotels.append(code)
            if urgency not in URGENCY_ORDER:
                raise ValidationError("未知紧急程度", urgency=urgency,
                                      allowed=sorted(URGENCY_ORDER))
            respond_dt = None
            if respond_by is not None:
                respond_dt = parse_dt(respond_by)
                if respond_dt <= self._now():
                    raise ValidationError("最晚确认时间必须晚于当前时间")

            # 既有住宿 / 冻结夜 / 未裁决工单 / 权益余额：候补不得越过任何一条
            self._assert_waitlist_clear(app.applicant_id, dates)

            # 同一申请人不得有日期重叠的有效候补（含暂时保留）
            dup = self._overlapping_active_wait(app.applicant_id, dates)
            if dup is not None:
                raise WaitlistConflictError(
                    "已存在日期重叠的有效候补，请勿重复登记",
                    waitlist_id=dup.id, status=dup.status)

            self._waitlist_seq += 1
            decided_on = (app.eligibility or {}).get("decided_on") or app.submitted_on
            now = self._now()
            entry = WaitlistEntry(
                id=new_id("wait"),
                app_id=app.id, applicant_id=app.applicant_id,
                start=iso(start_d), end=iso(end_d),
                preferred_hotels=list(hotels),
                register_seq=self._waitlist_seq,
                decided_on=decided_on, urgency=urgency,
                created_at=now.isoformat(), updated_at=now.isoformat())
            entry.rank_key = [decided_on, -URGENCY_ORDER[urgency], self._waitlist_seq]
            entry.rank_reasons = {
                "rule": "资格决定时间升序 → 紧急程度降序 → 申请顺序升序",
                "decided_on": decided_on,
                "urgency": urgency,
                "urgency_weight": URGENCY_ORDER[urgency],
                "register_seq": self._waitlist_seq,
                "respond_by": respond_dt.isoformat() if respond_dt else None,
            }
            entry.history.append({"at": now.isoformat(), "from": None,
                                  "to": WAIT_QUEUED, "reason": "registered"})
            self.waitlist[entry.id] = entry
            self._pump_waitlist()
            return entry

    # ------------------------------------------------------------- 申请人动作

    def confirm_waitlist(self, actor, entry_id, token=None):
        """确认暂时保留方案：原子落地正式占房并推动下一位。

        重复确认（断网补传）幂等：已确认则原样返回同一占房，不再次扣减权益。
        """
        with self.lock:
            self._expire_due_offers()
            entry = self._load_wait(entry_id)
            if actor.role == "applicant" and actor.id != entry.applicant_id:
                raise PermissionError("只能确认本人的候补")
            self._require_role(actor, "applicant", "duty_manager")

            if entry.status == WAIT_CONFIRMED:
                alloc = self.allocations.get(entry.allocation_id)
                return {"waitlist_id": entry.id, "idempotent": True,
                        "status": WAIT_CONFIRMED, "allocation": alloc}
            if entry.status != WAIT_OFFERED:
                raise WaitlistConflictError("候补当前没有待确认的保留方案",
                                            waitlist_id=entry.id, status=entry.status)
            if token is not None and token != entry.offer_token:
                raise WaitlistConflictError("保留方案已变更，令牌失效",
                                            waitlist_id=entry.id,
                                            sent_token=token,
                                            current_token=entry.offer_token)
            # 二次校验：排队期间状态可能变化（本人另订、争议冻结、工单、额度）
            dates = self._wait_dates(entry)
            try:
                self._assert_waitlist_clear(entry.applicant_id, dates,
                                            ignore_entry=entry.id)
            except (WaitlistConflictError, QuotaError) as exc:
                reason = (BLOCK_QUOTA if isinstance(exc, QuotaError)
                          else exc.context.get("reason", BLOCK_EXISTING_STAY))
                self._release_offer(entry, reason)
                entry.status = WAIT_BLOCKED
                entry.blocked_reason = reason
                entry.blocked_detail = (
                    {"reason": reason, **exc.context} if isinstance(exc, QuotaError)
                    else exc.context)
                self._pump_waitlist()
                raise

            hotel = self.directory.get(entry.offer_hotel)
            pol = self.policies.get(self._load_app(entry.app_id).policy_code)
            idx_snapshot = dict(self._bed_night)
            person_snapshot = dict(self._person_night)
            hold_keys = [(entry.offer_hotel, entry.offer_bed, d)
                         for d in entry.offer_nights]
            try:
                alloc = Allocation(
                    id=new_id("alloc"), app_id=entry.app_id,
                    applicant_id=entry.applicant_id,
                    hotel_code=entry.offer_hotel, bed_key=entry.offer_bed,
                    start=entry.start, end=entry.end, planned_end=entry.end,
                    policy_code=pol.code, chain_id=new_id("chain"),
                    created_from="waitlist", created_at=self._now().isoformat())
                for d in dates:
                    day = iso(d)
                    # 持有索引让位给正式占用索引：先删持有、再写正式占用
                    held_by = self._held_offers.pop(
                        (entry.offer_hotel, entry.offer_bed, day), None)
                    if held_by not in (None, entry.id):
                        raise WaitlistConflictError("保留床位已被他人占用", date=day)
                    if (alloc.hotel_code, alloc.bed_key, day) in self._bed_night:
                        raise WaitlistConflictError("床位夜已被占用", date=day)
                    if self._person_night.get((entry.applicant_id, day)) is not None:
                        raise BookingConflictError("申请人当夜已有占房", date=day)
                    alloc.nights[day] = NightLine(
                        date=day, state=NIGHT_HELD,
                        rate_locked=self.rates.rate_on(hotel.code, d),
                        subsidy_locked=pol.subsidy_per_night, policy_code=pol.code)
                    self._bed_night[(alloc.hotel_code, alloc.bed_key, day)] = alloc.id
                    self._person_night[(entry.applicant_id, day)] = alloc.id
            except Exception:
                # 回滚：恢复正式索引与持有的释放，不留中间态
                self._bed_night.clear(); self._bed_night.update(idx_snapshot)
                self._person_night.clear(); self._person_night.update(person_snapshot)
                for key in hold_keys:
                    self._held_offers.setdefault(key, entry.id)
                raise

            self.allocations[alloc.id] = alloc
            now = self._now()
            entry.status = WAIT_CONFIRMED
            entry.allocation_id = alloc.id
            entry.updated_at = now.isoformat()
            entry.history.append({"at": now.isoformat(), "from": WAIT_OFFERED,
                                  "to": WAIT_CONFIRMED, "reason": "confirmed",
                                  "allocation_id": alloc.id,
                                  "offer_token": entry.offer_token})
            self._pump_waitlist()
            return {"waitlist_id": entry.id, "idempotent": False,
                    "status": WAIT_CONFIRMED, "allocation": alloc}

    def decline_waitlist(self, actor, entry_id, reason=""):
        """申请人拒绝保留：原子释放床位并推动下一位。"""
        with self.lock:
            entry = self._load_wait(entry_id)
            self._require_role(actor, "applicant")
            if actor.id != entry.applicant_id:
                raise PermissionError("只能拒绝本人的候补")
            if entry.status == WAIT_DECLINED:
                return {"waitlist_id": entry.id, "idempotent": True,
                        "status": WAIT_DECLINED}
            if entry.status != WAIT_OFFERED:
                raise WaitlistConflictError("候补当前没有待拒绝的保留方案",
                                            status=entry.status)
            self._release_offer(entry, "declined")
            now = self._now()
            entry.status = WAIT_DECLINED
            entry.updated_at = now.isoformat()
            entry.history.append({"at": now.isoformat(), "from": WAIT_OFFERED,
                                  "to": WAIT_DECLINED,
                                  "reason": reason or "applicant_declined"})
            self._pump_waitlist()
            return {"waitlist_id": entry.id, "status": WAIT_DECLINED}

    def cancel_waitlist(self, actor, entry_id, reason=""):
        """撤回候补（排队中或已出保留均可）。"""
        with self.lock:
            entry = self._load_wait(entry_id)
            self._require_role(actor, "applicant", "duty_manager")
            if actor.role == "applicant" and actor.id != entry.applicant_id:
                raise PermissionError("只能撤回本人的候补")
            if entry.status in WAIT_FINAL:
                raise WaitlistConflictError("候补已结束，不能撤回",
                                            status=entry.status)
            if entry.status == WAIT_OFFERED:
                self._release_offer(entry, "cancelled")
            old = entry.status
            now = self._now()
            entry.status = WAIT_CANCELLED
            entry.updated_at = now.isoformat()
            entry.history.append({"at": now.isoformat(), "from": old,
                                  "to": WAIT_CANCELLED,
                                  "reason": reason or "applicant_cancelled"})
            self._pump_waitlist()
            return {"waitlist_id": entry.id, "status": WAIT_CANCELLED}

    # ------------------------------------------------------------- 跨日截止

    def expire_waitlists(self, actor=None, at=None):
        """让所有已到截止点的保留/过期候补失效（自动化定时任务可调用）。

        跨日推进只依赖注入时钟：截止时刻是登记时固化的绝对时间，这里不重算。
        """
        if actor is not None:
            self._require_role(actor, "verifier", "duty_manager")
        with self.lock:
            expired = self._expire_due_offers(
                parse_dt(at) if at is not None else self._now())
            expired += self._expire_passed_windows(
                parse_dt(at) if at is not None else self._now())
            if expired:
                self._pump_waitlist()
            return {"at": (parse_dt(at) if at is not None else self._now()).isoformat(),
                    "expired": [e.id for e in expired]}

    def _expire_due_offers(self, now=None):
        now = now or self._now()
        due = [e for e in self.waitlist.values()
               if e.status == WAIT_OFFERED and parse_dt(e.confirm_deadline) <= now]
        for entry in due:
            self._release_offer(entry, "deadline_passed")
            entry.status = WAIT_EXPIRED
            entry.updated_at = now.isoformat()
            entry.history.append({
                "at": now.isoformat(), "from": WAIT_OFFERED, "to": WAIT_EXPIRED,
                "reason": "deadline_passed", "deadline": entry.confirm_deadline})
        return due

    def _expire_passed_windows(self, now=None):
        """候补日期窗口整体已成为过去：无论是否出过保留都结束。"""
        now = now or self._now()
        today = now.date()
        out = []
        for entry in self.waitlist.values():
            if entry.status in (WAIT_QUEUED, WAIT_OFFERED, WAIT_BLOCKED) \
                    and parse_date(entry.end) < today:
                if entry.status == WAIT_OFFERED:
                    self._release_offer(entry, "window_passed")
                old = entry.status
                entry.status = WAIT_EXPIRED
                entry.updated_at = now.isoformat()
                entry.history.append({"at": now.isoformat(), "from": old,
                                      "to": WAIT_EXPIRED,
                                      "reason": "window_passed"})
                out.append(entry)
        return out

    # ------------------------------------------------------------- 队列推进

    def _pump_waitlist(self):
        """房态恢复后按固化顺序晋位；调用方必须已持有系统锁。

        每个条目至多暂时保留一个方案；保留成功即停止该条目的匹配，继续下一位。
        """
        if getattr(self, "_pumping", False):
            return
        self._pumping = True
        try:
            self._expire_due_offers()
            # blocked 条目先复查：冲突消除则带原 rank_key 归队
            for entry in list(self.waitlist.values()):
                if entry.status != WAIT_BLOCKED:
                    continue
                conflict = self._waitlist_conflict(
                    entry.applicant_id, self._wait_dates(entry), entry.id)
                if conflict is None:
                    now = self._now()
                    entry.status = WAIT_QUEUED
                    entry.blocked_reason = None
                    entry.blocked_detail = {}
                    entry.updated_at = now.isoformat()
                    entry.history.append({"at": now.isoformat(),
                                          "from": WAIT_BLOCKED, "to": WAIT_QUEUED,
                                          "reason": "conflict_cleared"})

            active = [e for e in self.waitlist.values() if e.status == WAIT_QUEUED]
            active.sort(key=lambda e: tuple(e.rank_key))
            for entry in active:
                if entry.status != WAIT_QUEUED:
                    continue
                conflict = self._waitlist_conflict(
                    entry.applicant_id, self._wait_dates(entry), entry.id)
                if conflict is not None:
                    self._mark_blocked(entry, conflict)
                    continue
                choice = self._find_offer_bed(entry)
                if choice is None:
                    entry.last_skip_reason = {
                        "code": "no_bed",
                        "at": self._now().isoformat(),
                        "preferred_hotels": list(entry.preferred_hotels)}
                    continue
                hotel_code, bed_key = choice
                dates = self._wait_dates(entry)
                now = self._now()
                deadline = self._offer_deadline(entry, now)
                if deadline <= now:
                    # 申请人声明的最晚确认时间已过：不出保留，直接过期并继续下一位
                    old = entry.status
                    entry.status = WAIT_EXPIRED
                    entry.updated_at = now.isoformat()
                    entry.last_skip_reason = {"code": "respond_by_passed",
                                              "at": now.isoformat()}
                    entry.history.append({"at": now.isoformat(), "from": old,
                                          "to": WAIT_EXPIRED,
                                          "reason": "respond_by_passed",
                                          "deadline": deadline.isoformat()})
                    continue
                token = f"offer_{entry.register_seq}_{len(entry.history)}_{deadline.isoformat()}"
                for d in dates:
                    self._held_offers[(hotel_code, bed_key, iso(d))] = entry.id
                entry.status = WAIT_OFFERED
                entry.offer_hotel = hotel_code
                entry.offer_bed = bed_key
                entry.offer_nights = [iso(d) for d in dates]
                entry.offered_at = now.isoformat()
                entry.confirm_deadline = deadline.isoformat()
                entry.offer_token = token
                entry.last_skip_reason = None
                entry.updated_at = now.isoformat()
                entry.history.append({
                    "at": now.isoformat(), "from": WAIT_QUEUED, "to": WAIT_OFFERED,
                    "reason": "room_recovered", "hotel": hotel_code,
                    "bed": bed_key, "deadline": deadline.isoformat()})
        finally:
            self._pumping = False

    def _find_offer_bed(self, entry):
        """按申请人偏好顺序找整个日期区间都空着的第一张床（确定序）。"""
        dates = self._wait_dates(entry)
        for code in entry.preferred_hotels:
            hotel = self.directory.get(code)
            for bed in hotel.beds:
                if bed.status == BED_BLOCKED:
                    continue
                if all((code, bed.key, iso(d)) not in self._bed_night
                       and self._held_offers.get((code, bed.key, iso(d))) in
                       (None, entry.id) for d in dates):
                    return code, bed.key
        return None

    def _offer_deadline(self, entry, now):
        """保留期限：申请人声明的最晚确认时间优先，否则用默认保留时长。

        期限是绝对时刻，一经算出随保留方案持久化；之后推进时钟/重启服务都不变。
        """
        respond = entry.rank_reasons.get("respond_by")
        if respond:
            return parse_dt(respond)
        return now + timedelta(minutes=DEFAULT_OFFER_HOLD_MINUTES)

    def _mark_blocked(self, entry, conflict):
        now = self._now()
        entry.status = WAIT_BLOCKED
        entry.blocked_reason = conflict.get("reason")
        entry.blocked_detail = conflict
        entry.updated_at = now.isoformat()
        entry.history.append({"at": now.isoformat(), "from": WAIT_QUEUED,
                              "to": WAIT_BLOCKED, "reason": entry.blocked_reason,
                              "detail": conflict})

    def _release_offer(self, entry, reason):
        """删除该条目名下全部持有夜；非本人持有的键绝不误删。"""
        removed = []
        for key, owner in list(self._held_offers.items()):
            if owner == entry.id:
                del self._held_offers[key]
                removed.append(key)
        entry.offer_nights = []
        return removed

    # ------------------------------------------------------------- 冲突校验

    def _assert_waitlist_clear(self, applicant_id, dates, ignore_entry=None):
        """登记校验：任何冲突都直接拒绝（额度不足抛 QuotaError，与占房口径一致）。"""
        conflict = self._waitlist_conflict(applicant_id, dates, ignore_entry)
        if conflict is None:
            return
        if conflict["reason"] == BLOCK_QUOTA:
            raise QuotaError("剩余免费住宿权益不足以覆盖候补日期",
                             request_nights=conflict["request_nights"],
                             remaining_nights=conflict["remaining_nights"])
        raise WaitlistConflictError(
            "候补与既有住宿、争议冻结或未裁决工单冲突", **conflict)

    def _waitlist_conflict(self, applicant_id, dates, ignore_entry=None):
        overlap = self._person_overlap(applicant_id, dates)
        if overlap:
            return {"reason": BLOCK_EXISTING_STAY, "conflicts": overlap[:10]}
        frozen, open_tickets = [], []
        for alloc in self._applicant_allocations(applicant_id):
            for d, line in alloc.nights.items():
                if line.state == NIGHT_BLOCKED:
                    frozen.append(d)
            for t in self._tickets_for(alloc.id):
                if t.status == TICKET_OPEN:
                    open_tickets.append(t.id)
        # 资格申诉类工单不挂占房，按申请人补查
        for t in self.tickets.values():
            if t.applicant_id == applicant_id and t.status == TICKET_OPEN \
                    and t.id not in open_tickets:
                open_tickets.append(t.id)
        if frozen:
            return {"reason": BLOCK_FROZEN, "dates": sorted(frozen)[:10]}
        if open_tickets:
            return {"reason": BLOCK_OPEN_TICKET, "tickets": sorted(open_tickets)[:10]}
        remaining = self.entitlement(applicant_id)["remaining_nights"]
        if len(dates) > remaining:
            return {"reason": BLOCK_QUOTA, "request_nights": len(dates),
                    "remaining_nights": remaining}
        return None

    def _overlapping_active_wait(self, applicant_id, dates, ignore=None):
        days = set(dates)
        for e in self.waitlist.values():
            if e.applicant_id != applicant_id or e.id == ignore:
                continue
            if e.status in (WAIT_QUEUED, WAIT_OFFERED, WAIT_BLOCKED) \
                    and days.intersection(set(self._wait_dates(e))):
                return e
        return None

    # ------------------------------------------------------------- 视图

    def waitlist_queue_view(self, actor, status=None, hotel_code=None, date=None):
        """工作人员队列视图：含排序依据、跳过原因与保留状态。"""
        self._require_role(actor, "verifier", "duty_manager", "hotel_front",
                           "service_officer")
        if actor.role == "hotel_front":
            hotel_code = actor.hotels[0] if actor.hotels else hotel_code
            if actor.hotels and hotel_code not in actor.hotels:
                raise PermissionError("无权查看该酒店候补队列")
        day = parse_date(date) if date else None
        with self.lock:
            self._expire_due_offers()
            entries = list(self.waitlist.values())
            if status and status != "all":
                statuses = status.split(",")
                entries = [e for e in entries if e.status in statuses]
            elif status is None:
                entries = [e for e in entries if e.status not in WAIT_FINAL]
            if hotel_code:
                entries = [e for e in entries
                           if hotel_code in e.preferred_hotels
                           or e.offer_hotel == hotel_code]
            if day:
                entries = [e for e in entries
                           if parse_date(e.start) <= day <= parse_date(e.end)]
            ordered = sorted(entries, key=self._view_sort_key)
            positions = self._positions()
            return {"queue": [self._waitlist_dict(e, positions.get(e.id))
                              for e in ordered]}

    def waitlist_mine(self, actor):
        """申请人视图：只含本人候补，附队列位次与可解释排序。"""
        self._require_role(actor, "applicant")
        with self.lock:
            self._expire_due_offers()
            positions = self._positions()
            entries = sorted(
                (e for e in self.waitlist.values() if e.applicant_id == actor.id),
                key=lambda e: e.register_seq)
            return {"queue": [self._waitlist_dict(e, positions.get(e.id))
                              for e in entries]}

    def get_waitlist(self, actor, entry_id):
        with self.lock:
            self._expire_due_offers()
            entry = self._load_wait(entry_id)
            self._assert_waitlist_visible(actor, entry)
            return entry

    def waitlist_entry_view(self, actor, entry_id):
        """单条候补的可解释视图（含队列位次），供 HTTP 层直接输出。"""
        with self.lock:
            self._expire_due_offers()
            entry = self._load_wait(entry_id)
            self._assert_waitlist_visible(actor, entry)
            return self._waitlist_dict(entry, self._positions().get(entry.id))

    def _assert_waitlist_visible(self, actor, entry):
        if actor.role == "applicant" and actor.id != entry.applicant_id:
            raise PermissionError("只能查看本人候补")
        if actor.role not in ("applicant", "verifier", "duty_manager",
                              "hotel_front", "service_officer"):
            raise PermissionError("无权查看候补队列")
        if actor.role == "hotel_front" and actor.hotels \
                and entry.offer_hotel not in actor.hotels \
                and not set(entry.preferred_hotels) & set(actor.hotels):
            raise PermissionError("无权查看该酒店候补")

    def _positions(self):
        """活跃队列中的全局位次（按固化 rank_key）。"""
        active = [e for e in self.waitlist.values()
                  if e.status in (WAIT_QUEUED, WAIT_OFFERED)]
        active.sort(key=lambda e: tuple(e.rank_key))
        return {e.id: i + 1 for i, e in enumerate(active)}

    def _view_sort_key(self, entry):
        # 活跃条目按晋位顺序，其余按更新时间倒序
        if entry.status in (WAIT_QUEUED, WAIT_OFFERED):
            return (0, *entry.rank_key)
        order = {WAIT_BLOCKED: 1, WAIT_CONFIRMED: 2, WAIT_DECLINED: 3,
                 WAIT_EXPIRED: 4, WAIT_CANCELLED: 5}
        return (1, order.get(entry.status, 9), entry.register_seq)

    def _waitlist_dict(self, entry, position=None):
        app = self.applications.get(entry.app_id)
        return {
            "id": entry.id,
            "app_id": entry.app_id,
            "applicant_id": entry.applicant_id,
            "applicant_name": (app.material.get("name") if app else None),
            "start": entry.start,
            "end": entry.end,
            "nights": len(self._wait_dates(entry)),
            "preferred_hotels": list(entry.preferred_hotels),
            "urgency": entry.urgency,
            "status": entry.status,
            "queue_position": position,
            "rank_key": list(entry.rank_key),
            "rank_reasons": dict(entry.rank_reasons),
            "decided_on": entry.decided_on,
            "register_seq": entry.register_seq,
            "offer": None if entry.status == WAIT_QUEUED and not entry.offer_hotel
            else self._offer_dict(entry),
            "blocked_reason": entry.blocked_reason,
            "blocked_detail": dict(entry.blocked_detail),
            "allocation_id": entry.allocation_id,
            "last_skip_reason": entry.last_skip_reason,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "history": list(entry.history),
        }

    def _offer_dict(self, entry):
        if not entry.offer_hotel:
            return None
        return {
            "hotel_code": entry.offer_hotel,
            "bed_key": entry.offer_bed,
            "nights": list(entry.offer_nights),
            "offered_at": entry.offered_at,
            "confirm_deadline": entry.confirm_deadline,
            "offer_token": entry.offer_token,
            "seconds_remaining": max(
                0, int((parse_dt(entry.confirm_deadline) - self._now())
                       .total_seconds())) if entry.status == WAIT_OFFERED else 0,
        }

    # ------------------------------------------------------------- 持久化辅助

    def export_waitlist(self):
        """导出可 JSON 化的候补状态（含持有索引与顺序计数器）。"""
        return {
            "seq": self._waitlist_seq,
            "entries": [asdict(e) for e in self.waitlist.values()],
            "holds": [[h, b, d, eid] for (h, b, d), eid
                      in sorted(self._held_offers.items())],
        }

    def import_waitlist(self, data):
        """从导出数据恢复候补；晋位顺序（rank_key/seq）与保留期限原样恢复。"""
        self._waitlist_seq = int(data.get("seq", 0))
        self.waitlist = {}
        for raw in data.get("entries", []):
            raw = dict(raw)
            raw["rank_key"] = list(raw.get("rank_key") or [])
            raw["rank_reasons"] = dict(raw.get("rank_reasons") or {})
            raw["preferred_hotels"] = list(raw.get("preferred_hotels") or [])
            raw["offer_nights"] = list(raw.get("offer_nights") or [])
            raw["history"] = list(raw.get("history") or [])
            raw["blocked_detail"] = dict(raw.get("blocked_detail") or {})
            self.waitlist[raw["id"]] = WaitlistEntry(**raw)
        self._held_offers = {(h, b, d): eid
                             for h, b, d, eid in data.get("holds", [])}

    # ------------------------------------------------------------- 内部

    def _load_wait(self, entry_id):
        entry = self.waitlist.get(entry_id)
        if entry is None:
            raise NotFoundError("候补条目不存在", waitlist_id=entry_id)
        return entry

    def _wait_dates(self, entry):
        return list(daterange(parse_date(entry.start), parse_date(entry.end)))
