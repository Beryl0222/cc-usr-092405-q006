"""实体到可 JSON 化字典的转换。"""

from datetime import datetime

from . import models


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def dto(obj):
    if isinstance(obj, dict):
        return {k: dto(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [dto(v) for v in obj]
    return _jsonable(obj)


def application_dict(app: "models.Application"):
    return {
        "id": app.id,
        "applicant_id": app.applicant_id,
        "submitted_on": app.submitted_on,
        "policy_code": app.policy_code,
        "status": app.status,
        "eligibility": app.eligibility,
        "tickets": list(app.tickets),
        "withdrawn_at": dto(app.withdrawn_at),
    }


def allocation_dict(alloc: "models.Allocation", include_nights=True):
    out = {
        "id": alloc.id,
        "app_id": alloc.app_id,
        "applicant_id": alloc.applicant_id,
        "hotel_code": alloc.hotel_code,
        "bed_key": alloc.bed_key,
        "start": alloc.start,
        "end": alloc.end,
        "planned_end": alloc.planned_end,
        "policy_code": alloc.policy_code,
        "chain_id": alloc.chain_id,
        "created_from": alloc.created_from,
        "created_at": dto(alloc.created_at),
        "check_in_at": dto(alloc.check_in_at),
        "check_out_at": dto(alloc.check_out_at),
        "settlement_id": alloc.settlement_id,
    }
    if include_nights:
        out["nights"] = {d: line.to_dict() for d, line in sorted(alloc.nights.items())}
    return out


def ticket_dict(t: "models.ReviewTicket"):
    return {
        "id": t.id,
        "type": t.type,
        "hotel_code": t.hotel_code,
        "applicant_id": t.applicant_id,
        "allocation_id": t.allocation_id,
        "status": t.status,
        "summary": t.summary,
        "evidence": dto(t.evidence),
        "owner_id": t.owner_id,
        "owner_name": t.owner_name,
        "created_at": dto(t.created_at),
        "decided_at": dto(t.decided_at),
        "decision": t.decision,
        "decision_note": t.decision_note,
        "decider_id": t.decider_id,
    }


def event_dict(e: "models.EventRecord"):
    return {
        "event_id": e.event_id,
        "source": e.source,
        "hotel_code": e.hotel_code,
        "bed_key": e.bed_key,
        "event_type": e.event_type,
        "occurred_at": e.occurred_at,
        "recorded_at": e.recorded_at,
        "allocation_id": e.allocation_id,
        "applicant_id": e.applicant_id,
        "payload": e.payload,
        "status": e.status,
        "duplicate_of": e.duplicate_of,
        "ticket_id": e.ticket_id,
        "offline": e.offline,
    }


def request_dict(r: "models.ServiceRequest"):
    return {
        "id": r.id,
        "app_id": r.app_id,
        "applicant_id": r.applicant_id,
        "kind": r.kind,
        "content": r.content,
        "status": r.status,
        "handler_id": r.handler_id,
        "handler_name": r.handler_name,
        "created_at": dto(r.created_at),
        "messages": dto(r.messages),
    }


def settlement_dict(s: "models.Settlement"):
    return {
        "id": s.id,
        "allocation_id": s.allocation_id,
        "applicant_id": s.applicant_id,
        "hotel_code": s.hotel_code,
        "policy_code": s.policy_code,
        "created_at": dto(s.created_at),
        "finance_id": s.finance_id,
        "lines": dto(s.lines),
        "total_nights": s.total_nights,
        "total_subsidy": s.total_subsidy,
    }
