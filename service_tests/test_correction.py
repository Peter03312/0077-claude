"""前向变更：更正限原提交者、携完整替代载荷；自更正序号起替代重放；
若令后续引用无效则整次拒绝。"""

from service_tests.helpers import fabric, member, post, rule, setup_world


def _rules_for_all_actions(client):
    # 一条规则匹配所有动作类事件
    rule(
        client,
        "ALL",
        10,
        {"event_types": [], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": ["club"], "next_action_ids": ["note"]},
    )


def test_correct_replaces_payload_from_correction_seq_and_owner_only(client):
    setup_world(client)
    _rules_for_all_actions(client)

    # alice 把布转交给 bob（seq 5）
    t = post(client, "transfer", {"fabric_id": "cloth", "to_member_id": "bob"}, "alice")
    seq_t = t.json()["seq"]
    assert client.get("/api/state").json()["fabrics"][0]["holder_id"] == "bob"

    # 非原提交者更正 -> 403
    r = post(
        client,
        "correct",
        {
            "target_seq": seq_t,
            "new_type": "transfer",
            "new_payload": {"fabric_id": "cloth", "to_member_id": "alice"},
        },
        "bob",
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "correct_not_original_submitter"

    # 原提交者更正：转交对象其实是 alice
    nxt = client.get("/health").json()["next_seq"]
    c = post(
        client,
        "correct",
        {
            "target_seq": seq_t,
            "new_type": "transfer",
            "new_payload": {"fabric_id": "cloth", "to_member_id": "alice"},
        },
        "alice",
    )
    assert c.status_code == 201
    corr_seq = c.json()["seq"]

    # 当前投影：持有人变为 alice
    assert client.get("/api/state").json()["fabrics"][0]["holder_id"] == "alice"

    # as-of 更正序号之前：仍是 bob（更正自其序号起生效）
    before = client.get(f"/api/events/{seq_t}?as_of={corr_seq - 1}").json()
    assert before["payload"]["to_member_id"] == "bob"
    assert before.get("correction_pending") is True

    # as-of 更正序号之后：原行保留原载荷但被标记 superseded；
    # 替代形态（新持有人 alice）挂在更正行的 effective_event 上
    after = client.get(f"/api/events/{seq_t}").json()
    assert after["payload"]["to_member_id"] == "bob"
    assert after["superseded"] is True
    corr_view = client.get(f"/api/events/{corr_seq}").json()
    assert corr_view["type"] == "correct"
    assert corr_view["effective_event"]["payload"]["to_member_id"] == "alice"
    assert client.get("/api/state").json()["fabrics"][0]["holder_id"] == "alice"
    # 更正行上也可直接读到替代后重放的裁决
    assert corr_view["verdict"]["winning_rule_ids"] == ["ALL"]


def test_correct_event_type_switch_changes_verdict(client):
    setup_world(client)
    rule(
        client,
        "CUT",
        1,
        {"event_types": ["cut"], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": ["cutter"], "next_action_ids": ["cut_done"]},
    )
    rule(
        client,
        "TR",
        1,
        {"event_types": ["transfer"], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": ["carrier"], "next_action_ids": ["log"]},
    )

    # 误提交成 cut
    ev = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    seq = ev.json()["seq"]
    assert ev.json()["event"]["verdict"]["responsible_member_ids"] == ["cutter"]

    # 更正为 transfer bob
    rc = post(
        client,
        "correct",
        {
            "target_seq": seq,
            "new_type": "transfer",
            "new_payload": {"fabric_id": "cloth", "to_member_id": "bob"},
        },
        "alice",
    )
    corr_seq = rc.json()["seq"]

    # 原 cut 行仍可读，但其结果被取代
    old = client.get(f"/api/events/{seq}").json()
    assert old["type"] == "cut"
    assert old["superseded"] is True

    # 替代形态与裁决挂在更正行上
    corr_view = client.get(f"/api/events/{corr_seq}").json()
    assert corr_view["effective_event"]["type"] == "transfer"
    assert corr_view["verdict"]["responsible_member_ids"] == ["carrier"]
    # 自更正序号起投影改变：持有人为 bob
    assert client.get("/api/state").json()["fabrics"][0]["holder_id"] == "bob"


def test_correction_that_invalidates_later_withdraw_is_rejected_entirely(client):
    setup_world(client)
    _rules_for_all_actions(client)

    g = post(
        client,
        "grant",
        {"fabric_id": "cloth", "grantee_id": "bob", "action_id": "cut"},
        "alice",
    )
    grant_seq = g.json()["seq"]
    w = post(client, "withdraw", {"fabric_id": "cloth", "grant_seq": grant_seq}, "alice")
    assert w.status_code == 201

    # 尝试把 grant 改成 return：后续 withdraw 引用的授权序号不再是 grant -> 整次拒绝
    r = post(
        client,
        "correct",
        {
            "target_seq": grant_seq,
            "new_type": "return",
            "new_payload": {"fabric_id": "cloth"},
        },
        "alice",
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "grant_ref_invalid"

    # 流未被污染：撤回仍然有效
    grants = client.get("/api/state").json()["grants"]
    assert grants[0]["grant_seq"] == grant_seq
    assert grants[0]["valid"] is False


def test_correction_that_invalidates_later_appeal_is_rejected(client):
    setup_world(client)
    _rules_for_all_actions(client)

    cut = post(client, "cut", {"fabric_id": "cloth"}, "alice")
    cut_seq = cut.json()["seq"]
    ap = post(
        client,
        "appeal",
        {"target_seq": cut_seq, "fabric_id": "cloth"},
        "alice",
    )
    assert ap.status_code == 201

    # 把 cut 改成 setup 事件（member_define）：申诉将引用不到裁决 -> 拒绝
    r = post(
        client,
        "correct",
        {
            "target_seq": cut_seq,
            "new_type": "member_define",
            "new_payload": {"member_id": "dora"},
        },
        "alice",
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "appeal_target_not_verdict"


def test_correct_requires_complete_payload(client):
    setup_world(client)
    _rules_for_all_actions(client)
    t = post(client, "transfer", {"fabric_id": "cloth", "to_member_id": "bob"}, "alice")
    seq_t = t.json()["seq"]

    # 缺字段的替代载荷直接 422
    r = post(
        client,
        "correct",
        {"target_seq": seq_t, "new_type": "transfer", "new_payload": {"fabric_id": "cloth"}},
        "alice",
    )
    assert r.status_code == 422

    # 不能更正成另一条更正
    r2 = post(
        client,
        "correct",
        {
            "target_seq": seq_t,
            "new_type": "correct",
            "new_payload": {"target_seq": 1, "new_type": "cut", "new_payload": {}},
        },
        "alice",
    )
    assert r2.status_code == 409
