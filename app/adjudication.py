"""规则裁决模块。

裁决只依赖“此前投影”：调用方在应用事件 *之前* 调用 :func:`adjudicate`。

* 事件只取 ``version_seq <= event_seq`` 的规则版本——由 fold 推进规则表天然保证，
  后定义的规则版本不会出现在 ``state.rules`` 中，因此**后版不得重算旧事件**。
* ``when`` 为条件合取：事件类型、操作者身份（owner/holder/other，可多重命中）、
  是否存在有效授权；空集合 / None 表示该项恒真。
* priority 小者优先；同级命中规则的两列 effects 各求并集后排序；无命中为空。
* 若裁决时刻没有任何规则版本，事件被拒绝（无适用版本）。
"""

from __future__ import annotations

from typing import Any

from .errors import RejectEvent


def _identity_set(actor: str, fabric: Any) -> set[str]:
    """操作者相对布料的身份集合（主人与持有人可同时命中）。"""
    ids: set[str] = set()
    if actor == fabric.owner_id:
        ids.add("owner")
    if actor == fabric.holder_id:
        ids.add("holder")
    if not ids:
        ids.add("other")
    return ids


def _clause_match(rule: Any, event_type: str, identities: set[str], authorized: bool):
    when = rule.when
    type_ok = (not when.get("event_types")) or event_type in when["event_types"]
    wanted_ids = when.get("operator_identities") or []
    identity_ok = (not wanted_ids) or bool(identities.intersection(wanted_ids))
    auth_flag = when.get("authorized")
    auth_ok = auth_flag is None or bool(auth_flag) == bool(authorized)
    return type_ok, identity_ok, auth_ok


def adjudicate(state: Any, seq: int, event_type: str, actor: str, fabric_id: str) -> dict:
    """按当前 state.rules 裁决，返回裁决书（含逐条依据）。不修改 state。"""
    fabric = state.fabrics.get(fabric_id)
    if fabric is None:
        raise RejectEvent(f"序号 {seq}: 布料 {fabric_id} 不存在", 422, "fabric_not_found")

    if not state.rules:
        raise RejectEvent(
            f"序号 {seq}: 无适用规则版本（不晚于本序号没有任何规则）",
            422,
            "no_applicable_rule",
        )

    identities = _identity_set(actor, fabric)
    authorized = state.has_valid_grant(fabric_id, actor, event_type)

    # 版本顺序仅用于依据的确定性展示：按 (版本序号, rule_id)
    versions = sorted(state.rules.values(), key=lambda r: (r.seq, r.rule_id))

    basis: list[dict] = []
    matched: list[Any] = []
    for rv in versions:
        type_ok, identity_ok, auth_ok = _clause_match(
            rv, event_type, identities, authorized
        )
        hit = type_ok and identity_ok and auth_ok
        basis.append(
            {
                "rule_id": rv.rule_id,
                "version_seq": rv.seq,
                "priority": rv.priority,
                "when": rv.when,
                "effects": rv.effects,
                "matched": hit,
                "clause": {
                    "event_type": type_ok,
                    "operator_identity": identity_ok,
                    "authorized": auth_ok,
                },
            }
        )
        if hit:
            matched.append(rv)

    verdict: dict[str, Any] = {
        "seq": seq,
        "event_type": event_type,
        "fabric_id": fabric_id,
        "actor": actor,
        "actor_identities": sorted(identities),
        "authorized": authorized,
        "responsible_member_ids": [],
        "next_action_ids": [],
        "winning_rule_ids": [],
        "priority": None,
        "basis": basis,
    }

    if not matched:
        return verdict

    priority = min(r.priority for r in matched)
    winners = [r for r in matched if r.priority == priority]
    members: set[str] = set()
    actions: set[str] = set()
    for r in winners:
        members.update(r.effects.get("responsible_member_ids", []))
        actions.update(r.effects.get("next_action_ids", []))

    verdict["priority"] = priority
    verdict["winning_rule_ids"] = [r.rule_id for r in winners]
    verdict["responsible_member_ids"] = sorted(members)
    verdict["next_action_ids"] = sorted(actions)
    return verdict
