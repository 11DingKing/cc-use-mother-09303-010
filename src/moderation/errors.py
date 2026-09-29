"""领域错误类型。"""
from __future__ import annotations


class ModerationError(Exception):
    """所有领域错误的基类。"""


class ValidationError(ModerationError):
    """输入不满足领域约束，映射为 400。"""


class NotFoundError(ModerationError):
    """资源不存在，映射为 404。"""


class ConflictError(ModerationError):
    """状态冲突或违反不变量，映射为 409。"""
