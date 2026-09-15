"""请求与载荷模型。

事件统一信封（见 POST /api/events）：

    {
      "type": "cut",                         # 事件类型
      "submitted_by": "m1",                  # 原提交者（更正后也不变）
      "payload": { ... },                    # 类型相关的完整载荷
      "expected_next_seq": 7                 # 乐观并发：期望分配到的序号
    }

规则 ``when`` 为条件合取，字段缺省/为空表示该项不加约束（即“空为真”）：

* ``event_types``  命中的事件类型集合，空列表表示任意类型
* ``operator_identities``  操作者相对布料的身份集合：owner / holder / other，
  空列表表示任意身份
* ``authorized``  动作是否存在有效授权；None 表示不检查

规则 ``effects`` 为两列：责任成员 ID 列与下一动作 ID 列。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---- 规则子结构 -------------------------------------------------------------


class WhenCond(BaseModel):
    event_types: list[str] = Field(default_factory=list)
    operator_identities: list[Literal["owner", "holder", "other"]] = Field(
        default_factory=list
    )
    authorized: Optional[bool] = None


class Effects(BaseModel):
    responsible_member_ids: list[str] = Field(default_factory=list)
    next_action_ids: list[str] = Field(default_factory=list)


# ---- 事件载荷（10 类） -------------------------------------------------------


class FabricDefinePayload(BaseModel):
    """登记布料：含主人；初始持有人即主人。"""

    fabric_id: str
    owner_id: str


class MemberDefinePayload(BaseModel):
    """登记成员。"""

    member_id: str
    age: Optional[int] = None
    gender: Optional[str] = None


class RuleDefinePayload(BaseModel):
    """登记 / 改版规则：同一 rule_id 的新事件即新版本。"""

    rule_id: str
    priority: int = Field(..., description="小者优先")
    when: WhenCond
    effects: Effects


class GrantPayload(BaseModel):
    """主人授权某成员执行某动作（动作 id 自由命名，如 cut/transfer/return）。"""

    fabric_id: str
    grantee_id: str
    action_id: str


class CutPayload(BaseModel):
    """剪裁布料。"""

    fabric_id: str


class TransferPayload(BaseModel):
    """转交：更换持有人。"""

    fabric_id: str
    to_member_id: str


class ReturnPayload(BaseModel):
    """返还：持有人恢复为主人。"""

    fabric_id: str


class WithdrawPayload(BaseModel):
    """撤回：引用一条授权，自本事件序号起失效。"""

    fabric_id: str
    grant_seq: int = Field(..., description="被撤回的 grant 事件序号")


class CorrectPayload(BaseModel):
    """更正：限原提交者，携带完整替代载荷（不携带替代信封）。"""

    target_seq: int
    new_type: str
    new_payload: dict


class AppealPayload(BaseModel):
    """申诉：引用某条裁决，依本序号规则追加补救，不改旧裁决。"""

    target_seq: int
    fabric_id: str


# 按事件类型到载荷模型的映射
PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "fabric_define": FabricDefinePayload,
    "member_define": MemberDefinePayload,
    "rule_define": RuleDefinePayload,
    "grant": GrantPayload,
    "cut": CutPayload,
    "transfer": TransferPayload,
    "return": ReturnPayload,
    "withdraw": WithdrawPayload,
    "correct": CorrectPayload,
    "appeal": AppealPayload,
}

# 需要“按此前投影裁决”的动作类事件（setup 类不裁决）
ADJUDICATED_TYPES = frozenset(
    {"grant", "cut", "transfer", "return", "withdraw", "appeal"}
)


class EventIn(BaseModel):
    type: str
    submitted_by: str = Field(..., min_length=1)
    payload: dict
    expected_next_seq: int = Field(..., ge=1)


def validate_payload(event_type: str, payload: dict) -> BaseModel:
    model = PAYLOAD_MODELS.get(event_type)
    if model is None:
        raise ValueError(f"未知事件类型: {event_type}")
    return model.model_validate(payload)
