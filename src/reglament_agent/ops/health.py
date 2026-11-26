"""Stand health checks and a mountable FastAPI router. Independent of chat code."""
import asyncio
import os
import subprocess
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import httpx
import psycopg
from fastapi import APIRouter, Response

CRITICAL = ("db",)


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3.5:9b-q4_K_M"
    embed_model: str = "bge-m3"
    mail_host: str = "127.0.0.1"
    mail_port: int = 1025
    timeout: float = 3.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, compare=False)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(database_url=e("DATABASE_URL", ""),
                   ollama_url=e("MODEL_OLLAMA_URL", cls.ollama_url),
                   chat_model=e("MODEL_CHAT", cls.chat_model), embed_model=e("MODEL_EMBED", cls.embed_model),
                   mail_host=e("MAIL_HOST", cls.mail_host), mail_port=int(e("MAIL_PORT", cls.mail_port)))


def app_version() -> str:
    try:
        out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], capture_output=True,
                             text=True, timeout=2, cwd=Path(__file__).parent)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        return metadata.version("reglament-agent")
    except Exception:
        return "unknown"


def _db_sync(dsn: str, timeout: float) -> str:
    with psycopg.connect(dsn, connect_timeout=max(1, int(timeout)), autocommit=True) as conn:
        conn.execute("SELECT 1")
        ext = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        try:
            docs = conn.execute("SELECT count(*) FROM documents WHERE status='active'").fetchone()[0]
            pts = conn.execute("SELECT count(*) FROM points p JOIN documents d "
                               "ON d.active_version_id=p.version_id").fetchone()[0]
            data = f"{docs} active documents, {pts} points"
        except psycopg.Error:
            data = "schema not migrated"
        return f"pgvector {ext[0] if ext else 'missing'}; {data}"


async def _timed(name: str, coro, timeout: float) -> dict:
    start = time.monotonic()
    try:
        ok, detail = await asyncio.wait_for(coro, timeout)
    except Exception as exc:  # a check must never raise
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:200]}"
    return {"name": name, "ok": ok, "ms": round((time.monotonic() - start) * 1000), "detail": detail}


async def _check_db(s: Settings):
    if not s.database_url:
        return False, "DATABASE_URL is not set"
    detail = await asyncio.to_thread(_db_sync, s.database_url, s.timeout)
    return "pgvector missing" not in detail, detail


async def _check_ollama(s: Settings):
    async with httpx.AsyncClient(base_url=s.ollama_url, timeout=s.timeout, trust_env=False,
                                 transport=s.transport) as client:
        tags = (await client.get("/api/tags")).raise_for_status().json()
        names = {m.get("name") for m in tags.get("models", [])} | {m.get("model") for m in tags.get("models", [])}
        have = lambda m: m in names or f"{m}:latest" in names
        missing = [m for m in (s.chat_model, s.embed_model) if not have(m)]
        try:
            ps = (await client.get("/api/ps")).raise_for_status().json()
            loaded = sorted(m.get("name", "") for m in ps.get("models", []))
        except Exception:
            loaded = []
    detail = f"missing models: {missing}" if missing else "models present"
    return not missing, f"{detail}; loaded: {loaded or 'none'}"


async def _check_smtp(s: Settings):
    _, writer = await asyncio.open_connection(s.mail_host, s.mail_port)
    writer.close()
    return True, f"{s.mail_host}:{s.mail_port} reachable"


async def health(settings: Settings) -> dict:
    checks = await asyncio.gather(
        _timed("db", _check_db(settings), settings.timeout + 1),
        _timed("ollama", _check_ollama(settings), settings.timeout + 1),
        _timed("smtp", _check_smtp(settings), settings.timeout))
    return {"ok": all(c["ok"] for c in checks if c["name"] in CRITICAL), "version": app_version(),
            "checks": {c.pop("name"): c for c in checks}}


def make_router(settings: Settings | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def get_health(response: Response) -> dict:
        body = await health(settings or Settings.from_env())
        response.status_code = 200 if body["ok"] else 503
        return body

    return router


router = make_router()
