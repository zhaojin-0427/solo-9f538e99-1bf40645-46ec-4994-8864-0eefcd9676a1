"""pytest 公共夹具：每个测试使用隔离的临时 SQLite 库。"""

import tempfile

import pytest
from fastapi.testclient import TestClient

_TMP = tempfile.mkdtemp(prefix="contract-test-")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("CONTRACT_DB_PATH", db_path)
    # 数据库模块的初始化标志与路径在进程内缓存，需重置
    from app import database
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "_initialized", False)

    from app.main import app
    with TestClient(app) as c:
        yield c
