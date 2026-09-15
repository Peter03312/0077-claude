"""领域错误：事件被投影/裁决拒绝时抛出。"""

from __future__ import annotations


class RejectEvent(Exception):
    """事件不合法（不满足投影不变量或无适用规则版本）。

    ``status`` 区分两类调用方语义：
    * 422 —— 追加时载荷本身违反领域约束；
    * 409 —— 序号冲突、幂等键冲突、更正重放导致后续引用失效。
    """

    def __init__(self, message: str, status: int = 422, code: str = "rejected"):
        super().__init__(message)
        self.status = status
        self.code = code


class Conflict(RejectEvent):
    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message, status=409, code=code)


class Forbidden(RejectEvent):
    def __init__(self, message: str, code: str = "forbidden"):
        super().__init__(message, status=403, code=code)
