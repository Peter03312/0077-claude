"""测试工具：构造事件并追加。"""

from __future__ import annotations


def post(client, event_type, payload, submitted_by, *, key=None, expected=None):
    """按当前 next_seq 追加；expected 显式给出时用于制造序号冲突。"""
    if expected is None:
        expected = client.get("/health").json()["next_seq"]
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        "/api/events",
        json={
            "type": event_type,
            "submitted_by": submitted_by,
            "payload": payload,
            "expected_next_seq": expected,
        },
        headers=headers,
    )


def member(client, member_id, **extra):
    return post(client, "member_define", {"member_id": member_id, **extra}, member_id)


def fabric(client, fabric_id, owner_id):
    return post(client, "fabric_define", {"fabric_id": fabric_id, "owner_id": owner_id}, owner_id)


RULE = {
    "event_types": [],
    "operator_identities": [],
    "authorized": None,
}


def rule(client, rule_id, priority, when=None, effects=None, submitted_by="club"):
    return post(
        client,
        "rule_define",
        {
            "rule_id": rule_id,
            "priority": priority,
            "when": when or dict(RULE),
            "effects": effects or {"responsible_member_ids": [], "next_action_ids": []},
        },
        submitted_by,
    )


def setup_world(client):
    """成员 alice(主人) / bob 与布料 cloth，返回各创建响应。"""
    member(client, "alice", age=14, gender="F")
    member(client, "bob", age=15, gender="F")
    fabric(client, "cloth", "alice")
