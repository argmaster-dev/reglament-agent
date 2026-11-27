"""Stand health checks and a mountable FastAPI router. Independent of chat code."""
import asyncio
import os
import subprocess
import time
from dataclasses import dataclass, field
from functools import cache
from importlib import metadata
from pathlib import Path

import httpx
import psycopg
from fastapi import APIRouter, Response

from reglament_agent.model.settings import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_URL,
    ModelSettings,
)

from .provenance import (
    build_details,
    deployment_image_digests,
    release_info as release_info,
    valid_sha256_digest,
)


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    ollama_url: str = DEFAULT_OLLAMA_URL
    chat_model: str = DEFAULT_CHAT_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL
    mail_host: str = "127.0.0.1"
    mail_port: int = 1025
    timeout: float = 3.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, compare=False)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        model = ModelSettings()
        return cls(
            database_url=e("DATABASE_URL", ""),
            ollama_url=model.ollama_url,
            chat_model=model.chat_model,
            embed_model=model.embed_model,
            mail_host=e("MAIL_HOST", cls.mail_host),
            mail_port=int(e("MAIL_PORT", cls.mail_port)),
        )


@cache
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


def _db_sync(dsn: str, timeout: float) -> tuple[str, str | None]:
    with psycopg.connect(dsn, connect_timeout=max(1, int(timeout)), autocommit=True) as conn:
        conn.execute("SELECT 1")
        ext = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        try:
            schema_version = conn.execute("SELECT max(name) FROM schema_migrations").fetchone()[0]
        except psycopg.errors.UndefinedTable:
            schema_version = None
        try:
            docs = conn.execute("SELECT count(*) FROM documents WHERE status='active'").fetchone()[0]
            pts = conn.execute("SELECT count(*) FROM points p JOIN documents d "
                               "ON d.active_version_id=p.version_id").fetchone()[0]
            data = f"{docs} active documents, {pts} points"
        except psycopg.Error:
            data = "schema not migrated"
        return f"pgvector {ext[0] if ext else 'missing'}; {data}", schema_version


async def _timed(name: str, coro, timeout: float) -> tuple[str, dict, dict]:
    start = time.monotonic()
    try:
        ok, detail, provenance = await asyncio.wait_for(coro, timeout)
    except Exception as exc:  # a check must never raise
        ok, detail, provenance = False, f"{type(exc).__name__}: {str(exc)[:200]}", {}
    return name, {
        "ok": ok, "ms": round((time.monotonic() - start) * 1000), "detail": detail,
    }, provenance


async def _check_db(s: Settings):
    if not s.database_url:
        return False, "DATABASE_URL is not set", {}
    detail, schema_version = await asyncio.to_thread(_db_sync, s.database_url, s.timeout)
    ok = "pgvector missing" not in detail and "schema not migrated" not in detail
    return ok, detail, {"schema_version": schema_version}


async def _check_ollama(s: Settings):
    async with httpx.AsyncClient(base_url=s.ollama_url, timeout=s.timeout, trust_env=False,
                                 transport=s.transport) as client:
        tags = (await client.get("/api/tags")).raise_for_status().json()
        available = {}
        for entry in tags.get("models", []):
            for name in (entry.get("name"), entry.get("model")):
                if name:
                    available[name] = entry.get("digest")
        missing = []
        digests = {}
        for name in (s.chat_model, s.embed_model):
            tag = name if name in available else f"{name}:latest"
            if tag not in available:
                missing.append(name)
            elif valid_sha256_digest(available[tag]):
                digests[name] = available[tag]
        try:
            ps = (await client.get("/api/ps")).raise_for_status().json()
            loaded = sorted(m.get("name", "") for m in ps.get("models", []))
        except Exception:
            loaded = []
    detail = f"missing models: {missing}" if missing else "models present"
    return not missing, f"{detail}; loaded: {loaded or 'none'}", {"model_digests": digests}


async def _check_smtp(s: Settings):
    _, writer = await asyncio.open_connection(s.mail_host, s.mail_port)
    writer.close()
    return True, f"{s.mail_host}:{s.mail_port} reachable", {}


async def health(settings: Settings) -> dict:
    (build, source_status), *probes = await asyncio.gather(
        asyncio.to_thread(build_details),
        _timed("db", _check_db(settings), settings.timeout + 1),
        _timed("ollama", _check_ollama(settings), settings.timeout + 1),
        _timed("smtp", _check_smtp(settings), settings.timeout),
    )
    checks = {}
    provenance = {"schema_version": None, "model_digests": {}}
    for name, check, facts in probes:
        checks[name] = check
        provenance.update(facts)
    image_digests = deployment_image_digests()
    return {
        "ok": source_status != "invalid" and all(check["ok"] for check in checks.values()),
        "version": app_version(),
        "checks": checks,
        "build": build,
        "source_digest_status": source_status,
        "image_digests": image_digests,
        "image_digest_source": "deployment_snapshot" if image_digests else None,
        **provenance,
    }


def make_router(settings: Settings | None = None) -> APIRouter:
    # Chainlit attaches the routes directly, so warm provenance before serving requests.
    build_details()
    router = APIRouter()

    @router.get("/health")
    async def get_health(response: Response) -> dict:
        body = await health(settings or Settings.from_env())
        response.status_code = 200 if body["ok"] else 503
        return body

    return router


router = make_router()
