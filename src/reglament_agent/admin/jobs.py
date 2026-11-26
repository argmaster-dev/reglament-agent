"""Publication jobs: DB-backed queue, in-process background runner, honest progress."""
from __future__ import annotations

import asyncio
import os
import time

import psycopg
from psycopg.types.json import Jsonb

from reglament_agent.admin.selfcheck import selfcheck
from reglament_agent.ingest.embedding import Embedder, make_embedder
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.storage.migrate import migrate
from reglament_agent.storage.publish import publish


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def ensure_schema() -> None:
    with connect() as conn:
        migrate(conn)


def update_job(job_id: int, *, status=None, stage=None, done=None, total=None, error=None,
               selfcheck_result=None, stats: dict | None = None) -> None:
    """Short separate connection so progress is visible to page polling immediately."""
    sets, params = ["updated_at=now()"], []
    for column, value in (("status", status), ("stage", stage), ("done", done), ("total", total),
                          ("error", error)):
        if value is not None:
            sets.append(f"{column}=%s")
            params.append(value)
    if selfcheck_result is not None:
        sets.append("selfcheck=%s")
        params.append(Jsonb(selfcheck_result))
    if stats:
        sets.append("stats=stats || %s")
        params.append(Jsonb(stats))
    with connect() as conn:
        conn.execute(f"UPDATE ingest_jobs SET {', '.join(sets)} WHERE id=%s", (*params, job_id))


class ProgressEmbedder:
    """Wraps an embedder; reports done/total per batch to the job row."""

    def __init__(self, inner: Embedder, job_id: int, batch: int = 16):
        self.inner, self.job_id, self.batch = inner, job_id, batch

    def embed(self, texts):
        texts = list(texts)
        update_job(self.job_id, stage="embed", done=0, total=len(texts))
        result = []
        for i in range(0, len(texts), self.batch):
            result.extend(self.inner.embed(texts[i:i + self.batch]))
            update_job(self.job_id, done=len(result))
        return result


def run_job(job_id: int, embedder: Embedder | None = None) -> None:
    """Run one queued job to completion (blocking). Never raises: failures are stored on the job."""
    with connect() as conn:
        row = conn.execute("SELECT card, filename, data, created_by, stats FROM ingest_jobs WHERE id=%s",
                           (job_id,)).fetchone()
    card_data, filename, data, created_by, _ = row
    card = DocumentCard(card_data["doc_id"], card_data["title"], card_data["department"],
                        tuple(card_data["stages"]), filename)
    started = time.time()
    update_job(job_id, status="running", stage="embed", done=0, total=0, stats={"started": started})
    try:
        inner = embedder or make_embedder()
        with connect() as conn:
            t_publish = time.time()
            pub = publish(conn, card, bytes(data), ProgressEmbedder(inner, job_id), created_by)
            published = time.time()
            if not pub.created:
                update_job(job_id, status="duplicate", stage="publish",
                           stats={"version": pub.number, "finished": published})
                return
            with connect() as c2:
                total = c2.execute("SELECT done, total FROM ingest_jobs WHERE id=%s", (job_id,)).fetchone()
            update_job(job_id, stage="selfcheck", done=0, total=pub.stats["points"], stats={
                "version": pub.number, "embed": {"done": total[0], "total": total[1]},
                "publish_sec": round(published - t_publish, 2), "published": published,
                "publication": pub.stats})
            result = selfcheck(conn, card.doc_id, lambda k, n: update_job(job_id, done=k, total=n))
        finished = time.time()
        update_job(job_id, status="ready", stage="ready", selfcheck_result=result,
                   stats={"finished": finished, "selfcheck_sec": round(finished - published, 2)})
    except Exception as exc:  # noqa: BLE001 - stored for the expert, never swallowed silently
        update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}", stats={"finished": time.time()})


def claim_next() -> int | None:
    with connect() as conn:
        row = conn.execute("""UPDATE ingest_jobs SET status='running', updated_at=now() WHERE id=(
            SELECT id FROM ingest_jobs WHERE status='queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING id""").fetchone()
    return row[0] if row else None


async def runner_loop(poll: float = 0.5) -> None:
    # A restart leaves 'running' rows nobody works on: fail them honestly.
    await asyncio.to_thread(lambda: update_orphans())
    while True:
        try:
            job_id = await asyncio.to_thread(claim_next)
            if job_id is None:
                await asyncio.sleep(poll)
                continue
            await asyncio.to_thread(run_job, job_id)
        except Exception:  # noqa: BLE001 - keep the loop alive (e.g. DB briefly down)
            await asyncio.sleep(2)


def update_orphans() -> None:
    with connect() as conn:
        conn.execute("UPDATE ingest_jobs SET status='failed', error='Прервано перезапуском сервера', "
                     "updated_at=now() WHERE status='running'")


_task: asyncio.Task | None = None


def ensure_runner() -> None:
    """Start the background runner on the running event loop (idempotent)."""
    global _task
    if os.environ.get("ADMIN_BACKGROUND") == "0":
        return
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(runner_loop())
