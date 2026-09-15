"""版本语义：事件只取不晚于自身的最近规则版本；后版不重算旧事件；无适用版本拒绝。"""

from service_tests.helpers import fabric, member, post, rule, setup_world


def test_action_before_any_rule_is_rejected(client):
    setup_world(client)
    r = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "no_applicable_rule"


def test_new_version_only_affects_future_events_and_old_verdict_is_stable(client):
    setup_world(client)

    # 首版规则（序号 4）：cut -> alice / pay
    rule(
        client,
        "R",
        priority=10,
        when={"event_types": ["cut"], "operator_identities": [], "authorized": None},
        effects={"responsible_member_ids": ["alice"], "next_action_ids": ["pay"]},
    )
    r1 = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    assert r1.status_code == 201
    seq_cut_v1 = r1.json()["seq"]
    v1 = r1.json()["event"]["verdict"]
    assert v1["responsible_member_ids"] == ["alice"]
    assert v1["next_action_ids"] == ["pay"]
    assert v1["winning_rule_ids"] == ["R"]
    assert v1["basis"][0]["version_seq"] == 4

    # 改版（序号 6）：同 rule_id 新版本，唯一生效序号被替换
    rule(
        client,
        "R",
        priority=10,
        when={"event_types": ["cut"], "operator_identities": [], "authorized": None},
        effects={"responsible_member_ids": ["carol"], "next_action_ids": ["pay2"]},
    )
    r2 = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    assert r2.status_code == 201
    v2 = r2.json()["event"]["verdict"]
    assert v2["responsible_member_ids"] == ["carol"]
    assert v2["next_action_ids"] == ["pay2"]
    assert v2["basis"][0]["version_seq"] == 6

    # 旧事件按旧版本裁决，重放后也不变（后版不重算旧事件）
    old = client.get(f"/api/events/{seq_cut_v1}").json()["verdict"]
    assert old["responsible_member_ids"] == ["alice"]
    assert old["next_action_ids"] == ["pay"]
    assert old["basis"][0]["version_seq"] == 4

    # 生效规则只有一个版本
    state = client.get("/api/state").json()
    rule_versions = [r for r in state["rules"] if r["rule_id"] == "R"]
    assert len(rule_versions) == 1
    assert rule_versions[0]["effective_seq"] == 6


def test_no_match_yields_empty_effects_but_event_accepted(client):
    setup_world(client)
    # 规则只匹配 transfer，cut 无命中 -> 空责任空动作，但事件照常成立
    rule(
        client,
        "R",
        priority=1,
        when={"event_types": ["transfer"], "operator_identities": [], "authorized": None},
        effects={"responsible_member_ids": ["x"], "next_action_ids": ["y"]},
    )
    r = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    assert r.status_code == 201
    v = r.json()["event"]["verdict"]
    assert v["responsible_member_ids"] == []
    assert v["next_action_ids"] == []
    assert v["winning_rule_ids"] == []
    assert v["basis"][0]["matched"] is False
