"""确定性重放与状态投影模块。

核心事实：任何查询都从原始事件流折叠（fold）而来——同一条事件流必然得到同一份
状态与同一批裁决，不依赖任何缓存。事件先按“此前投影”裁决，裁决通过后才更新状态。

更正（correct）语义：
* 更正事件自身的提交者必须等于目标事件的原提交者（追加时 403）；
* 从更正事件所在序号起，用完整替代载荷替代目标事件重放；
* 若替代会使后续引用（撤回引用授权、申诉引用裁决、另一更正引用序号）失效，
  整次更正拒绝（追加时 409）；
* 更正不产生裁决，只改变流的物化结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError

from . import adjudication
from .errors import Conflict, Forbidden, RejectEvent
from .models import validate_payload


# ---- 投影数据结构 ------------------------------------------------------------


@dataclass
class Grant:
    seq: int
    fabric_id: str
    grantee_id: str
    action_id: str
    withdrawn_at: Optional[int] = None  # 撤回事件序号；None 仍有效


@dataclass
class Fabric:
    fabric_id: str
    owner_id: str
    holder_id: str
    created_seq: int


@dataclass
class RuleVersion:
    rule_id: str
    seq: int  # 生效序号（rule_define / 更正后的版本序号）
    priority: int
    when: dict
    effects: dict


@dataclass
class State:
    members: dict[str, dict] = field(default_factory=dict)
    fabrics: dict[str, Fabric] = field(default_factory=dict)
    rules: dict[str, RuleVersion] = field(default_factory=dict)
    # grant 事件序号 -> Grant（撤回时原地标注，重放可重复得到同一结果）
    grants: dict[int, Grant] = field(default_factory=dict)
    verdicts: dict[int, dict] = field(default_factory=dict)
    # 物化后实际发生过的序号集合（被更正替换的旧 grant 序号不参与引用）
    seqs: set[int] = field(default_factory=set)
    # appeal_seq -> 补救信息（依申诉本序号规则追加，不改旧裁决）
    appeal_remedies: dict[int, dict] = field(default_factory=dict)
    # 更正序号 -> 目标序号在影子重放中得到的替代裁决（若目标为裁决类事件）
    replacement_verdicts: dict[int, dict] = field(default_factory=dict)

    def has_valid_grant(self, fabric_id: str, actor: str, action_id: str) -> bool:
        for g in self.grants.values():
            if (
                g.fabric_id == fabric_id
                and g.grantee_id == actor
                and g.action_id == action_id
                and g.withdrawn_at is None
            ):
                return True
        return False

    def snapshot(self) -> dict:
        return {
            "members": sorted(self.members),
            "fabrics": [
                {
                    "fabric_id": f.fabric_id,
                    "owner_id": f.owner_id,
                    "holder_id": f.holder_id,
                }
                for f in sorted(self.fabrics.values(), key=lambda x: x.created_seq)
            ],
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "effective_seq": r.seq,
                    "priority": r.priority,
                    "when": r.when,
                    "effects": r.effects,
                }
                for r in sorted(self.rules.values(), key=lambda x: (x.seq, x.rule_id))
            ],
            "grants": [
                {
                    "grant_seq": g.seq,
                    "fabric_id": g.fabric_id,
                    "grantee_id": g.grantee_id,
                    "action_id": g.action_id,
                    "withdrawn_at": g.withdrawn_at,
                    "valid": g.withdrawn_at is None,
                }
                for g in sorted(self.grants.values(), key=lambda x: x.seq)
            ],
            "verdicts": [self.verdicts[s] for s in sorted(self.verdicts)],
            "appeal_remedies": [
                {"appeal_seq": s, **rem}
                for s, rem in sorted(self.appeal_remedies.items())
            ],
        }


# ---- 原始事件 -> 物化事件 ----------------------------------------------------


@dataclass
class RawEvent:
    seq: int
    type: str
    submitted_by: str
    payload: dict
    idempotency_key: Optional[str] = None


def _validate_correct_event(raw: RawEvent, target: RawEvent) -> None:
    """追加更正时的信封级校验（重放时同样的规则自然成立）。"""
    p = raw.payload
    if raw.submitted_by != target.submitted_by:
        raise Forbidden(
            f"序号 {raw.seq}: 更正仅限原提交者 {target.submitted_by}",
            code="correct_not_original_submitter",
        )
    new_type = p["new_type"]
    if new_type == "correct":
        raise Conflict(
            f"序号 {raw.seq}: 不能把事件更正为另一条更正",
            code="correct_chain_forbidden",
        )


def materialize(
    rows: list[RawEvent],
) -> list[tuple[RawEvent, RawEvent, Optional[RawEvent]]]:
    """按序号收集更正映射，返回按序号排序的三元组：

    ``(原始事件, 生效事件, 更正事件或 None)``

    先收集所有 correct 事件再物化，保证更早的更正能影响更晚更正的目标读取。
    生效事件的序号沿用目标序号（替代载荷，原提交者不变）。
    """
    by_seq = {r.seq: r for r in rows}
    # target_seq -> correction_raw；同目标重复更正在这里拒绝
    corrections: dict[int, RawEvent] = {}
    for r in sorted(rows, key=lambda x: x.seq):
        if r.type != "correct":
            continue
        target_seq = r.payload["target_seq"]
        target = by_seq.get(target_seq)
        if target is None:
            raise Conflict(
                f"序号 {r.seq}: 更正目标 {target_seq} 不存在",
                code="correct_target_missing",
            )
        if target.type == "correct":
            raise Conflict(
                f"序号 {r.seq}: 不能更正另一条更正",
                code="correct_chain_forbidden",
            )
        _validate_correct_event(r, target)
        if target_seq in corrections:
            raise Conflict(
                f"序号 {r.seq}: 序号 {target_seq} 已被更正",
                code="target_already_corrected",
            )
        corrections[target_seq] = r

    triples: list[tuple[RawEvent, RawEvent, Optional[RawEvent]]] = []
    for r in sorted(rows, key=lambda x: x.seq):
        if r.type == "correct":
            continue
        corr = corrections.get(r.seq)
        if corr is not None:
            eff = RawEvent(
                seq=r.seq,
                type=corr.payload["new_type"],
                submitted_by=r.submitted_by,  # 原提交者不变
                payload=corr.payload["new_payload"],
            )
        else:
            eff = r
        triples.append((r, eff, corr))
    return triples


# ---- 折叠 --------------------------------------------------------------------


def _shadow_state(
    rows: list[RawEvent],
    substitutions: dict[int, tuple[str, dict, str]],
    up_to: int,
) -> State:
    """影子重放：用 substitutions 替换目标事件后，从头折叠到 up_to（含）。

    替换发生在目标自己的位置，因此更正点之后继续折叠所依赖的投影与
    “目标当初就是替代载荷”一致；本函数只产出状态，旧裁决的冻结由调用方负责。
    """
    state = State()
    for raw in sorted(rows, key=lambda x: x.seq):
        if raw.seq > up_to:
            break
        if raw.type == "correct":
            continue
        if raw.seq in substitutions:
            new_type, new_payload, original_submitter = substitutions[raw.seq]
            effective = RawEvent(
                seq=raw.seq,
                type=new_type,
                submitted_by=original_submitter,  # 原提交者不变
                payload=new_payload,
            )
            _apply(state, effective)
        else:
            _apply(state, raw)
    return state


def fold(
    rows: list[RawEvent],
    *,
    up_to: Optional[int] = None,
) -> State:
    """把原始事件流确定性地折叠为 :class:`State`。

    更正的位置语义（“**自更正序号起**用替代载荷重放目标”）：

    * 更正序号之前，一切按原始事件流折叠，旧裁决原封不动（后版/替代都不重算它们）；
    * 到达更正序号时，用“目标位置被替换”的影子重放重建一份状态，
      但此前序号的裁决/补救从冻结副本恢复；
    * 更正序号之后的撤回、申诉、剪裁等继续在重建状态上折叠——
      若替代令这些后续引用失效（授权消失、裁决消失），影子重放即抛错，
      于是整次更正在追加前被拒绝。
    """
    state = State()
    substitutions: dict[int, tuple[str, dict, str]] = {}
    frozen_verdicts: dict[int, dict] = {}
    frozen_remedies: dict[int, dict] = {}

    for raw in sorted(rows, key=lambda x: x.seq):
        if up_to is not None and raw.seq > up_to:
            break

        if raw.type == "correct":
            # 冻结更正点之前的裁决与补救
            for s, v in state.verdicts.items():
                frozen_verdicts.setdefault(s, v)
            for s, rem in state.appeal_remedies.items():
                frozen_remedies.setdefault(s, rem)

            target_seq = raw.payload["target_seq"]
            target = next(r for r in rows if r.seq == target_seq)
            substitutions[target_seq] = (
                raw.payload["new_type"],
                raw.payload["new_payload"],
                target.submitted_by,
            )
            # 影子重放重建截至本更正序号的投影
            state = _shadow_state(rows, substitutions, up_to=raw.seq)
            # 目标在替代后的裁决归属更正行展示（旧裁决仍冻结在原序号上）
            if target_seq in state.verdicts:
                state.replacement_verdicts[raw.seq] = state.verdicts[target_seq]
            # 影子里“更正点之前”的裁决与补救是重算产物，必须用冻结值覆盖，
            # 但被更正目标自己的替代结果要保留（它不属于“旧裁决”）。
            for s, v in frozen_verdicts.items():
                if s != target_seq:
                    state.verdicts[s] = v
            state.appeal_remedies = dict(frozen_remedies)
            continue

        # 已生效更正的目标事件：其效果已在影子重放中，不能重复折叠
        if raw.seq in substitutions:
            continue

        _apply(state, raw)

    return state


def _require_member(state: State, member_id: str, seq: int, what: str) -> None:
    if member_id not in state.members:
        raise RejectEvent(
            f"序号 {seq}: {what} {member_id} 尚未登记", 422, "member_not_found"
        )


def _apply(state: State, ev: RawEvent) -> None:
    seq = ev.seq
    p = ev.payload
    typ = ev.type
    actor = ev.submitted_by
    state.seqs.add(seq)

    if typ == "member_define":
        mid = p["member_id"]
        if mid in state.members:
            raise RejectEvent(f"序号 {seq}: 成员 {mid} 已登记", 422, "member_exists")
        state.members[mid] = {"member_id": mid, "age": p.get("age"), "gender": p.get("gender")}
        return

    if typ == "fabric_define":
        fid = p["fabric_id"]
        owner = p["owner_id"]
        _require_member(state, owner, seq, "主人")
        if fid in state.fabrics:
            raise RejectEvent(f"序号 {seq}: 布料 {fid} 已登记", 422, "fabric_exists")
        state.fabrics[fid] = Fabric(fid, owner, owner, seq)
        return

    if typ == "rule_define":
        # 同 rule_id 的新事件即新版本，唯一生效序号被覆盖
        state.rules[p["rule_id"]] = RuleVersion(
            rule_id=p["rule_id"],
            seq=seq,
            priority=p["priority"],
            when=p["when"],
            effects=p["effects"],
        )
        return

    if typ == "grant":
        fid = p["fabric_id"]
        grantee = p["grantee_id"]
        action = p["action_id"]
        fabric = state.fabrics.get(fid)
        if fabric is None:
            raise RejectEvent(f"序号 {seq}: 布料 {fid} 不存在", 422, "fabric_not_found")
        _require_member(state, grantee, seq, "被授权成员")
        if actor != fabric.owner_id:
            raise RejectEvent(
                f"序号 {seq}: 仅主人可授权（主人为 {fabric.owner_id}）",
                422,
                "grant_not_by_owner",
            )
        # 动作类事件：先裁决，再更新投影
        verdict = adjudication.adjudicate(state, seq, typ, actor, fid)
        state.verdicts[seq] = verdict
        if state.has_valid_grant(fid, grantee, action):
            raise RejectEvent(
                f"序号 {seq}: 该有效授权已存在", 422, "duplicate_grant"
            )
        state.grants[seq] = Grant(seq, fid, grantee, action)
        return

    if typ in ("cut", "transfer", "return", "withdraw", "appeal"):
        fid = p["fabric_id"]
        verdict = adjudication.adjudicate(state, seq, typ, actor, fid)
        state.verdicts[seq] = verdict

        if typ == "cut":
            return  # 剪裁只裁决，不改主人/持有人投影

        if typ == "transfer":
            to = p["to_member_id"]
            _require_member(state, to, seq, "接收成员")
            state.fabrics[fid].holder_id = to
            return

        if typ == "return":
            state.fabrics[fid].holder_id = state.fabrics[fid].owner_id
            return

        if typ == "withdraw":
            grant_seq = p["grant_seq"]
            if grant_seq not in state.grants:
                raise Conflict(
                    f"序号 {seq}: 撤回引用的授权序号 {grant_seq} 不存在或已失效",
                    code="grant_ref_invalid",
                )
            grant = state.grants[grant_seq]
            if grant.fabric_id != fid:
                raise Conflict(
                    f"序号 {seq}: 授权 {grant_seq} 不属于布料 {fid}",
                    code="grant_ref_fabric_mismatch",
                )
            if grant.withdrawn_at is not None:
                raise Conflict(
                    f"序号 {seq}: 授权 {grant_seq} 已被撤回",
                    code="grant_already_withdrawn",
                )
            grant.withdrawn_at = seq
            return

        if typ == "appeal":
            target_seq = p["target_seq"]
            if target_seq not in state.verdicts:
                raise Conflict(
                    f"序号 {seq}: 申诉目标 {target_seq} 不是可引用的裁决"
                    "（或已被更正替代为非裁决事件）",
                    code="appeal_target_not_verdict",
                )
            # 补救依“本序号规则”追加；旧裁决原样不动
            state.appeal_remedies[seq] = {
                "target_seq": target_seq,
                "fabric_id": fid,
                "responsible_member_ids": list(verdict["responsible_member_ids"]),
                "next_action_ids": list(verdict["next_action_ids"]),
                "winning_rule_ids": list(verdict["winning_rule_ids"]),
            }
            return

    raise RejectEvent(f"序号 {seq}: 未知事件类型 {typ}", 422, "unknown_event_type")


# ---- 追加前的更正校验 --------------------------------------------------------


def validate_correction(rows: list[RawEvent], candidate: RawEvent) -> None:
    """在追加 candidate（必为 correct）前验证：原提交者 + 重放不令后续引用失效。

    做法：把候选更正插入物化流做一次完整试折叠；折叠成功即说明后续撤回、申诉、
    另一更正引用仍然有效（失效会抛 Conflict）。
    """
    by_seq = {r.seq: r for r in rows}
    p = candidate.payload
    target = by_seq.get(p["target_seq"])
    if target is None:
        raise Conflict(
            f"序号 {candidate.seq}: 更正目标 {p['target_seq']} 不存在",
            code="correct_target_missing",
        )
    if target.type == "correct":
        raise Conflict(
            f"序号 {candidate.seq}: 不能更正另一条更正",
            code="correct_chain_forbidden",
        )
    if candidate.submitted_by != target.submitted_by:
        raise Forbidden(
            f"序号 {candidate.seq}: 更正仅限原提交者 {target.submitted_by}",
            code="correct_not_original_submitter",
        )
    if p["new_type"] == "correct":
        raise Conflict(
            f"序号 {candidate.seq}: 不能把事件更正为另一条更正",
            code="correct_chain_forbidden",
        )
    if any(r.type == "correct" and r.payload["target_seq"] == target.seq for r in rows):
        raise Conflict(
            f"序号 {candidate.seq}: 序号 {target.seq} 已被更正",
            code="target_already_corrected",
        )

    # 替代载荷必须是新类型的“完整载荷”，先做结构校验再试重放
    try:
        validate_payload(p["new_type"], p["new_payload"])
    except ValidationError:
        raise RejectEvent(
            f"序号 {candidate.seq}: 替代载荷不满足 {p['new_type']} 的完整结构",
            422,
            "invalid_replacement_payload",
        )
    except ValueError:
        raise RejectEvent(
            f"序号 {candidate.seq}: 未知替代事件类型 {p['new_type']}",
            422,
            "unknown_event_type",
        )

    trial = rows + [candidate]
    # 任一步引用失效（grant 消失、申诉目标不再是裁决等）都会在这里抛出
    fold(trial)


# ---- 查询视图 ----------------------------------------------------------------


def _remedies_for(seq: int, state: State, as_of: Optional[int]) -> list[dict]:
    remedies = []
    for appeal_seq, rem in state.appeal_remedies.items():
        if rem["target_seq"] == seq and (as_of is None or appeal_seq <= as_of):
            remedies.append({"appeal_seq": appeal_seq, **rem})
    return remedies


def _verdict_with_remedies(v: dict, state: State, as_of: Optional[int]) -> dict:
    view = dict(v)
    view["appeal_remedies"] = _remedies_for(v["seq"], state, as_of)
    return view


def _view_verdict(seq: int, state: State, as_of: Optional[int]) -> dict:
    return _verdict_with_remedies(state.verdicts[seq], state, as_of)


def event_views(rows: list[RawEvent], as_of: Optional[int] = None) -> list[dict]:
    """历史查询：原始事件全列；更正自其序号起替代目标。

    * as_of 早于更正序号：目标仍展示原载荷/原裁决，标 ``correction_pending``；
    * 否则目标保留原内容并标 ``superseded``（其旧裁决仍可查），更正行携带
      替代形态 ``effective_event`` 与替代后重放得到的 ``verdict``。
    """
    triples = materialize(rows)
    by_seq = {original.seq: (original, eff, corr) for original, eff, corr in triples}
    state = fold(rows, up_to=as_of)
    # as_of 折叠在更正点之前时不含替代裁决；对“已生效更正”的展示取当前折叠
    current = fold(rows) if as_of is not None else state
    views: list[dict] = []

    for raw in sorted(rows, key=lambda x: x.seq):
        if as_of is not None and raw.seq > as_of:
            continue

        if raw.type == "correct":
            target_seq = raw.payload["target_seq"]
            original, eff, _ = by_seq[target_seq]
            view: dict[str, Any] = {
                "seq": raw.seq,
                "type": "correct",
                "submitted_by": raw.submitted_by,
                "payload": raw.payload,
                "effective_event": {
                    "seq": target_seq,
                    "type": eff.type,
                    "submitted_by": eff.submitted_by,
                    "payload": eff.payload,
                },
            }
            if raw.seq in current.replacement_verdicts:
                view["verdict"] = _verdict_with_remedies(
                    current.replacement_verdicts[raw.seq], current, None
                )
            views.append(view)
            continue

        original, eff, corr = by_seq[raw.seq]
        active = corr is not None and (as_of is None or corr.seq <= as_of)
        view = {
            "seq": raw.seq,
            "type": raw.type,
            "submitted_by": raw.submitted_by,
            "payload": raw.payload,
        }
        if raw.seq in state.verdicts:
            view["verdict"] = _view_verdict(raw.seq, state, as_of)
        if corr is not None:
            view["corrected_by_seq"] = corr.seq
            if active:
                view["superseded"] = True
            else:
                view["correction_pending"] = True
        views.append(view)
    return views


def state_at(rows: list[RawEvent], as_of: Optional[int] = None) -> dict:
    state = fold(rows, up_to=as_of)
    snap = state.snapshot()
    # 给快照中的裁决补上申诉补救
    snap["verdicts"] = [
        _view_verdict(s, state, as_of) for s in sorted(state.verdicts)
    ]
    return snap
