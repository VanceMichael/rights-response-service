"""领域错误与 HTTP 状态映射。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务规则错误的基类。"""

    status = 400

    def __init__(self, message: str, *, code: str | None = None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__
        self.details = details

    def to_body(self) -> dict:
        body = {"error": self.message, "code": self.code}
        if self.details is not None:
            body["details"] = self.details
        return body


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class ValidationFailed(DomainError):
    status = 422


class Forbidden(DomainError):
    status = 403


class IncompleteMaterials(ValidationFailed):
    """材料不全，需补件，不进入核准。"""


class ScopeOutOfMandate(ValidationFailed):
    """申请范围超出有效委托的地区/媒介/时间。"""


class PlatformCapability(Conflict):
    """平台协议不支持所申请的限制粒度，须保留待协商。"""


class StateConflict(Conflict):
    """当前状态不允许该操作（如重复送达、已结束异议重新计时）。"""


class UnknownDelivery(Conflict):
    """平台受理后结果不明，只能继续查询原流水，不能另发相反请求。"""
