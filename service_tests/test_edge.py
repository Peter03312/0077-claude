"""入口级边界：未知事件类型与 OpenAPI 基本可用性。"""

from service_tests.helpers import post, setup_world


def test_unknown_event_type_is_422(client):
    setup_world(client)
    r = post(client, "burn", {"fabric_id": "cloth"}, "alice")
    assert r.status_code == 422


def test_malformed_payload_is_422(client):
    r = post(client, "fabric_define", {"owner_id": "alice"}, "alice")
    assert r.status_code == 422


def test_rule_correction_does_not_recompute_prior_cut_but_applies_later(client):
    setup_world(client)
    # seq 4：规则 R 责任 alice
    r1 = post(
        client,
        "rule_define",
        {
            "rule_id": "R",
            "priority": 10,
            "when": {"event_types": ["cut"], "operator_identities": [], "authorized": None},
            "effects": {"responsible_member_ids": ["alice"], "next_action_ids": ["pay"]},
        },
        "club",
    )
    rule_seq = r1.json()["seq"]

    cut1 = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    cut1_seq = cut1.json()["seq"]
    assert cut1.json()["event"]["verdict"]["responsible_member_ids"] == ["alice"]

    # 更正规则本身：责任改为 bob
    rc = post(
        client,
        "correct",
        {
            "target_seq": rule_seq,
            "new_type": "rule_define",
            "new_payload": {
                "rule_id": "R",
                "priority": 10,
                "when": {"event_types": ["cut"], "operator_identities": [], "authorized": None},
                "effects": {"responsible_member_ids": ["bob"], "next_action_ids": ["pay2"]},
            },
        },
        "club",
    )
    assert rc.status_code == 201
    corr_seq = rc.json()["seq"]

    # 更正之前的 cut 裁决不变（后版/更正不重算旧事件）
    old = client.get(f"/api/events/{cut1_seq}").json()
    assert old["verdict"]["responsible_member_ids"] == ["alice"]
    # 更正时点状态里 R 已被替代：之后的 cut 用新责任
    cut2 = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    assert cut2.json()["event"]["verdict"]["responsible_member_ids"] == ["bob"]
    # 更正行携带替代规则版本
    corr_view = client.get(f"/api/events/{corr_seq}").json()
    assert corr_view["effective_event"]["type"] == "rule_define"


def test_correcting_rule_to_setup_removes_version_for_future_events(client):
    setup_world(client)
    # 先有一条对所有动作生效的规则作为“桥梁”，使旧 cut 已有裁决
    post(
        client,
        "rule_define",
        {
            "rule_id": "BRIDGE",
            "priority": 99,
            "when": {"event_types": [], "operator_identities": [], "authorized": None},
            "effects": {"responsible_member_ids": ["x"], "next_action_ids": ["y"]},
        },
        "club",
    )
    r1 = post(
        client,
        "rule_define",
        {
            "rule_id": "R",
            "priority": 10,
            "when": {"event_types": ["cut"], "operator_identities": [], "authorized": None},
            "effects": {"responsible_member_ids": ["alice"], "next_action_ids": ["pay"]},
        },
        "club",
    )
    # 把 R 更正成 member_define：不影响任何后续引用（规则不被序号引用），允许；
    # 之后 cut 仍有 BRIDGE 兜底，但不再命中 R
    rc = post(
        client,
        "correct",
        {
            "target_seq": r1.json()["seq"],
            "new_type": "member_define",
            "new_payload": {"member_id": "zoe"},
        },
        "club",
    )
    assert rc.status_code == 201
    cut = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    assert cut.json()["event"]["verdict"]["winning_rule_ids"] == ["BRIDGE"]
