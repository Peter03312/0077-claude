"""申诉：引用裁决并依本序号规则追加补救，不改旧裁决。"""

from service_tests.helpers import post, rule, setup_world


def test_appeal_appends_remedy_under_current_rule_without_changing_old_verdict(client):
    setup_world(client)

    # cut 旧规则：责任 alice
    rule(
        client,
        "OLD",
        10,
        {"event_types": ["cut"], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": ["alice"], "next_action_ids": ["pay"]},
    )
    cut = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    cut_seq = cut.json()["seq"]
    old_verdict = cut.json()["event"]["verdict"]
    assert old_verdict["responsible_member_ids"] == ["alice"]

    # 申诉时刻的规则：appeal -> 追加补救 bob/remedy
    rule(
        client,
        "AP",
        5,
        {"event_types": ["appeal"], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": ["bob"], "next_action_ids": ["remedy"]},
    )
    ap = post(
        client,
        "appeal",
        {"target_seq": cut_seq, "fabric_id": "cloth"},
        "alice",
    )
    assert ap.status_code == 201
    appeal_seq = ap.json()["seq"]
    apv = ap.json()["event"]["verdict"]
    assert apv["responsible_member_ids"] == ["bob"]
    assert apv["next_action_ids"] == ["remedy"]

    # 旧裁决内容不变，但带有追加而来的补救
    view = client.get(f"/api/events/{cut_seq}").json()
    assert view["verdict"]["responsible_member_ids"] == ["alice"]
    assert view["verdict"]["next_action_ids"] == ["pay"]
    remedies = view["verdict"]["appeal_remedies"]
    assert len(remedies) == 1
    assert remedies[0]["appeal_seq"] == appeal_seq
    assert remedies[0]["target_seq"] == cut_seq
    assert remedies[0]["responsible_member_ids"] == ["bob"]
    assert remedies[0]["next_action_ids"] == ["remedy"]

    # 申诉之前的时点查询看不到补救
    before = client.get(f"/api/events/{cut_seq}?as_of={appeal_seq - 1}").json()
    assert before["verdict"]["appeal_remedies"] == []


def test_appeal_must_reference_a_verdict(client):
    setup_world(client)
    rule(
        client,
        "AP",
        1,
        {"event_types": ["appeal"], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": ["bob"], "next_action_ids": ["remedy"]},
    )
    # 序号 1 是 member_define（无裁决）
    r = post(client, "appeal", {"target_seq": 1, "fabric_id": "cloth"}, "alice")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "appeal_target_not_verdict"
