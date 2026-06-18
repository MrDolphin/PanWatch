"""设置页头像:存取 + 路由优先级(PUT /avatar 不被 /{key} 抢匹配)+ 不污染通用设置列表。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401 (确保模型注册到 Base)
from src.web.api import settings as settings_api
from src.web.database import Base, get_db


def _client() -> TestClient:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(settings_api.router, prefix="/settings")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_avatar_default_empty():
    """未设置时头像为空字符串。"""
    client = _client()
    r = client.get("/settings/avatar")
    assert r.status_code == 200
    assert r.json()["value"] == ""


def test_avatar_set_get_roundtrip():
    """通用 PUT /settings/ui_avatar 写入后,GET /settings/avatar 能读回(读写不依赖路由顺序)。"""
    client = _client()
    data_url = "data:image/png;base64,AAAA"
    r = client.put("/settings/ui_avatar", json={"value": data_url})
    assert r.status_code == 200, r.text
    assert client.get("/settings/avatar").json()["value"] == data_url


def test_avatar_not_in_generic_list():
    """头像存于独立 key,不应出现在通用设置列表(避免大 base64 污染)。"""
    client = _client()
    client.put("/settings/ui_avatar", json={"value": "data:image/png;base64,XYZ"})
    keys = [s["key"] for s in client.get("/settings").json()]
    assert "ui_avatar" not in keys


def test_avatar_clear():
    """传空字符串可清空头像。"""
    client = _client()
    client.put("/settings/ui_avatar", json={"value": "data:image/png;base64,XYZ"})
    client.put("/settings/ui_avatar", json={"value": ""})
    assert client.get("/settings/avatar").json()["value"] == ""
