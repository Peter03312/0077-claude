"""pytest 共享夹具：每个测试一个临时 SQLite 文件 + FastAPI TestClient。"""

from __future__ import annotations

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_events.db")
    monkeypatch.setenv("APP_DB_PATH", db_path)

    import app.main as main

    importlib.reload(main)  # 确保每个测试拿到全新的 Database 单例
    main._db = None

    with TestClient(main.app) as c:
        c.db_path = db_path
        yield c
