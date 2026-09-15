"""docker compose verify 服务入口：

1. 探测 api 容器的 HTTP（健康检查 + 一段端到端事件追加/裁决/查询）；
2. 在同一镜像内运行 pytest；
随后以相应退出码退出（verify 不常驻）。

本地可直接指向一个运行中的 api：

    SRV_DIR=. API_BASE_URL=http://127.0.0.1:8000 python scripts/verify.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("API_BASE_URL", "http://api:8000")
SRV_DIR = os.environ.get("SRV_DIR", "/srv")


def request(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers=headers or {}
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_for_api(retries: int = 30, delay: float = 1.0) -> None:
    for _ in range(retries):
        try:
            status, _ = request("GET", "/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(delay)
    raise SystemExit("api 在限定时间内未就绪")


def smoke() -> None:
    """端到端冒烟：登记 -> 布料 -> 规则 -> 剪裁裁决 -> 幂等复用 -> 查询。

    所有实体 id 带本次运行的随机后缀，使 verify 在同一持久卷上重复执行也互不干扰；
    幂等复用检测使用同一 run 内唯一键。
    """
    print("== HTTP 冒烟：成员/布料/规则/授权/剪裁 ==", flush=True)
    run_id = uuid.uuid4().hex[:8]
    owner = f"alice_{run_id}"
    other = f"bob_{run_id}"
    fabric_id = f"cloth_{run_id}"
    rule_id = f"ALL_{run_id}"
    idem_key = f"smoke-member-{run_id}"

    _, health = request("GET", "/health")
    seq = health["next_seq"]

    def append(event_type, submitted_by, payload, key=None):
        nonlocal seq
        hdr = {"Idempotency-Key": key} if key else {}
        status, body = request(
            "POST",
            "/api/events",
            {
                "type": event_type,
                "submitted_by": submitted_by,
                "payload": payload,
                "expected_next_seq": seq,
            },
            hdr,
        )
        assert status == 201, f"{event_type} 追加失败: {status} {body}"
        assert body["seq"] == seq, f"序号异常: {body}"
        seq += 1
        return body

    member_body = {"member_id": owner, "age": 14, "gender": "F"}
    append("member_define", owner, member_body, key=idem_key)
    # 幂等复用：同键同载荷返回同一序号
    status, reused = request(
        "POST",
        "/api/events",
        {
            "type": "member_define",
            "submitted_by": owner,
            "payload": member_body,
            "expected_next_seq": seq - 1,
        },
        {"Idempotency-Key": idem_key},
    )
    assert status == 201 and reused["reused"] is True, reused

    append("member_define", other, {"member_id": other, "age": 15, "gender": "F"})
    append("fabric_define", owner, {"fabric_id": fabric_id, "owner_id": owner})

    # 规则：所有动作 -> 责任 club，下一动作 record
    append(
        "rule_define",
        "club",
        {
            "rule_id": rule_id,
            "priority": 10,
            "when": {"event_types": [], "operator_identities": [], "authorized": None},
            "effects": {
                "responsible_member_ids": ["club"],
                "next_action_ids": ["record"],
            },
        },
    )
    cut = append("cut", owner, {"fabric_id": fabric_id})
    verdict = cut["event"]["verdict"]
    assert verdict["responsible_member_ids"] == ["club"], verdict
    assert verdict["next_action_ids"] == ["record"], verdict
    assert verdict["basis"], "裁决缺少逐条依据"

    # 查询接口
    status, view = request("GET", f"/api/events/{cut['seq']}")
    assert status == 200 and view["verdict"]["responsible_member_ids"] == ["club"], view
    status, events = request("GET", "/api/events")
    assert status == 200 and len(events["events"]) >= seq - 1, events
    print(
        f"   布料 {fabric_id} 剪裁裁决责任={verdict['responsible_member_ids']} "
        f"下一动作={verdict['next_action_ids']} 依据条数={len(verdict['basis'])}"
    )
    print("== HTTP 冒烟通过 ==", flush=True)


def main() -> int:
    wait_for_api()
    try:
        smoke()
    except AssertionError as exc:
        print(f"HTTP 冒烟失败: {exc}", file=sys.stderr)
        return 1

    print("== 运行 pytest ==", flush=True)
    env = dict(os.environ)
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=SRV_DIR,
        env=env,
    )
    if proc.returncode != 0:
        return proc.returncode

    print("== verify 全部通过，退出 ==", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
