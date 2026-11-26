import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ops.demo import demo_reset
from reglament_agent.ops.health import Settings, health, make_router

DOCX = Path(__file__).resolve().parents[2] / "docx"
DEAD_PORT = 1  # nothing listens there


def settings(dsn, transport=None):
    return Settings(database_url=dsn, ollama_url="http://ollama.test", mail_host="127.0.0.1",
                    mail_port=DEAD_PORT, timeout=1, transport=transport)


def refuse(request):
    raise httpx.ConnectError("down")


def test_db_ok_others_down(fresh_db):
    demo_reset(fresh_db, DOCX, embedder=FakeEmbedder())
    s = settings(fresh_db, httpx.MockTransport(refuse))
    body = asyncio.run(health(s))
    assert not body["ok"] and body["checks"]["db"]["ok"] and "4 active documents" in body["checks"]["db"]["detail"]
    assert not body["checks"]["ollama"]["ok"] and not body["checks"]["smtp"]["ok"]
    app = FastAPI()
    app.include_router(make_router(s))
    r = TestClient(app).get("/health")
    assert r.status_code == 503 and r.json()["version"]


def test_ollama_models(fresh_db):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b-q4_K_M"}, {"name": "bge-m3:latest"}]})
        return httpx.Response(200, json={"models": [{"name": "bge-m3:latest"}]})
    body = asyncio.run(health(settings(fresh_db, httpx.MockTransport(handler))))
    assert body["checks"]["ollama"]["ok"] and "bge-m3" in body["checks"]["ollama"]["detail"]


def test_db_down_503():
    app = FastAPI()
    app.include_router(make_router(settings("postgresql://x@127.0.0.1:1/none", httpx.MockTransport(refuse))))
    r = TestClient(app).get("/health")
    assert r.status_code == 503 and r.json()["ok"] is False and not r.json()["checks"]["db"]["ok"]
