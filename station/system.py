"""住宿运营核心系统。

一份住宿权益贯穿"确认占房—入住核验—临时离店—跨站调剂—退房清算"全流程；
所有写操作在同一把锁内顺序提交，保证 (酒店, 床位, 夜) 与 (申请人, 夜) 两个
维度的占用唯一性。系统不会静默放行异常：爽约、超期、紧急延住、无法识别或
互相矛盾的补传事件全部生成挂到值班长名下的人工复核工单。

房态唯一来源是按夜占用索引 _bed_night；床位本身只有 free/blocked（停售）
两种运营状态，"今晚谁住"一律由索引回答，避免多夜占房跨日互相覆盖。
"""

import threading
import uuid
from datetime import timedelta

from . import materials as mat
from . import policy as policy_mod
from .catalog import BED_BLOCKED
from .errors import (BookingConflictError, DomainError, NotFoundError,
                     PermissionError, PolicyError, QuotaError,
                     ReviewRequiredError, RoomUnavailableError, ValidationError)
from .models import *
from .timeutil import daterange, iso, now_cst, parse_date, parse_dt
from .waitlist import WaitlistMixin

# 仍在占用（消耗权益、阻挡他人订房）的夜状态
OCCUPYING = {NIGHT_HELD, NIGHT_STAYED, NIGHT_AWAY, NIGHT_OVERSTAY, NIGHT_BLOCKED}
# 同源语义事件在该窗口内视为同一件事（门锁与前台各自补传也能去重）
SEMANTIC_DEDUP_MINUTES = 30

