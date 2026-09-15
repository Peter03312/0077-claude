"""并发与幂等：expected_next_seq 原子追加；同键同载荷复用，同键异载荷冲突。"""

import concurrent.futures

from service_tests.helpers import fabric, member, post, rule, setup_world


def test_idempotent_same_key_same_payload_reuses(client):
    setup_world(client)
    rule(
        client,
        "R",
        1,
        {"event_types": ["cut"], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": ["alice"], "next_action_ids": ["pay"]},
    )
    body = {
        "type": "cut",
        "submitted_by": "alice",
        "payload": {"fabric_id": "cloth"},
        "expected_next_seq": 5,
    }
    r1 = client.post("/api/events", json=body, headers={"Idempotency-Key": "k1"})
    r2 = client.post("/api/events", json=body, headers={"Idempotency-Key": "k1"})
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["seq"] == r2.json()["seq"]
    assert r1.json()["reused"] is False
    assert r2.json()["reused"] is True

    # 只落了一行
    assert client.get("/health").json()["next_seq"] == 6


def test_same_key_different_payload_conflicts(client):
    setup_world(client)
    rule(
        client,
        "R",
        1,
        {"event_types": [], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": [], "next_action_ids": []},
    )
    r1 = post(
        client, "cut", {"fabric_id": "cloth"}, "alice", key="dup", expected=5
    )
    assert r1.status_code == 201
    r2 = post(
        client,
        "transfer",
        {"fabric_id": "cloth", "to_member_id": "bob"},
        "alice",
        key="dup",
        expected=6,
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "idempotency_conflict"
    assert client.get("/health").json()["next_seq"] == 6


def test_stale_expected_next_seq_conflicts(client):
    setup_world(client)
    rule(
        client,
        "R",
        1,
        {"event_types": [], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": [], "next_action_ids": []},
    )
    post(client, "cut", {"fabric_id": "cloth"}, "alice")  # seq 5
    r = post(client, "cut", {"fabric_id": "cloth"}, "alice", expected=5)  # 已过期
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "seq_conflict"


def test_parallel_appends_assign_distinct_seqs_and_idempotent_pair_reuses(client):
    setup_world(client)
    rule(
        client,
        "R",
        1,
        {"event_types": [], "operator_identities": [], "authorized": None},
        {"responsible_member_ids": [], "next_action_ids": []},
    )

    def cut(expected, key=None):
        body = {
            "type": "cut",
            "submitted_by": "alice",
            "payload": {"fabric_id": "cloth"},
            "expected_next_seq": expected,
        }
        headers = {"Idempotency-Key": key} if key else {}
        resp = client.post("/api/events", json=body, headers=headers)
        return resp.status_code, resp.json()

    # 第一波：3 个无键线程竞争序号 5，恰好一个胜出
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        wave1 = list(pool.map(lambda _: cut(5), range(3)))
    assert [s for s, _ in wave1].count(201) == 1
    assert [s for s, _ in wave1].count(409) == 2
    assert client.get("/health").json()["next_seq"] == 6

    # 第二波：2 个同键线程竞争序号 6，一个落库、一个幂等复用同一行
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        wave2 = list(pool.map(lambda _: cut(6, key="same"), range(2)))
    statuses = [s for s, _ in wave2]
    assert statuses == [201, 201]
    reused = sorted(j["reused"] for _, j in wave2)
    assert reused == [False, True]
    assert len({j["seq"] for _, j in wave2}) == 1
    assert client.get("/health").json()["next_seq"] == 7
