"""领域错误。HTTP 层据此映射状态码。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400


class ValidationError(DomainError):
    """请求参数不合法。"""

    status = 400


class NotFoundError(DomainError):
    """引用的资源不存在。"""

    status = 404


class ConflictError(DomainError):
    """操作与当前批次状态或领域不变量冲突。"""

    status = 409