_EVENT_TYPES = {"check_in", "door_open", "temp_leave", "return", "check_out"}


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LodgingSystem(WaitlistMixin):
    def __init__(self, policies, directory, rates, now_fn=None):
        self.policies = policies
        self.directory = directory
        self.rates = rates
        self._now = now_fn or now_cst
        self.lock = threading.RLock()

        self.users = {}              # user_id -> Actor
        self.duty_manager_id = None  # 人工例外责任人
        self.applications = {}
        self.allocations = {}
        self.events = {}
        self.tickets = {}
        self.requests = {}
        self.settlements = {}
        # (hotel, bed, iso_day) -> allocation_id
        self._bed_night = {}
        # (applicant_id, iso_day) -> allocation_id，防止跨店重复占房/重复报销
        self._person_night = {}
        # 语义去重指纹 -> event_id
        self._fingerprints = {}
        # 候补队列：entry_id -> WaitlistEntry；register_seq 全局自增保证申请顺序
        self.waitlist = {}
        self._waitlist_seq = 0
        # (hotel, bed, iso_day) -> waitlist_entry_id：暂时保留的按夜持有索引，
        # 与 _bed_night 共用排他性但不消耗权益
        self._held_offers = {}
        self._pumping = False

    # ------------------------------------------------------------------ 用户

    def register_user(self, actor: Actor):
        self.users[actor.id] = actor
        if actor.role == "duty_manager" and self.duty_manager_id is None:
            self.duty_manager_id = actor.id
        return actor

    def actor(self, user_id):
        if user_id not in self.users:
            raise PermissionError("未知用户", user_id=user_id)
        return self.users[user_id]

    def _require_role(self, actor, *roles):
        if actor.role not in roles:
            raise PermissionError("该操作不允许当前角色", role=actor.role, need=list(roles))

    def _duty_manager(self):
        if not self.duty_manager_id:
            raise ValidationError("尚未配置值班长，无法生成人工复核工单")
        return self.users[self.duty_manager_id]

    # ------------------------------------------------------------- 申请与资格

    def submit_application(self, actor: Actor, material):
        """提交申请并按"申请当日有效"的政策版本固化资格结论。"""
        self._require_role(actor, "applicant")
        if actor.id != material.get("applicant_id"):
            raise PermissionError("只能为本人提交申请")
        with self.lock:
            on = self._now().date()
            pol = self.policies.effective_at(on)
            app = Application(
                id=_new_id("app"), applicant_id=actor.id,
                material=dict(material), submitted_on=iso(on),
                policy_code=pol.code)
            try:
                decision = policy_mod.evaluate(pol, material, on)
                app.eligibility = decision.to_dict()
                app.status = APP_ELIGIBLE
            except PolicyError as exc:
                if exc.appealable:
                    app.status = APP_IN_REVIEW
                    ticket = self._open_ticket(
                        REVIEW_ELIGIBILITY_APPEAL, None, app, None,
                        "资格处于政策临界，需人工复核",
                        {"reasons": exc.reasons, "policy": pol.code})
                    app.tickets.append(ticket.id)
                    self.applications[app.id] = app
                    raise ReviewRequiredError(
                        "资格处于临界，已转人工复核", REVIEW_ELIGIBILITY_APPEAL,
                        ticket.id, reasons=exc.reasons)
                app.status = APP_DENIED
                self.applications[app.id] = app
                raise
            self.applications[app.id] = app
            return app

    def get_application(self, actor: Actor, app_id):
        app = self._load_app(app_id)
        if actor.role == "applicant" and actor.id != app.applicant_id:
            raise PermissionError("只能查看本人申请")
        return app

    def view_material(self, actor: Actor, app_id):
        """按角色投影材料：团干部处理诉求时看不到非必要的求职字段。"""
        app = self.get_application(actor, app_id)
        return mat.redact_fields(actor.role, app.material)

    # ------------------------------------------------------------- 占房与权益

    def entitlement(self, applicant_id):
        """一份权益的逐日消耗视图：跨店合计、剩余夜数、待裁决例外夜。

        争议冻结夜（blocked）仍占用权益：裁决结论出来之前不能把额度还给申请人，
        否则会出现"一边争议未决、一边再订新房"的重复占用。
        """
        with self.lock:
            policy_code = None
            consumed = {}
            exception_days = set()
            blocked_days = set()
            for alloc in self._applicant_allocations(applicant_id):
                policy_code = alloc.policy_code
                for day, line in alloc.nights.items():
                    if line.state in (NIGHT_HELD, NIGHT_STAYED, NIGHT_AWAY,
                                      NIGHT_OVERSTAY, NIGHT_BLOCKED):
                        consumed[day] = alloc.id
                    if line.state == NIGHT_OVERSTAY or not line.compliant:
                        exception_days.add(day)
                    if line.state == NIGHT_BLOCKED:
                        blocked_days.add(day)
            if policy_code:
                pol = self.policies.get(policy_code)
            else:
                pol = self.policies.effective_at(self._now().date())
            used = len(consumed)
            return {
                "applicant_id": applicant_id,
                "policy_code": pol.code,
                "max_free_nights": pol.max_free_nights,
                "used_nights": used,
                "remaining_nights": max(0, pol.max_free_nights - used),
                "exception_nights": sorted(exception_days),
                "blocked_nights": sorted(blocked_days),
                "consumed_dates": sorted(consumed),
            }

    def confirm_booking(self, actor: Actor, app_id, hotel_code, start, end, bed_key=None):
        """确认占房：原子选择床位、锁价、扣减跨店共享权益。"""
        with self.lock:
            app = self._load_app(app_id)
            self._require_role(actor, "applicant", "verifier", "duty_manager")
            if actor.role == "applicant" and actor.id != app.applicant_id:
                raise PermissionError("只能为本人确认占房")
            if actor.role in ("verifier", "duty_manager") and not actor.can_access_hotel(hotel_code):
                raise PermissionError("无权操作该酒店")
            if app.status != APP_ELIGIBLE:
                raise ValidationError("申请尚未通过资格核验", status=app.status)

            start, end = parse_date(start), parse_date(end)
            if end < start:
                raise ValidationError("结束日期早于开始日期")
            dates = list(daterange(start, end))
            hotel = self.directory.get(hotel_code)

            chosen = self._pick_bed(hotel, dates, bed_key)
            overlap = self._person_overlap(app.applicant_id, dates)
            if overlap:
                raise BookingConflictError(
                    "与既有占房日期重叠，请勿重复占房；跨店续住请使用调剂",
                    conflicts=overlap[:10])

            pol = self.policies.get(app.policy_code)
            ent = self.entitlement(app.applicant_id)
            if len(dates) > ent["remaining_nights"]:
                raise QuotaError(
                    "超出剩余免费住宿权益",
                    max_free_nights=ent["max_free_nights"],
                    used_nights=ent["used_nights"],
                    request_nights=len(dates),
                    remaining_nights=ent["remaining_nights"])

            alloc = Allocation(
                id=_new_id("alloc"), app_id=app.id, applicant_id=app.applicant_id,
                hotel_code=hotel_code, bed_key=chosen.key,
                start=iso(start), end=iso(end), planned_end=iso(end),
                policy_code=pol.code, chain_id=_new_id("chain"),
                created_from="direct", created_at=self._now().isoformat())
            self._populate_nights(alloc, hotel, dates, pol)
            self.allocations[alloc.id] = alloc
            return alloc

    def _pick_bed(self, hotel, dates, bed_key=None):
        """在唯一占用索引下挑床；显式指定但不可用时直接拒绝（不静默换床）。"""
        candidates = [b for b in hotel.beds if b.status != BED_BLOCKED]
        if bed_key:
            wanted = next((b for b in candidates if b.key == bed_key), None)
            if wanted is None:
                raise RoomUnavailableError("指定床位不可用或已停售",
                                           hotel=hotel.code, bed=bed_key)
            candidates = [wanted]
        for b in candidates:
            if self._bed_available(hotel.code, b.key, dates):
                return b
        raise RoomUnavailableError("所选日期没有可用床位",
                                   hotel=hotel.code,
                                   start=iso(dates[0]), end=iso(dates[-1]))

    def _populate_nights(self, alloc, hotel, dates, pol):
        for d in dates:
            day = iso(d)
            bed_owner = self._bed_night.get((hotel.code, alloc.bed_key, day))
            if bed_owner is not None:
                # 不变量破坏：唯一房态索引已被他单占用（调用方应先用 _pick_bed 排除）
                raise RoomUnavailableError("床位夜已被占用",
                                           hotel=hotel.code, bed=alloc.bed_key, date=day,
                                           allocation_id=bed_owner)
            person_owner = self._person_night.get((alloc.applicant_id, day))
            if person_owner is not None:
                raise BookingConflictError("申请人当夜已有占房", date=day,
                                           allocation_id=person_owner)
            alloc.nights[day] = NightLine(
                date=day, state=NIGHT_HELD,
                rate_locked=self.rates.rate_on(hotel.code, d),
                subsidy_locked=pol.subsidy_per_night, policy_code=pol.code)
            self._bed_night[(hotel.code, alloc.bed_key, day)] = alloc.id
            self._person_night[(alloc.applicant_id, day)] = alloc.id

    def withdraw_dates(self, actor: Actor, allocation_id, dates):
        """申请人撤回尚未使用的日期：立即释放房间与权益。"""
        with self.lock:
            alloc = self._load_alloc(allocation_id)
            self._require_owner_or_staff(actor, alloc)
            days = sorted({iso(parse_date(d)) for d in dates})
            released = []
            for day in days:
                line = alloc.nights.get(day)
                if line is None:
                    raise NotFoundError("该日期不在占房范围内", date=day)
                if line.state != NIGHT_HELD:
                    raise ValidationError("只能撤回尚未使用的日期",
                                          date=day, state=line.state)
                line.state = NIGHT_RELEASED
                released.append(day)
                self._release_indices(alloc, day)
            self._trim_span(alloc)
            self._pump_waitlist()   # 房态恢复：按固化顺序晋位
            return {"allocation_id": alloc.id, "released": released}

    def cancel_booking(self, actor: Actor, allocation_id):
        """整单取消（仅限全部夜均未使用）。"""
        with self.lock:
            alloc = self._load_alloc(allocation_id)
            self._require_owner_or_staff(actor, alloc)
            blocked = [d for d, l in alloc.nights.items() if l.state == NIGHT_BLOCKED]
            if blocked:
                raise ValidationError("存在争议冻结夜，请先等待人工裁决", dates=blocked[:10])
            used = [d for d, l in alloc.nights.items()
                    if l.state in (NIGHT_STAYED, NIGHT_AWAY, NIGHT_OVERSTAY)]
            if used:
                raise ValidationError("已有入住记录，不能整单取消", dates=used[:10])
            released = []
            for day, line in alloc.nights.items():
                if line.state == NIGHT_HELD:
                    line.state = NIGHT_RELEASED
                    released.append(day)
                    self._release_indices(alloc, day)
            self._trim_span(alloc)
            self._pump_waitlist()   # 整单退订释放：按固化顺序晋位
            return {"allocation_id": alloc.id, "cancelled": True, "released": released}

    # ------------------------------------------------------------- 跨站调剂

    def transfer(self, actor: Actor, allocation_id, to_hotel_code, on_date,
                 new_end=None, bed_key=None):
        """跨站调剂：当日截断原单（保留已入住夜），在新店原子承接剩余权益。

        调剂不是先退后订（中间态会让最后床位被别人抢走），而是在同一把锁内
        完成"释放旧夜 + 占用新店"，对外只呈现成功或失败两种结果。
        """
        self._require_role(actor, "applicant", "verifier", "duty_manager")
        with self.lock:
            src = self._load_alloc(allocation_id)
            if actor.role == "applicant" and actor.id != src.applicant_id:
                raise PermissionError("只能调剂本人占房")
            if actor.role != "applicant" and not actor.can_access_hotel(to_hotel_code):
                raise PermissionError("无权操作目标酒店")
            if any(t.status == TICKET_OPEN for t in self._tickets_for(src.id)):
                raise ValidationError("存在未裁决工单，不能调剂")
            on = parse_date(on_date)
            if iso(on) <= src.start:
                raise ValidationError("调剂日必须晚于原单开始日", start=src.start)
            used_later = [d for d, l in src.nights.items()
                          if parse_date(d) >= on
                          and l.state in (NIGHT_STAYED, NIGHT_AWAY, NIGHT_OVERSTAY)]
            if used_later:
                raise ValidationError("调剂日及以后已有在店记录，无法调剂，请发起人工复核",
                                      dates=used_later[:10])
            blocked_later = [d for d, l in src.nights.items()
                             if parse_date(d) >= on and l.state == NIGHT_BLOCKED]
            if blocked_later:
                raise ValidationError("调剂日及以后存在争议冻结夜，请先等待人工裁决",
                                      dates=blocked_later[:10])

            # ---- 第一阶段：只读校验（任何失败都不留副作用）----
            # 调剂即"人离开旧店"的核验：预览结转，不改变状态
            checkin_day = parse_dt(src.check_in_at).date() if src.check_in_at else None
            preview_late, preview_stayed = [], []
            for d, l in src.nights.items():
                day = parse_date(d)
                if l.state != NIGHT_HELD or not (day < on):
                    continue
                if checkin_day and day >= checkin_day:
                    preview_stayed.append(d)
                else:
                    preview_late.append(d)

            # 承接夜 = 原单中调剂日及以后、申请人未撤回的预订夜；
            # 已撤回释放或争议冻结的日期不能在新店复活。
            carry = sorted(parse_date(d) for d, l in src.nights.items()
                           if parse_date(d) >= on and l.state == NIGHT_HELD)
            if new_end is not None:
                new_end_d = parse_date(new_end)
                if new_end_d < on:
                    raise ValidationError("调剂结束日早于调剂日")
                carry = [d for d in carry if d <= new_end_d]
            if not carry:
                raise ValidationError("调剂日之后没有可承接的有效预订夜")
            hotel = self.directory.get(to_hotel_code)
            chosen = self._pick_bed(hotel, carry, bed_key)
            overlap = self._person_overlap(src.applicant_id, carry, exclude=src.id)
            if overlap:
                raise BookingConflictError("调剂日期与其他占房重叠", conflicts=overlap[:10])
            ent = self.entitlement(src.applicant_id)
            if len(carry) > ent["remaining_nights"]:
                raise QuotaError("调剂夜数超出剩余权益",
                                 remaining_nights=ent["remaining_nights"])
            pol = self.policies.get(src.policy_code)

            # ---- 第二阶段：统一变更（失败则全量回滚，含撤回已开工单）----
            idx_snapshot_bed = {k: v for k, v in self._bed_night.items()}
            idx_snapshot_person = {k: v for k, v in self._person_night.items()}
            state_snapshot = {d: l.state for d, l in src.nights.items()}
            bill_snapshot = {d: l.billable for d, l in src.nights.items()}
            tickets_before = set(self.tickets)
            created_ticket = None
            try:
                for d in preview_stayed:
                    src.nights[d].state = NIGHT_STAYED
                    src.nights[d].billable = True
                if preview_late and not self._has_open_ticket(src.id, REVIEW_NO_SHOW):
                    created_ticket = self._open_ticket(
                        REVIEW_NO_SHOW, src.id, self.applications.get(src.app_id), src,
                        "跨站调剂时发现起始日后、实际入住前的未使用夜，需认定是否爽约",
                        {"dates": sorted(preview_late), "transfer_on": iso(on)})
                    for d in preview_late:
                        src.nights[d].state = NIGHT_BLOCKED
                        src.nights[d].billable = False

                released = []
                for day, line in list(src.nights.items()):
                    if parse_date(day) >= on and line.state == NIGHT_HELD:
                        line.state = NIGHT_RELEASED
                        released.append(day)
                        self._release_indices(src, day)

                dst = Allocation(
                    id=_new_id("alloc"), app_id=src.app_id,
                    applicant_id=src.applicant_id, hotel_code=to_hotel_code,
                    bed_key=chosen.key, start=iso(on), end=iso(carry[-1]),
                    planned_end=src.planned_end, policy_code=src.policy_code,
                    chain_id=src.chain_id, created_from="transfer",
                    created_at=self._now().isoformat())
                self._populate_nights(dst, hotel, carry, pol)
            except DomainError:
                self._bed_night.clear(); self._bed_night.update(idx_snapshot_bed)
                self._person_night.clear(); self._person_night.update(idx_snapshot_person)
                for d, st in state_snapshot.items():
                    src.nights[d].state = st
                    src.nights[d].billable = bill_snapshot[d]
                if created_ticket is not None:
                    self.tickets.pop(created_ticket.id, None)
                    app = self.applications.get(src.app_id)
                    if app and created_ticket.id in app.tickets:
                        app.tickets.remove(created_ticket.id)
                raise
            self.allocations[dst.id] = dst
            self._trim_span(src)
            self._pump_waitlist()   # 旧店夜已释放：按固化顺序晋位
            return {"closed_partial": src.id, "released": released,
                    "new_allocation": dst, "late_nights_ticket": created_ticket.id if created_ticket else None}

    # ------------------------------------------------------- 入住事件与离线补传

    def ingest_event(self, actor: Actor, payload):
        """接收门锁/前台事件（含断网补传）。重复事件幂等识别，矛盾不静默放行。"""
        self._require_role(actor, "hotel_front", "verifier", "duty_manager")
        with self.lock:
            result = self._safe_ingest(payload, actor)
            self._pump_waitlist()   # 退房等事件可能释放床位
            return result

    def ingest_events(self, actor: Actor, payloads):
        """批量补传：整批在锁内顺序处理，逐条给出落地结果。"""
        self._require_role(actor, "hotel_front", "verifier", "duty_manager")
        with self.lock:
            results = [self._safe_ingest(p, actor) for p in payloads]
            self._pump_waitlist()
            return results

    def _safe_ingest(self, payload, actor):
        try:
            result = self._ingest_one(payload, actor)
            return {"event_id": result.get("event_id"), "status": result.get("status"),
                    "result": result}
        except ReviewRequiredError as exc:
            return {"event_id": payload.get("event_id"), "status": "pending_review",
                    "ticket_id": exc.ticket_id, "review_type": exc.review_type,
                    "message": exc.message}
        except DomainError as exc:
            return {"event_id": payload.get("event_id"), "status": "rejected",
                    "code": exc.code, "message": exc.message}

    def _ingest_one(self, payload, actor):
        event_id = payload.get("event_id") or _new_id("evt")
        if event_id in self.events:
            old = self.events[event_id]
            return {"event_id": event_id, "status": "duplicate",
                    "duplicate_of": old.event_id, "first_recorded_at": old.recorded_at}

        source = payload.get("source")
        if source not in (SRC_DOOR_LOCK, SRC_FRONT_DESK, SRC_SYSTEM):
            raise ValidationError("未知事件来源", source=source)
        etype = payload.get("event_type")
        if etype not in _EVENT_TYPES:
            raise ValidationError("未知事件类型", event_type=etype)
        hotel_code = payload.get("hotel_code")
        if not hotel_code:
            raise ValidationError("缺少 hotel_code")
        if not actor.can_access_hotel(hotel_code):
            raise PermissionError("无权上报该酒店事件")
        bed_key = payload.get("bed_key")
        occurred = parse_dt(payload.get("occurred_at") or self._now())
        recorded = parse_dt(payload.get("recorded_at")) or self._now()
        offline = bool(payload.get("offline")) or recorded - occurred > timedelta(minutes=15)

        # 语义去重：门锁与前台对同一件事各报一次，时间窗内只认第一条
        fp = self._semantic_fp(hotel_code, bed_key, etype, payload, occurred)
        dup_id = self._find_fingerprint(fp, occurred) if fp else None
        if dup_id:
            old = self.events[dup_id]
            self.events[event_id] = EventRecord(
                event_id=event_id, source=source, hotel_code=hotel_code,
                bed_key=bed_key, event_type=etype, occurred_at=occurred.isoformat(),
                recorded_at=recorded.isoformat(), payload=dict(payload.get("payload", payload)),
                status="duplicate", duplicate_of=dup_id, offline=offline,
                applicant_id=old.applicant_id, allocation_id=old.allocation_id)
            return {"event_id": event_id, "status": "duplicate",
                    "duplicate_of": dup_id, "first_recorded_at": old.recorded_at}

        event = EventRecord(
            event_id=event_id, source=source, hotel_code=hotel_code,
            bed_key=bed_key, event_type=etype, occurred_at=occurred.isoformat(),
            recorded_at=recorded.isoformat(),
            payload={k: v for k, v in payload.items() if k not in (
                "event_id", "source", "hotel_code", "bed_key", "event_type",
                "occurred_at", "recorded_at", "offline")},
            offline=offline)
        self.events[event_id] = event
        if fp:
            self._fingerprints[fp] = event_id

        alloc = self._resolve_allocation(hotel_code, bed_key, occurred.date(),
                                         payload.get("applicant_id"), etype)
        if alloc is None:
            self._pend_event(event, None, REVIEW_UNKNOWN_EVENT, hotel_code,
                             "补传事件找不到有效占房，已转人工复核")
            raise ReviewRequiredError("事件无对应占房", REVIEW_UNKNOWN_EVENT, event.ticket_id)
        event.allocation_id = alloc.id
        event.applicant_id = alloc.applicant_id

        # 过夜结转：事件日之前、入住之后的预订夜，若无离店记录覆盖（仍为 held），
        # 即视为连续在店；入住事件本身不向前结转（晚到前的夜交给爽约裁决）。
        if etype != "check_in":
            through = parse_dt(event.occurred_at).date() - timedelta(days=1)
            self._roll_forward(alloc, through)

        return {
            "check_in": self._apply_check_in,
            "door_open": self._apply_door_open,
            "temp_leave": self._apply_temp_leave,
            "return": self._apply_return,
            "check_out": self._apply_check_out,
        }[etype](event, alloc)

    def _apply_check_in(self, event, alloc):
        day = iso(parse_dt(event.occurred_at).date())
        line = alloc.nights.get(day)
        if line is None:
            self._pend_event(event, alloc.id, REVIEW_EVENT_CONFLICT, alloc.hotel_code,
                             "入住日期不在占房区间内",
                             {"date": day, "start": alloc.start, "end": alloc.end})
            raise ReviewRequiredError("入住日期冲突", REVIEW_EVENT_CONFLICT, event.ticket_id)
        if line.state == NIGHT_BLOCKED:
            self._pend_event(event, alloc.id, REVIEW_EVENT_CONFLICT, alloc.hotel_code,
                             "争议冻结期间收到入住记录，需人工确认", {"date": day})
            raise ReviewRequiredError("争议夜收到入住记录", REVIEW_EVENT_CONFLICT, event.ticket_id)
        if alloc.check_in_at:
            prior = next((e for e in self.events.values()
                          if e.allocation_id == alloc.id and e.event_type == "check_in"
                          and e.event_id != event.event_id and e.status == "applied"), None)
            event.status = "duplicate"
            event.duplicate_of = prior.event_id if prior else None
            return {"event_id": event.event_id, "status": "duplicate",
                    "duplicate_of": event.duplicate_of}
        # 只把实际入住日记为在店；此前未使用的夜保持 held，由爽约巡检裁决，
        # 绝不能因为一次晚到补登就把没住的夜也算进补贴。
        alloc.check_in_at = event.occurred_at
        line.state = NIGHT_STAYED
        line.billable = True
        line.events.append(event.event_id)
        return {"event_id": event.event_id, "status": "applied",
                "allocation_id": alloc.id, "check_in_at": alloc.check_in_at}

    def _apply_door_open(self, event, alloc):
        day = iso(parse_dt(event.occurred_at).date())
        line = alloc.nights.get(day)
        if line is None:
            self._pend_event(event, alloc.id, REVIEW_UNKNOWN_EVENT, alloc.hotel_code,
                             "开门记录落在占房区间之外", {"date": day})
            raise ReviewRequiredError("开门日期超出占房区间", REVIEW_UNKNOWN_EVENT, event.ticket_id)
        line.events.append(event.event_id)
        note = None
        if line.state == NIGHT_HELD:
            # 门锁开门是"该夜实际在店"的佐证
            line.state = NIGHT_STAYED
            line.billable = True
        elif line.state == NIGHT_AWAY:
            note = "离店期间开门，已留存佐证，归还请以 return 事件为准"
        return {"event_id": event.event_id, "status": "applied", "note": note}

    def _apply_temp_leave(self, event, alloc):
        if not alloc.check_in_at:
            self._pend_event(event, alloc.id, REVIEW_EVENT_CONFLICT, alloc.hotel_code,
                             "尚未入住即上报临时离店")
            raise ReviewRequiredError("未入住先离店", REVIEW_EVENT_CONFLICT, event.ticket_id)
        start_day = parse_dt(event.occurred_at).date()
        if event.payload.get("expected_return") is None:
            raise ValidationError("临时离店必须给出 expected_return")
        until = parse_date(event.payload["expected_return"])
        if until < start_day or iso(until) > alloc.end:
            raise ValidationError("预计归还日期超出占房区间",
                                  expected_return=iso(until), end=alloc.end)
        # 夜粒度：离店当日 .. 归还日前一日 的夜人不在店（房间保留、消耗权益、不计补贴）
        changed = []
        for d, line in alloc.nights.items():
            day = parse_date(d)
            if start_day <= day < until and line.state in (NIGHT_HELD, NIGHT_STAYED, NIGHT_AWAY):
                line.state = NIGHT_AWAY
                line.billable = False
                line.events.append(event.event_id)
                changed.append(d)
        event.status = "applied"
        return {"event_id": event.event_id, "status": "applied",
                "away_dates": changed, "return_date": iso(until)}

    def _apply_return(self, event, alloc):
        # 归还只说明"从此刻起人在店"：已划定的 away 夜是既成事实（人不在店、
        # 不计补贴），不改标；当日及之后的 held 由过夜结转为 stayed。
        event.status = "applied"
        away_kept = [d for d, l in alloc.nights.items() if l.state == NIGHT_AWAY]
        return {"event_id": event.event_id, "status": "applied",
                "away_nights": sorted(away_kept)}

    def _apply_check_out(self, event, alloc):
        day = parse_dt(event.occurred_at).date()
        checkin_day = parse_dt(alloc.check_in_at).date() if alloc.check_in_at else day
        stayed, released, late = [], [], []
        for d, line in list(alloc.nights.items()):
            cd = parse_date(d)
            if cd < day:
                if line.state == NIGHT_HELD and cd >= checkin_day:
                    # 追认入住日之后的预订夜为连续在店
                    line.state = NIGHT_STAYED
                    line.billable = True
                    stayed.append(d)
                elif line.state == NIGHT_HELD and cd < checkin_day:
                    # 晚到前未使用的夜：不静默放行也不擅自算爽约，冻结待认定
                    late.append(d)
            elif line.state in (NIGHT_HELD, NIGHT_OVERSTAY):
                line.state = NIGHT_RELEASED
                self._release_indices(alloc, d)
                released.append(d)

        if late and not self._has_open_ticket(alloc.id, REVIEW_NO_SHOW):
            ticket = self._open_ticket(
                REVIEW_NO_SHOW, alloc.id,
                self.applications.get(alloc.app_id), alloc,
                "实际入住日晚于占房起始日，此前未使用夜需人工认定是否爽约",
                {"event_id": event.event_id, "dates": sorted(late),
                 "start": alloc.start, "check_in_at": alloc.check_in_at})
            for d in late:
                alloc.nights[d].state = NIGHT_BLOCKED
                alloc.nights[d].billable = False

        # 退房时仍存在未闭合的临时离店：从离店发生到退房前的夜全部冻结，
        # 不能静默按在店清算（有无开门佐证由值班长认定）。
        frozen = []
        open_leaves = self._open_leaves(alloc)
        if open_leaves:
            earliest = min(
                parse_dt(self.events[leaf["event_id"]].occurred_at).date()
                for leaf in open_leaves)
            for d, line in list(alloc.nights.items()):
                cd = parse_date(d)
                if earliest <= cd < day and line.state in (NIGHT_STAYED, NIGHT_AWAY, NIGHT_HELD):
                    line.state = NIGHT_BLOCKED
                    line.billable = False
                    frozen.append(d)

        alloc.check_out_at = event.occurred_at
        self._trim_span(alloc)
        event.status = "applied"
        result = {"event_id": event.event_id, "status": "applied",
                  "stayed": stayed, "released": released, "frozen": sorted(frozen)}
        if frozen:
            ticket = self._open_ticket(
                REVIEW_UNCLOSED_LEAVE, alloc.id,
                self.applications.get(alloc.app_id), alloc,
                "退房时存在未闭合的临时离店记录，需人工认定在店夜",
                {"event_id": event.event_id, "dates": sorted(frozen)})
            result["ticket_id"] = ticket.id
        return result

    def _roll_forward(self, alloc, through):
        """过夜结转：入住后到 through 之间仍 held 的夜视为连续在店。

        离店夜在上报时已标 away，不会被结转；入住日之前的 held 保持不动，
        以免把晚到前没住的夜算成补贴。
        """
        if not alloc.check_in_at:
            return
        checkin_day = parse_dt(alloc.check_in_at).date()
        for d, line in alloc.nights.items():
            day = parse_date(d)
            if checkin_day <= day <= through and line.state == NIGHT_HELD:
                line.state = NIGHT_STAYED
                line.billable = True

    # ------------------------------------------------------------- 日常巡检

    def run_daily_review_scan(self, actor: Actor, on_date=None):
        """爽约、超期、离店未归还：只生成挂名工单并冻结争议，绝不静默放行。"""
        self._require_role(actor, "verifier", "duty_manager")
        on = parse_date(on_date or self._now().date())
        opened = []
        with self.lock:
            for alloc in list(self.allocations.values()):
                if alloc.check_out_at:
                    continue

                # 1) 爽约：过了起始日仍无入住，冻结未使用夜（不释放，防止二次售出后无法裁决）
                if not alloc.check_in_at and on > parse_date(alloc.start):
                    held = [d for d, l in alloc.nights.items() if l.state == NIGHT_HELD]
                    if held and not self._has_open_ticket(alloc.id, REVIEW_NO_SHOW):
                        ticket = self._open_ticket(
                            REVIEW_NO_SHOW, alloc.id,
                            self.applications.get(alloc.app_id), alloc,
                            "超过入住日仍未到店，疑似爽约",
                            {"start": alloc.start, "scanned_on": iso(on), "nights": held})
                        opened.append(ticket.id)
                        for d in held:
                            alloc.nights[d].state = NIGHT_BLOCKED

                # 2) 超期：超过结束日仍在店。先把超期夜占进唯一索引，再交人工裁决，
                #    这样别的店/别的人订不到"看起来空着"的最后床位。
                if alloc.check_in_at and on > parse_date(alloc.end) and not alloc.check_out_at:
                    if not self._has_open_ticket(alloc.id, REVIEW_OVERSTAY):
                        pol = self.policies.get(alloc.policy_code)
                        conflicts, created = [], []
                        for extra in daterange(parse_date(alloc.end) + timedelta(days=1), on):
                            d = iso(extra)
                            bed_owner = self._bed_night.get(
                                (alloc.hotel_code, alloc.bed_key, d))
                            person_owner = self._person_night.get(
                                (alloc.applicant_id, d))
                            held_by = self._held_offers.get(
                                (alloc.hotel_code, alloc.bed_key, d))
                            if (bed_owner and bed_owner != alloc.id) or \
                               (person_owner and person_owner != alloc.id) or \
                               held_by:
                                # 上一张超期工单被驳回后床位已售出/被候补保留/本人已另订：
                                # 绝不覆盖唯一索引，登记冲突交值班长现场处置
                                conflicts.append({"date": d, "bed_owner": bed_owner,
                                                  "person_owner": person_owner,
                                                  "held_by_waitlist": held_by})
                                continue
                            alloc.nights[d] = NightLine(
                                date=d, state=NIGHT_OVERSTAY, billable=False,
                                compliant=False,
                                rate_locked=self.rates.rate_on(alloc.hotel_code, extra),
                                subsidy_locked=pol.subsidy_per_night,
                                policy_code=pol.code)
                            self._bed_night[(alloc.hotel_code, alloc.bed_key, d)] = alloc.id
                            self._person_night[(alloc.applicant_id, d)] = alloc.id
                            created.append(d)
                        if created:
                            alloc.end = created[-1]
                        ticket = self._open_ticket(
                            REVIEW_OVERSTAY, alloc.id,
                            self.applications.get(alloc.app_id), alloc,
                            "超过确认占房结束日仍在店，需紧急延住复核或退房"
                            + ("（部分夜床位已另有占用，需现场处置）" if conflicts else ""),
                            {"end": alloc.end, "scanned_on": iso(on),
                             "created_nights": created,
                             "bed_conflicts": conflicts})
                        opened.append(ticket.id)

                # 3) 临时离店未归还
                open_leaves = self._open_leaves(alloc)
                if open_leaves and not self._has_open_ticket(alloc.id, REVIEW_UNCLOSED_LEAVE):
                    last_until = max(l["until"] for l in open_leaves)
                    if on > last_until:
                        ticket = self._open_ticket(
                            REVIEW_UNCLOSED_LEAVE, alloc.id,
                            self.applications.get(alloc.app_id), alloc,
                            "临时离店超过预计归还日，未收到归还记录",
                            {"expected_return": iso(last_until), "scanned_on": iso(on)})
                        opened.append(ticket.id)
                        for d, line in alloc.nights.items():
                            if line.state == NIGHT_AWAY and parse_date(d) > last_until:
                                line.state = NIGHT_BLOCKED
                                line.billable = False
            return {"scanned_on": iso(on), "tickets_opened": sorted(set(opened))}

    def _open_leaves(self, alloc):
        """按事件顺序配对 temp_leave/return，返回尚未闭合的离店。"""
        leaves = []
        events = sorted((e for e in self.events.values()
                         if e.allocation_id == alloc.id and e.status == "applied"
                         and e.event_type in ("temp_leave", "return")),
                        key=lambda e: e.occurred_at)
        open_count = 0
        for e in events:
            if e.event_type == "temp_leave":
                open_count += 1
                leaves.append({"event_id": e.event_id, "until": parse_date(
                    e.payload.get("expected_return")), "open": True})
            elif e.event_type == "return" and open_count > 0:
                open_count -= 1
                for leaf in reversed(leaves):
                    if leaf["open"]:
                        leaf["open"] = False
                        break
        return [l for l in leaves if l["open"]]

    def request_emergency_extension(self, actor: Actor, allocation_id, nights, reason):
        """紧急延住只能发起申请，值班长复核通过后才追加夜数。"""
        with self.lock:
            alloc = self._load_alloc(allocation_id)
            self._require_owner_or_staff(actor, alloc)
            if not reason:
                raise ValidationError("紧急延住必须说明原因")
            pol = self.policies.get(alloc.policy_code)
            if nights > pol.emergency_extension_nights:
                raise ValidationError("单次紧急延住超出政策上限",
                                      request=nights, limit=pol.emergency_extension_nights)
            ticket = self._open_ticket(
                REVIEW_EMERGENCY_EXTENSION, alloc.id,
                self.applications.get(alloc.app_id), alloc,
                f"紧急延住 {nights} 夜申请：{reason}",
                {"nights": nights, "reason": reason,
                 "entitlement": self.entitlement(alloc.applicant_id)})
            raise ReviewRequiredError(
                "紧急延住已转人工复核", REVIEW_EMERGENCY_EXTENSION, ticket.id)

    # ------------------------------------------------------------- 人工复核裁决

    def decide_ticket(self, actor: Actor, ticket_id, decision, note="", billable=False):
        """值班长对例外工单给出唯一结论；每个工单只能裁决一次，全程留名。"""
        self._require_role(actor, "duty_manager")
        with self.lock:
            ticket = self.tickets.get(ticket_id)
            if ticket is None:
                raise NotFoundError("复核工单不存在", ticket_id=ticket_id)
            if ticket.status != TICKET_OPEN:
                raise ValidationError("工单已裁决", status=ticket.status)
            if decision not in (TICKET_APPROVED, TICKET_REJECTED):
                raise ValidationError("decision 必须是 approved/rejected")
            ticket.status = decision
            ticket.decided_at = self._now().isoformat()
            ticket.decision_note = note
            ticket.decider_id = actor.id

            alloc = self.allocations.get(ticket.allocation_id) if ticket.allocation_id else None
            app = self.applications.get(alloc.app_id) if alloc else next(
                (a for a in self.applications.values()
                 if a.applicant_id == ticket.applicant_id), None)
            handler = getattr(self, f"_decide_{ticket.type}")
            handler(ticket, app, alloc, decision, note, billable)
            if app:
                self._refresh_app_status(app)
            self._pump_waitlist()   # 裁决可能释放冻结/超期夜
            return ticket

    def _decide_eligibility_appeal(self, ticket, app, alloc, decision, note, billable):
        if decision == TICKET_APPROVED:
            pol = self.policies.get(ticket.evidence.get("policy")) or \
                self.policies.effective_at(parse_date(app.submitted_on))
            app.eligibility = policy_mod.snapshot(
                pol, app.material, parse_date(app.submitted_on)).to_dict()
            app.status = APP_ELIGIBLE
        else:
            app.status = APP_DENIED

    def _decide_no_show(self, ticket, app, alloc, decision, note, billable):
        for d, line in list(alloc.nights.items()):
            if line.state != NIGHT_BLOCKED:
                continue
            if decision == TICKET_APPROVED:
                line.state = NIGHT_NO_SHOW
                line.billable = False
                self._release_indices(alloc, d)
            else:
                line.state = NIGHT_STAYED if alloc.check_in_at else NIGHT_HELD
                line.billable = bool(billable) and alloc.check_in_at is not None
        self._trim_span(alloc)
        if decision == TICKET_REJECTED:
            # 驳回通常伴随"其实到店了"的佐证：重放冻结期间挂起的事件
            self._replay_pending_events(alloc)

    def _replay_pending_events(self, alloc):
        pending = sorted((e for e in self.events.values()
                          if e.allocation_id == alloc.id and e.status == "pending"),
                         key=lambda e: e.occurred_at)
        for event in pending:
            event.status = "applied"
            try:
                {
                    "check_in": self._apply_check_in,
                    "door_open": self._apply_door_open,
                    "temp_leave": self._apply_temp_leave,
                    "return": self._apply_return,
                    "check_out": self._apply_check_out,
                }[event.event_type](event, alloc)
            except DomainError:
                event.status = "pending"
                continue
            # 重放成功：该事件此前自动生成的事件工单一并关闭，避免挡住清算
            if event.ticket_id and event.ticket_id in self.tickets:
                auto = self.tickets[event.ticket_id]
                if auto.status == TICKET_OPEN:
                    auto.status = TICKET_APPROVED
                    auto.decision = TICKET_APPROVED
                    auto.decision_note = "随爽约/争议裁决自动确认"
                    auto.decider_id = self.duty_manager_id
                    auto.decided_at = self._now().isoformat()

    def _decide_overstay(self, ticket, app, alloc, decision, note, billable):
        for d, line in list(alloc.nights.items()):
            if line.state != NIGHT_OVERSTAY:
                continue
            if decision == TICKET_APPROVED:
                line.state = NIGHT_STAYED
                line.billable = bool(billable)
                line.compliant = True
            else:
                line.state = NIGHT_RELEASED
                line.billable = False
                self._release_indices(alloc, d)
        if decision == TICKET_APPROVED:
            # 认定延住即承认人一直没离店：入住后到结束日的 held 夜转为在店
            self._roll_forward(alloc, parse_date(alloc.end))
        self._trim_span(alloc)

    def _decide_emergency_extension(self, ticket, app, alloc, decision, note, billable):
        if decision != TICKET_APPROVED:
            return
        approved_nights = int(ticket.evidence["nights"])
        pol = self.policies.get(alloc.policy_code)
        staying = bool(alloc.check_in_at)

        # 日终扫描可能已把超期夜占位为 overstay：这些夜优先转为合规，
        # 批准夜数仍不足的部分再追加，避免同一夜出现两条记录。
        overstay_days = sorted(
            parse_date(d) for d, l in alloc.nights.items()
            if l.state == NIGHT_OVERSTAY)[:approved_nights]
        for d in overstay_days:
            line = alloc.nights[iso(d)]
            line.state = NIGHT_STAYED if staying else NIGHT_HELD
            line.billable = bool(billable) and staying
            line.compliant = True

        remaining = approved_nights - len(overstay_days)
        dates = []
        # 批准延住即承认客人一直未离店：先把入住后、原结束日前后的 held 夜结转
        self._roll_forward(alloc, parse_date(alloc.end))
        if remaining > 0:
            ent = self.entitlement(alloc.applicant_id)
            if remaining > ent["remaining_nights"]:
                raise QuotaError("延住夜数超出剩余免费住宿权益",
                                 remaining_nights=ent["remaining_nights"], request=remaining)
            dates = [parse_date(alloc.end) + timedelta(days=i)
                     for i in range(1, remaining + 1)]
            if not self._bed_available(alloc.hotel_code, alloc.bed_key, dates):
                raise RoomUnavailableError("延住期间床位已不可用")
            for d in dates:
                day = iso(d)
                alloc.nights[day] = NightLine(
                    date=day, state=NIGHT_STAYED if staying else NIGHT_HELD,
                    billable=bool(billable) and staying, compliant=True,
                    rate_locked=self.rates.rate_on(alloc.hotel_code, d),
                    subsidy_locked=pol.subsidy_per_night, policy_code=pol.code)
                self._bed_night[(alloc.hotel_code, alloc.bed_key, day)] = alloc.id
                self._person_night[(alloc.applicant_id, day)] = alloc.id
        self._trim_span(alloc)
        if dates:
            new_end = iso(dates[-1])
        elif overstay_days:
            new_end = iso(max(overstay_days))
        else:
            new_end = alloc.end
        alloc.end = new_end
        alloc.planned_end = iso(max(parse_date(alloc.planned_end or new_end),
                                   parse_date(new_end)))

        # 批准窗口之外仍挂着的超期夜不予认可：释放房间与权益
        for d, line in list(alloc.nights.items()):
            if line.state == NIGHT_OVERSTAY:
                line.state = NIGHT_RELEASED
                line.billable = False
                self._release_indices(alloc, d)

        # 同一占房挂着的超期工单一并批准（紧急延住与超期是同一件事的两面），
        # 否则夜态已合规但工单仍挂起会永远挡住清算。
        for other in self._tickets_for(alloc.id):
            if other.type == REVIEW_OVERSTAY and other.status == TICKET_OPEN:
                other.status = TICKET_APPROVED
                other.decision = TICKET_APPROVED
                other.decision_note = f"随紧急延住工单 {ticket.id} 一并批准"
                other.decider_id = ticket.decider_id
                other.decided_at = ticket.decided_at

    def _decide_event_conflict(self, ticket, app, alloc, decision, note, billable):
        event = self.events.get(ticket.evidence.get("event_id"))
        if decision == TICKET_APPROVED and event and alloc:
            event.status = "applied"
            {
                "check_in": self._apply_check_in,
                "door_open": self._apply_door_open,
                "temp_leave": self._apply_temp_leave,
                "return": self._apply_return,
                "check_out": self._apply_check_out,
            }[event.event_type](event, alloc)

    _decide_unknown_event = _decide_event_conflict

    def _decide_unclosed_leave(self, ticket, app, alloc, decision, note, billable):
        for d, line in list(alloc.nights.items()):
            if line.state != NIGHT_BLOCKED:
                continue
            if decision == TICKET_APPROVED:
                line.state = NIGHT_AWAY
                line.billable = False
                line.compliant = True
            else:
                line.state = NIGHT_RELEASED
                line.billable = False
                self._release_indices(alloc, d)
        self._trim_span(alloc)

    # ------------------------------------------------------------- 财政清算

    def settle(self, actor: Actor, allocation_id):
        """只按实际合规入住夜清算；锁定补贴价不受后续房价/政策变化影响。"""
        self._require_role(actor, "finance", "duty_manager")
        with self.lock:
            alloc = self._load_alloc(allocation_id)
            if alloc.settlement_id:
                return self.settlements[alloc.settlement_id]
            open_tickets = [t for t in self._tickets_for(alloc.id)
                            if t.status == TICKET_OPEN]
            if open_tickets:
                raise ReviewRequiredError(
                    "存在未裁决的人工工单，不能清算", "open_tickets",
                    open_tickets[0].id, tickets=[t.id for t in open_tickets])
            unresolved = [d for d, l in alloc.nights.items()
                          if l.state in (NIGHT_BLOCKED, NIGHT_OVERSTAY)]
            if unresolved:
                raise ValidationError("仍有争议/超期夜未处理，不能清算", dates=unresolved[:10])
            lines = []
            for d in sorted(alloc.nights):
                line = alloc.nights[d]
                if line.state in BILLABLE and line.compliant and line.billable:
                    lines.append({
                        "date": d, "state": line.state,
                        "rate_locked": line.rate_locked,
                        "subsidy_locked": line.subsidy_locked,
                        "subsidy": line.subsidy_locked,
                        "policy_code": line.policy_code,
                        "events": list(line.events),
                    })
            settlement = Settlement(
                id=_new_id("stl"), allocation_id=alloc.id,
                applicant_id=alloc.applicant_id, hotel_code=alloc.hotel_code,
                policy_code=alloc.policy_code, created_at=self._now().isoformat(),
                finance_id=actor.id, lines=lines, total_nights=len(lines),
                total_subsidy=round(sum(l["subsidy"] for l in lines), 2))
            self.settlements[settlement.id] = settlement
            alloc.settlement_id = settlement.id
            return settlement

    # ------------------------------------------------------------- 服务诉求

    def create_service_request(self, actor: Actor, app_id, kind, content):
        with self.lock:
            app = self._load_app(app_id)
            if actor.role == "applicant" and actor.id != app.applicant_id:
                raise PermissionError("只能就本人申请发起诉求")
            req = ServiceRequest(
                id=_new_id("req"), app_id=app_id, applicant_id=app.applicant_id,
                kind=kind, content=content, created_at=self._now().isoformat())
            self.requests[req.id] = req
            return req

    def handle_request(self, actor: Actor, request_id, message):
        """团干部处理诉求：其材料视图中不含任何非必要的求职字段。"""
        self._require_role(actor, "service_officer", "duty_manager")
        with self.lock:
            req = self.requests.get(request_id)
            if req is None:
                raise NotFoundError("服务诉求不存在", request_id=request_id)
            req.status = "handling"
            req.handler_id = actor.id
            req.handler_name = actor.name
            req.messages.append({"from": actor.id, "at": self._now().isoformat(), "text": message})
            return req

    def close_request(self, actor: Actor, request_id):
        self._require_role(actor, "service_officer", "duty_manager")
        with self.lock:
            req = self.requests[request_id]
            req.status = "closed"
            return req

    # ------------------------------------------------------------- 房态视图

    def room_board(self, actor: Actor, hotel_code, day):
        """唯一房态：每床该夜至多一个占房来源，直接读唯一索引。"""
        self._require_role(actor, "verifier", "duty_manager", "hotel_front", "finance")
        day = iso(parse_date(day))
        with self.lock:
            hotel = self.directory.get(hotel_code)
            if actor.role == "hotel_front" and not actor.can_access_hotel(hotel_code):
                raise PermissionError("无权查看该酒店")
            rows = []
            for bed in hotel.beds:
                alloc_id = self._bed_night.get((hotel_code, bed.key, day))
                alloc = self.allocations.get(alloc_id) if alloc_id else None
                line = alloc.nights.get(day) if alloc else None
                rows.append({
                    "bed_key": bed.key,
                    "operational_status": bed.status,
                    "allocation_id": alloc_id,
                    "night_state": line.state if line else None,
                    "billable": line.billable if line else None,
                    "applicant_id": alloc.applicant_id if alloc else None,
                    # 候补暂时保留：工作人员可见保留来源，但不属于正式占房
                    "held_by_waitlist": self._held_offers.get(
                        (hotel_code, bed.key, day)),
                })
            return {"hotel_code": hotel_code, "date": day, "beds": rows}

    def availability(self, hotel_code, day):
        """任意角色可查的空房数（不含占用人信息）；候补暂时保留同样不计空房。"""
        day = iso(parse_date(day))
        with self.lock:
            hotel = self.directory.get(hotel_code)
            free = sum(1 for b in hotel.beds
                       if b.status != BED_BLOCKED
                       and (hotel_code, b.key, day) not in self._bed_night
                       and (hotel_code, b.key, day) not in self._held_offers)
            return {"hotel_code": hotel_code, "date": day,
                    "total_beds": hotel.bed_count(), "free_beds": free}

    def daily_subsidy_basis(self, actor: Actor, hotel_code, day):
        """逐日补贴依据：该夜每个占用的状态、合规性、锁定房价与补贴单价。"""
        self._require_role(actor, "finance", "verifier", "duty_manager")
        day = iso(parse_date(day))
        with self.lock:
            basis = []
            for alloc in self.allocations.values():
                if alloc.hotel_code != hotel_code:
                    continue
                line = alloc.nights.get(day)
                if line is None:
                    continue
                basis.append({
                    "allocation_id": alloc.id,
                    "applicant_id": alloc.applicant_id,
                    "bed_key": alloc.bed_key,
                    "state": line.state,
                    "compliant": line.compliant,
                    "billable": line.billable,
                    "rate_locked": line.rate_locked,
                    "subsidy_locked": line.subsidy_locked,
                    "policy_code": line.policy_code,
                    "settlement_id": alloc.settlement_id,
                })
            return {"hotel_code": hotel_code, "date": day, "lines": basis}

    # ------------------------------------------------------------- 快照与恢复

    def snapshot_state(self):
        """导出全部业务状态为可 JSON 化字典（重启/迁移边界）。

        政策、酒店目录、房价与时钟属于外部依赖，不进快照——恢复时重新注入；
        候补的晋位顺序（rank_key/register_seq）与保留截止时间（confirm_deadline）
        随业务状态原样持久化。
        """
        from dataclasses import asdict
        with self.lock:
            return {
                "duty_manager_id": self.duty_manager_id,
                "users": {k: asdict(v) for k, v in self.users.items()},
                "applications": {k: asdict(v) for k, v in self.applications.items()},
                "allocations": {k: asdict(v) for k, v in self.allocations.items()},
                "events": {k: asdict(v) for k, v in self.events.items()},
                "tickets": {k: asdict(v) for k, v in self.tickets.items()},
                "requests": {k: asdict(v) for k, v in self.requests.items()},
                "settlements": {k: asdict(v) for k, v in self.settlements.items()},
                "bed_night": [[*k, v] for k, v in self._bed_night.items()],
                "person_night": [[*k, v] for k, v in self._person_night.items()],
                "fingerprints": [[*k, v] for k, v in self._fingerprints.items()],
                "waitlist": self.export_waitlist(),
            }

    def restore_state(self, data):
        """从快照恢复业务状态；顺序索引与保留期限不重算。"""
        from .models import (Actor, Application, Allocation, EventRecord,
                             NightLine, ReviewTicket, ServiceRequest, Settlement)
        with self.lock:
            self.users = {k: Actor(**v) for k, v in data.get("users", {}).items()}
            self.duty_manager_id = data.get("duty_manager_id")
            self.applications = {k: Application(**v)
                                 for k, v in data.get("applications", {}).items()}
            self.allocations = {}
            for k, raw in data.get("allocations", {}).items():
                raw = dict(raw)
                raw["nights"] = {d: NightLine(**line)
                                 for d, line in (raw.get("nights") or {}).items()}
                self.allocations[k] = Allocation(**raw)
            self.events = {k: EventRecord(**v)
                           for k, v in data.get("events", {}).items()}
            self.tickets = {k: ReviewTicket(**v)
                            for k, v in data.get("tickets", {}).items()}
            self.requests = {k: ServiceRequest(**v)
                             for k, v in data.get("requests", {}).items()}
            self.settlements = {k: Settlement(**v)
                                for k, v in data.get("settlements", {}).items()}
            self._bed_night = {(h, b, d): v
                               for h, b, d, v in data.get("bed_night", [])}
            self._person_night = {(p, d): v
                                  for p, d, v in data.get("person_night", [])}
            self._fingerprints = {tuple(k[:-1]): k[-1]
                                  for k in data.get("fingerprints", [])}
            if "waitlist" in data:
                self.import_waitlist(data["waitlist"])
        return self

    # ------------------------------------------------------------------ 内部

    def _load_app(self, app_id):
        app = self.applications.get(app_id)
        if app is None:
            raise NotFoundError("申请不存在", app_id=app_id)
        return app

    def _load_alloc(self, alloc_id):
        alloc = self.allocations.get(alloc_id)
        if alloc is None:
            raise NotFoundError("占房记录不存在", allocation_id=alloc_id)
        return alloc

    def _require_owner_or_staff(self, actor, alloc):
        if actor.role not in ("applicant", "verifier", "duty_manager", "hotel_front"):
            raise PermissionError("无权操作占房", role=actor.role)
        if actor.role == "applicant" and actor.id != alloc.applicant_id:
            raise PermissionError("只能操作本人占房")
        if actor.role in ("verifier", "hotel_front") and not actor.can_access_hotel(alloc.hotel_code):
            raise PermissionError("无权操作该酒店")

    def _applicant_allocations(self, applicant_id):
        return [a for a in self.allocations.values() if a.applicant_id == applicant_id]

    def _bed_available(self, hotel_code, bed_key, dates):
        """正式占房视角的可用性：唯一占用索引与候补持有索引都为空才可用。"""
        return all((hotel_code, bed_key, iso(d)) not in self._bed_night
                   and (hotel_code, bed_key, iso(d)) not in self._held_offers
                   for d in dates)

    def _person_overlap(self, applicant_id, dates, exclude=None):
        return [iso(d) for d in dates
                if self._person_night.get((applicant_id, iso(d))) not in (None, exclude)]

    def _release_indices(self, alloc, day):
        if self._bed_night.get((alloc.hotel_code, alloc.bed_key, day)) == alloc.id:
            del self._bed_night[(alloc.hotel_code, alloc.bed_key, day)]
        if self._person_night.get((alloc.applicant_id, day)) == alloc.id:
            del self._person_night[(alloc.applicant_id, day)]

    def _trim_span(self, alloc):
        """释放后收窄 end 到仍占用或已在店的最后一夜（区间仅作展示）。"""
        kept = sorted(d for d, l in alloc.nights.items()
                      if l.state in OCCUPYING or l.state in BILLABLE)
        if kept:
            alloc.end = kept[-1]

    def _resolve_allocation(self, hotel_code, bed_key, day, applicant_id,
                            etype=None):
        days = [day]
        if etype == "check_out":
            # 退房通常发生在最后一夜之后的上午：允许向前回看一夜
            days.append(day - timedelta(days=1))
        for d in days:
            hit = self._resolve_on(hotel_code, bed_key, d, applicant_id)
            if hit is not None:
                return hit
        return None

    def _resolve_on(self, hotel_code, bed_key, day, applicant_id):
        if applicant_id:
            for alloc in self.allocations.values():
                if alloc.applicant_id == applicant_id and iso(day) in alloc.nights:
                    return alloc
        if bed_key:
            alloc_id = self._bed_night.get((hotel_code, bed_key, iso(day)))
            if alloc_id:
                return self.allocations[alloc_id]
        candidates = [a for a in self.allocations.values()
                      if a.hotel_code == hotel_code and iso(day) in a.nights
                      and a.nights[iso(day)].state in OCCUPYING]
        return candidates[0] if len(candidates) == 1 else None

    def _semantic_fp(self, hotel_code, bed_key, etype, payload, occurred):
        who = bed_key or payload.get("applicant_id")
        if not who:
            return None
        bucket = occurred.replace(second=0, microsecond=0,
                                  minute=occurred.minute - occurred.minute % 5)
        return (hotel_code, who, etype, bucket.isoformat())

    def _find_fingerprint(self, fp, occurred):
        hit = self._fingerprints.get(fp)
        if hit:
            return hit
        _, who, etype, bucket_iso = fp
        bucket = parse_dt(bucket_iso)
        for (h, w, t, b_iso), eid in self._fingerprints.items():
            if h == fp[0] and w == who and t == etype and abs(
                    (parse_dt(b_iso) - bucket).total_seconds()) <= SEMANTIC_DEDUP_MINUTES * 60:
                return eid
        return None

    def _open_ticket(self, ttype, alloc_id, app, alloc, summary, evidence=None,
                     hotel_code=None):
        dm = self._duty_manager()
        if alloc is None and alloc_id:
            alloc = self.allocations.get(alloc_id)
        ticket = ReviewTicket(
            id=_new_id("tkt"), type=ttype,
            hotel_code=hotel_code or (alloc.hotel_code if alloc else "—"),
            applicant_id=(alloc.applicant_id if alloc else
                          (app.applicant_id if app else "—")),
            allocation_id=alloc.id if alloc else alloc_id,
            created_at=self._now().isoformat(), owner_id=dm.id, owner_name=dm.name,
            summary=summary, evidence=evidence or {})
        self.tickets[ticket.id] = ticket
        if app is not None and ticket.id not in app.tickets:
            app.tickets.append(ticket.id)
        return ticket

    def _pend_event(self, event, alloc_id, ttype, hotel_code, summary, extra=None):
        alloc = self.allocations.get(alloc_id) if alloc_id else None
        app = self.applications.get(alloc.app_id) if alloc else None
        ticket = self._open_ticket(
            ttype, alloc_id, app, alloc, summary,
            {"event_id": event.event_id, **(extra or {})}, hotel_code=hotel_code)
        event.status = "pending"
        event.ticket_id = ticket.id
        return ticket

    def _has_open_ticket(self, alloc_id, ttype):
        return any(t.allocation_id == alloc_id and t.type == ttype
                   and t.status == TICKET_OPEN for t in self.tickets.values())

    def _tickets_for(self, alloc_id):
        return [t for t in self.tickets.values() if t.allocation_id == alloc_id]

    def _refresh_app_status(self, app):
        if app.status != APP_IN_REVIEW:
            return
        app.status = APP_ELIGIBLE if app.eligibility else APP_DENIED
