"""领域异常：错误码稳定，供 HTTP 层映射与前端区分处理。"""


class DomainError(Exception):
    """所有可预期的业务拒绝都继承本异常。"""

    code = "DOMAIN_ERROR"
    http_status = 400

    def __init__(self, message, **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self):
        payload = {"code": self.code, "message": self.message}
        if self.context:
            payload["context"] = self.context
        return payload


class ValidationError(DomainError):
    code = "VALIDATION_ERROR"
    http_status = 400


class PolicyError(DomainError):
    """资格不满足。可能可申诉（临界规则），也可能硬性不满足。"""

    code = "POLICY_DENIED"
    http_status = 422

    def __init__(self, message, reasons=None, appealable=False, **context):
        super().__init__(message, **context)
        self.reasons = reasons or []
        self.appealable = appealable

    def to_dict(self):
        payload = super().to_dict()
        payload["reasons"] = self.reasons
        payload["appealable"] = self.appealable
        return payload


class QuotaError(DomainError):
    code = "QUOTA_EXHAUSTED"
    http_status = 409


class RoomUnavailableError(DomainError):
    code = "ROOM_UNAVAILABLE"
    http_status = 409


class BookingConflictError(DomainError):
    """同一位申请人的日期与既有占房重叠（跨店重复占房）。"""

    code = "BOOKING_CONFLICT"
    http_status = 409


class WaitlistConflictError(DomainError):
    """候补与既有住宿、申诉冻结或服务工单冲突，不允许入队/晋位。"""

    code = "WAITLIST_CONFLICT"
    http_status = 409


class WaitlistStateError(DomainError):
    """候补条目/保留方案当前状态不允许该操作（重复确认、已终局等）。"""

    code = "WAITLIST_STATE"
    http_status = 409


class NotFoundError(DomainError):
    code = "NOT_FOUND"
    http_status = 404


class EventConflictError(DomainError):
    """离线补传事件与已有事件冲突（非简单重复），需人工复核。"""

    code = "EVENT_CONFLICT"
    http_status = 409


class ReviewRequiredError(DomainError):
    """系统拒绝静默放行，转交人工复核时抛出。"""

    code = "REVIEW_REQUIRED"
    http_status = 202

    def __init__(self, message, review_type=None, ticket_id=None, **context):
        super().__init__(message, **context)
        self.review_type = review_type
        self.ticket_id = ticket_id

    def to_dict(self):
        payload = super().to_dict()
        payload["review_type"] = self.review_type
        if self.ticket_id:
            payload["ticket_id"] = self.ticket_id
        return payload


class PermissionError(DomainError):
    code = "PERMISSION_DENIED"
    http_status = 403
