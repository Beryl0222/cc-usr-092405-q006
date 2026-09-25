"""HTTP 路由：把 JSON 请求映射到领域系统，把领域异常映射为稳定错误体。

路由层不做业务判断，只负责：身份解析、参数形状校验、序列化。
"""

from urllib.parse import parse_qs, urlparse

from .errors import DomainError, NotFoundError, PermissionError, ValidationError
from .serialization import (allocation_dict, application_dict, dto, request_dict,
                            settlement_dict, ticket_dict)


class Api:
    """无状态路由表；每个方法返回 (status, payload)。"""

    def __init__(self, system):
        self.system = system

    # ------------------------------------------------------------ 基础工具

    def _actor(self, headers):
        actor_id = headers.get("x-actor-id") or headers.get("X-Actor-Id")
        if not actor_id:
            raise PermissionError("缺少 X-Actor-Id 头")
        return self.system.actor(actor_id)

    def dispatch(self, method, raw_path, headers, body):
        path = urlparse(raw_path).path
        query = {k: v[0] for k, v in parse_qs(urlparse(raw_path).query).items()}
        body = body or {}
        segments = [s for s in path.split("/") if s]
        if not segments or segments[0] != "api":
            raise NotFoundError("未知路由", path=path)
        key = (method, tuple(segments[1:]))
        handler = self._match(key)
        if handler is None:
            raise NotFoundError("未知路由", path=path)
        return handler(self._actor(headers), segments[1:], query, body)

    def _match(self, key):
        method, segs = key
        table = {
            ("GET", ("policies",)): self._policies,
            ("GET", ("hotels",)): self._hotels,
            ("GET", ("hotels", "*", "board")): self._room_board,
            ("GET", ("hotels", "*", "subsidy-basis")): self._subsidy_basis,
            ("POST", ("applications",)): self._submit_application,
            ("GET", ("applications", "*")): self._get_application,
            ("GET", ("applications", "*", "material")): self._view_material,
            ("POST", ("applications", "*", "bookings")): self._confirm_booking,
            ("POST", ("applications", "*", "requests")): self._create_request,
            ("POST", ("allocations", "*", "withdraw")): self._withdraw,
            ("POST", ("allocations", "*", "cancel")): self._cancel,
            ("POST", ("allocations", "*", "transfer")): self._transfer,
            ("POST", ("allocations", "*", "emergency-extension")): self._emergency_extension,
            ("POST", ("allocations", "*", "settle")): self._settle,
            ("GET", ("allocations", "*")): self._get_allocation,
            ("POST", ("applications", "*", "waitlist")): self._register_waitlist,
            ("GET", ("waitlist",)): self._waitlist_queue,
            ("GET", ("waitlist", "mine")): self._waitlist_mine,
            ("POST", ("waitlist", "advance")): self._waitlist_advance,
            ("POST", ("waitlist", "expire")): self._waitlist_expire,
            ("GET", ("waitlist", "*")): self._waitlist_entry,
            ("POST", ("waitlist", "*", "confirm")): self._waitlist_confirm,
            ("POST", ("waitlist", "*", "reject")): self._waitlist_reject,
            ("POST", ("waitlist", "*", "cancel")): self._waitlist_cancel,
            ("POST", ("events",)): self._ingest_event,
            ("POST", ("events", "batch")): self._ingest_events,
            ("POST", ("review-scans",)): self._review_scan,
            ("GET", ("tickets",)): self._list_tickets,
            ("POST", ("tickets", "*", "decisions")): self._decide_ticket,
            ("GET", ("entitlements", "*")): self._entitlement,
            ("GET", ("requests",)): self._list_requests,
            ("GET", ("requests", "*")): self._get_request,
            ("POST", ("requests", "*", "messages")): self._handle_request,
            ("POST", ("requests", "*", "close")): self._close_request,
        }
        matches = []
        for (m, pattern), fn in table.items():
            if m != method or len(pattern) != len(segs):
                continue
            if all(p == "*" or p == s for p, s in zip(pattern, segs)):
                matches.append((pattern.count("*"), fn))
        if not matches:
            return None
        # 字面量片段优先于通配符（如 waitlist/mine 优先于 waitlist/*）
        matches.sort(key=lambda item: item[0])
        return matches[0][1]

    # ------------------------------------------------------------ 查询

    def _policies(self, actor, segs, query, body):
        return 200, {"policies": [p.to_dict() for p in self.system.policies.list()]}

    def _hotels(self, actor, segs, query, body):
        day = query.get("date")
        hotels = []
        for h in self.system.directory.list():
            item = {"code": h.code, "name": h.name, "district": h.district,
                    "address": h.address, "front_phone": h.front_phone,
                    "beds": h.bed_count()}
            if day:
                item["free_beds"] = self.system.availability(h.code, day)["free_beds"]
            hotels.append(item)
        return 200, {"hotels": hotels}

    def _room_board(self, actor, segs, query, body):
        day = query.get("date")
        if not day:
            raise ValidationError("缺少 date 参数")
        return 200, self.system.room_board(actor, segs[1], day)

    def _subsidy_basis(self, actor, segs, query, body):
        day = query.get("date")
        if not day:
            raise ValidationError("缺少 date 参数")
        return 200, self.system.daily_subsidy_basis(actor, segs[1], day)

    def _get_application(self, actor, segs, query, body):
        return 200, application_dict(self.system.get_application(actor, segs[1]))

    def _view_material(self, actor, segs, query, body):
        return 200, {"app_id": segs[1],
                     "material": self.system.view_material(actor, segs[1]),
                     "viewer_role": actor.role}

    def _get_allocation(self, actor, segs, query, body):
        alloc = self.system.allocations.get(segs[1])
        if alloc is None:
            raise NotFoundError("占房记录不存在", allocation_id=segs[1])
        if actor.role == "applicant" and actor.id != alloc.applicant_id:
            raise PermissionError("只能查看本人占房")
        return 200, allocation_dict(alloc)

    def _entitlement(self, actor, segs, query, body):
        applicant_id = segs[1]
        if actor.role == "applicant" and actor.id != applicant_id:
            raise PermissionError("只能查看本人权益")
        return 200, self.system.entitlement(applicant_id)

    def _list_tickets(self, actor, segs, query, body):
        status = query.get("status")
        tickets = [t for t in self.system.tickets.values()
                   if status is None or t.status == status]
        if actor.role == "duty_manager":
            pass
        elif actor.role in ("verifier", "finance"):
            pass
        else:
            raise PermissionError("无权查看复核工单")
        return 200, {"tickets": [ticket_dict(t) for t in
                                 sorted(tickets, key=lambda t: t.created_at)]}

    def _list_requests(self, actor, segs, query, body):
        status = query.get("status")
        if actor.role not in ("service_officer", "duty_manager", "verifier"):
            raise PermissionError("无权查看服务诉求列表")
        reqs = [r for r in self.system.requests.values()
                if status is None or r.status == status]
        return 200, {"requests": [request_dict(r) for r in
                                  sorted(reqs, key=lambda r: r.created_at)]}

    def _get_request(self, actor, segs, query, body):
        req = self.system.requests.get(segs[1])
        if req is None:
            raise NotFoundError("服务诉求不存在", request_id=segs[1])
        if actor.role == "applicant" and actor.id != req.applicant_id:
            raise PermissionError("只能查看本人诉求")
        return 200, request_dict(req)

    # ------------------------------------------------------------ 命令

    def _submit_application(self, actor, segs, query, body):
        material = dict(body.get("material") or {})
        material.setdefault("applicant_id", actor.id)
        app = self.system.submit_application(actor, material)
        return 201, application_dict(app)

    def _confirm_booking(self, actor, segs, query, body):
        alloc = self.system.confirm_booking(
            actor, segs[1], body["hotel_code"], body["start"], body["end"],
            body.get("bed_key"))
        return 201, allocation_dict(alloc)

    def _withdraw(self, actor, segs, query, body):
        return 200, self.system.withdraw_dates(actor, segs[1], body["dates"])

    def _cancel(self, actor, segs, query, body):
        return 200, self.system.cancel_booking(actor, segs[1])

    def _transfer(self, actor, segs, query, body):
        result = self.system.transfer(
            actor, segs[1], body["to_hotel_code"], body["on_date"],
            body.get("new_end"), body.get("bed_key"))
        return 200, {"closed_partial": result["closed_partial"],
                     "released": result["released"],
                     "late_nights_ticket": result["late_nights_ticket"],
                     "new_allocation": allocation_dict(result["new_allocation"])}

    def _emergency_extension(self, actor, segs, query, body):
        # 正常路径必抛 REVIEW_REQUIRED（由全局映射为 202）；此处仅为防御性返回
        self.system.request_emergency_extension(
            actor, segs[1], int(body["nights"]), body.get("reason", ""))
        return 202, {"status": "pending_review"}

    def _settle(self, actor, segs, query, body):
        return 200, settlement_dict(self.system.settle(actor, segs[1]))

    def _ingest_event(self, actor, segs, query, body):
        result = self.system.ingest_event(actor, body)
        status = 200 if result.get("status") in ("applied", "duplicate") else 202
        return status, result

    def _ingest_events(self, actor, segs, query, body):
        events = body.get("events")
        if not isinstance(events, list):
            raise ValidationError("events 必须是数组")
        return 200, {"results": self.system.ingest_events(actor, events)}

    def _review_scan(self, actor, segs, query, body):
        return 200, self.system.run_daily_review_scan(actor, body.get("on_date"))

    def _decide_ticket(self, actor, segs, query, body):
        ticket = self.system.decide_ticket(
            actor, segs[1], body["decision"], body.get("note", ""),
            bool(body.get("billable", False)))
        return 200, ticket_dict(ticket)

    def _create_request(self, actor, segs, query, body):
        req = self.system.create_service_request(
            actor, segs[1], body.get("kind", "consultation"), body.get("content", ""))
        return 201, request_dict(req)

    def _handle_request(self, actor, segs, query, body):
        return 200, request_dict(
            self.system.handle_request(actor, segs[1], body.get("message", "")))

    def _close_request(self, actor, segs, query, body):
        return 200, request_dict(self.system.close_request(actor, segs[1]))

    # ------------------------------------------------------------ 候补队列

    def _register_waitlist(self, actor, segs, query, body):
        entry = self.system.register_waitlist(
            actor, segs[1], body["start"], body["end"], body.get("hotels"),
            body.get("latest_confirm_at"), body.get("hold_minutes"),
            body.get("bed_key"), body.get("note", ""))
        return 201, self.system._entry_view(entry)

    def _waitlist_queue(self, actor, segs, query, body):
        status = query.get("status", "waiting")
        payload = self.system.waitlist_queue(
            actor, query.get("hotel"), status,
            include_offered=query.get("include_offered", "1") != "0")
        return 200, payload

    def _waitlist_mine(self, actor, segs, query, body):
        return 200, self.system.applicant_waitlist(actor)

    def _waitlist_entry(self, actor, segs, query, body):
        entry = self.system.get_waitlist_entry(segs[1])
        if actor.role == "applicant" and actor.id != entry.applicant_id:
            raise PermissionError("只能查看本人候补")
        view = self.system._entry_view(entry)
        if entry.status == "offered" and entry.offer_id:
            view["offer"] = self.system._offer_view(self.system.offers[entry.offer_id])
        return 200, view

    def _waitlist_advance(self, actor, segs, query, body):
        limit = body.get("limit", 1)
        return 200, self.system.advance_waitlist(
            actor, body.get("hotel_code"), body.get("on_date"),
            int(limit) if limit is not None else None)

    def _waitlist_expire(self, actor, segs, query, body):
        return 200, self.system.expire_waitlist(actor, body.get("offer_id"))

    def _waitlist_confirm(self, actor, segs, query, body):
        result = self.system.confirm_waitlist_offer(
            actor, entry_id=segs[1], request_id=body.get("request_id"))
        result["offer"] = result["offer"]
        result["allocation"] = allocation_dict(result["allocation"])
        return 200, result

    def _waitlist_reject(self, actor, segs, query, body):
        return 200, dto(self.system.reject_waitlist_offer(
            actor, entry_id=segs[1], note=body.get("note", "申请人拒绝")))

    def _waitlist_cancel(self, actor, segs, query, body):
        return 200, dto(self.system.cancel_waitlist(
            actor, segs[1], body.get("note", "申请人撤下")))


def error_payload(exc):
    if isinstance(exc, DomainError):
        return exc.http_status, {"error": exc.to_dict()}
    return 500, {"error": {"code": "INTERNAL", "message": str(exc)}}
