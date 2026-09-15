"""并列规则：priority 小者优先；同级两列 effects 各求并集并排序。"""

from service_tests.helpers import post, rule, setup_world

WHEN_CUT = {"event_types": ["cut"], "operator_identities": [], "authorized": None}
WHEN_TRANSFER = {
    "event_types": ["transfer"],
    "operator_identities": [],
    "authorized": None,
}


def test_same_priority_unions_and_sorts_both_columns(client):
    setup_world(client)
    rule(client, "R1", 10, WHEN_CUT,
         {"responsible_member_ids": ["bob"], "next_action_ids": ["a", "c"]})
    rule(client, "R2", 10, WHEN_CUT,
         {"responsible_member_ids": ["alice"], "next_action_ids": ["b"]})
    # 高优先级但不匹配，不应参与
    rule(client, "R3", 1, WHEN_TRANSFER,
         {"responsible_member_ids": ["zzz"], "next_action_ids": ["zzz"]})

    r = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    v = r.json()["event"]["verdict"]
    assert r.status_code == 201
    assert v["priority"] == 10
    assert v["winning_rule_ids"] == ["R1", "R2"]
    assert v["responsible_member_ids"] == ["alice", "bob"]
    assert v["next_action_ids"] == ["a", "b", "c"]


def test_lower_priority_shadows_higher_numbered(client):
    setup_world(client)
    rule(client, "R1", 10, WHEN_CUT,
         {"responsible_member_ids": ["bob"], "next_action_ids": ["a"]})
    rule(client, "R2", 10, WHEN_CUT,
         {"responsible_member_ids": ["alice"], "next_action_ids": ["b"]})
    # 新增更小 priority 的命中规则：只取它
    rule(client, "R4", 1, WHEN_CUT,
         {"responsible_member_ids": ["x"], "next_action_ids": ["z"]})

    r = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    v = r.json()["event"]["verdict"]
    assert v["priority"] == 1
    assert v["winning_rule_ids"] == ["R4"]
    assert v["responsible_member_ids"] == ["x"]
    assert v["next_action_ids"] == ["z"]


def test_identity_and_authorized_clauses_are_conjunctive(client):
    setup_world(client)
    # 仅“持有人且有有效授权”命中：alice 是主人也是初始持有人，但没有授权记录 -> 不命中
    rule(
        client,
        "RA",
        1,
        {"event_types": ["cut"], "operator_identities": ["holder"], "authorized": True},
        {"responsible_member_ids": ["h"], "next_action_ids": ["go"]},
    )
    r = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    v = r.json()["event"]["verdict"]
    assert v["winning_rule_ids"] == []
    assert v["authorized"] is False
    assert v["actor_identities"] == ["holder", "owner"]

    # 主人授权 alice 自己执行 cut（grant 也需先有规则；补一条宽松规则）
    rule(
        client,
        "RG",
        1,
        {"event_types": ["grant"], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": [], "next_action_ids": []},
    )
    g = post(
        client,
        "grant",
        {"fabric_id": "cloth", "grantee_id": "alice", "action_id": "cut"},
        "alice",
    )
    assert g.status_code == 201

    r2 = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    v2 = r2.json()["event"]["verdict"]
    assert v2["authorized"] is True
    assert v2["winning_rule_ids"] == ["RA"]
