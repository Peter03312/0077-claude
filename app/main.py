"""FastAPI 入口：事件追加、规则裁决（随追加触发）、历史查询。

所有写操作只发生在 POST /api/events；读接口全部从事件流确定性重放得到。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import ValidationError

from . import replay
from .database import Database
from .errors import RejectEvent
from .models import EventIn, validate_payload

DB_PATH = os.environ.get("APP_DB_PATH", os.path.join("data", "events.db"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_db()
    yield


app = FastAPI(title="服装社布料流转裁决 API", version="1.0.0", lifespan=lifespan)
_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(DB_PATH)
    return _db


def _canonical_hash(event_type: str, submitted_by: str, payload: dict) -> str:
    """幂等指纹：同键下保证“同载荷”判定稳定（键名排序、无空白差异）。"""
    envelope = json.dumps(
        {"type": event_type, "submitted_by": submitted_by, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


@app.get("/health")
def health() -> dict:
    db = get_db()
    return {"status": "ok", "next_seq": db.next_seq()}


@app.post("/api/events", status_code=201)
def append_event(
    body: EventIn,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """原子追加事件：expected_next_seq 乐观并发 + Idempotency-Key 幂等。"""
    db = get_db()
    payload_hash = _canonical_hash(body.type, body.submitted_by, body.payload)

    # 0. 幂等短路必须先于领域校验：同键同载荷即使重复提交的是 setup 事件，
    #    也直接复用既有序号；同键异载荷立即冲突。最终判定仍以事务行为准。
    if idempotency_key is not None:
        hit = db.find_idempotency(idempotency_key)
        if hit is not None:
            seq_existing, hash_existing = hit
            if hash_existing != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "idempotency_conflict",
                        "msg": "Idempotency-Key 已用于不同载荷",
                    },
                )
            rows = db.load_raw_events()
            views = {v["seq"]: v for v in replay.event_views(rows)}
            return {"seq": seq_existing, "reused": True, "event": views[seq_existing]}

    # 1. 载荷按类型做结构校验
    try:
        validate_payload(body.type, body.payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    rows = db.load_raw_events()
    candidate = replay.RawEvent(
        seq=body.expected_next_seq,
        type=body.type,
        submitted_by=body.submitted_by,
        payload=body.payload,
    )

    # 2. 追加前领域校验：
    #    - 更正：原提交者 + 完整重放不使后续引用失效
    #    - 其他事件：在“含候选事件”的流上试折叠，裁决/投影不变量全部走一遍
    try:
        if body.type == "correct":
            replay.validate_correction(rows, candidate)
        else:
            replay.fold(rows + [candidate])
    except RejectEvent as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "msg": str(exc)})

    # 3. 原子落库（序号 + 幂等键竞争在此裁决；并发下可能在此处得到 409/复用）
    try:
        seq, reused = db.append_event(
            event_type=body.type,
            submitted_by=body.submitted_by,
            payload=body.payload,
            payload_hash=payload_hash,
            expected_next_seq=body.expected_next_seq,
            idempotency_key=idempotency_key,
        )
    except RejectEvent as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "msg": str(exc)})

    # 4. 返回该序号的权威视图（落库后重放）
    rows = db.load_raw_events()
    views = {v["seq"]: v for v in replay.event_views(rows)}
    view = views.get(seq, {
        "seq": seq,
        "type": body.type,
        "submitted_by": body.submitted_by,
        "payload": body.payload,
    })
    return {"seq": seq, "reused": reused, "event": view}


@app.get("/api/events")
def list_events(as_of: Optional[int] = Query(default=None, ge=1)) -> dict:
    """历史查询：全部原始事件 + 各事件当前（或 as_of 时点）的裁决。"""
    rows = get_db().load_raw_events()
    return {"as_of": as_of, "events": replay.event_views(rows, as_of=as_of)}


@app.get("/api/events/{seq}")
def get_event(seq: int, as_of: Optional[int] = Query(default=None, ge=1)) -> dict:
    """查询单序号：动作事件给出责任、下一动作、逐条依据、申诉补救；
    更正事件同时给出其替代后的 effective_event 与重放裁决。"""
    rows = get_db().load_raw_events()
    for view in replay.event_views(rows, as_of=as_of):
        if view["seq"] == seq:
            return view
    raise HTTPException(status_code=404, detail={"code": "not_found", "msg": f"序号 {seq} 不存在"})


@app.get("/api/state")
def get_state(as_of: Optional[int] = Query(default=None, ge=1)) -> dict:
    """当前（或 as_of 时点）投影：成员、布料主/持有人、规则版本、授权、裁决。"""
    rows = get_db().load_raw_events()
    return replay.state_at(rows, as_of=as_of)
