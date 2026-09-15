"""布料生命周期投影：转交换持有人、返还恢复主人、撤回自引用序号失效授权。"""

from service_tests.helpers import post, rule, setup_world


def test_transfer_return_holder_projection(client):
    setup_world(client)
    rule(
        client,
        "R",
        1,
        {"event_types": [], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": [], "next_action_ids": []},
    )
    post(client, "transfer", {"fabric_id": "cloth", "to_member_id": "bob"}, "alice")
    assert client.get("/api/state").json()["fabrics"][0]["holder_id"] == "bob"

    post(client, "return", {"fabric_id": "cloth"}, "bob")
    f = client.get("/api/state").json()["fabrics"][0]
    assert f["holder_id"] == "alice"
    assert f["owner_id"] == "alice"


def test_grant_withdraw_lifecycle(client):
    setup_world(client)
    rule(
        client,
        "R",
        1,
        {"event_types": [], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": [], "next_action_ids": []},
    )
    # 非主人不能授权
    r = post(
        client,
        "grant",
        {"fabric_id": "cloth", "grantee_id": "bob", "action_id": "cut"},
        "bob",
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "grant_not_by_owner"

    g = post(
        client,
        "grant",
        {"fabric_id": "cloth", "grantee_id": "bob", "action_id": "cut"},
        "alice",
    )
    grant_seq = g.json()["seq"]

    post(client, "transfer", {"fabric_id": "cloth", "to_member_id": "bob"}, "alice")
    cut = post(client, "cut", {"fabric_id": "cloth"}, "bob")
    assert cut.status_code == 201
    assert cut.json()["event"]["verdict"]["authorized"] is True

    # 撤回自本序号起失效
    w = post(client, "withdraw", {"fabric_id": "cloth", "grant_seq": grant_seq}, "alice")
    assert w.status_code == 201
    grant = [x for x in client.get("/api/state").json()["grants"] if x["grant_seq"] == grant_seq][0]
    assert grant["valid"] is False
    assert grant["withdrawn_at"] == w.json()["seq"]

    cut2 = post(client, "cut", {"fabric_id": "cloth"}, "bob")
    assert cut2.json()["event"]["verdict"]["authorized"] is False

    # 重复撤回被拒绝
    w2 = post(client, "withdraw", {"fabric_id": "cloth", "grant_seq": grant_seq}, "alice")
    assert w2.status_code == 409
