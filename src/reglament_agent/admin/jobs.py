"""Publication jobs: DB-backed queue, in-process background runner, honest progress."""
from __future__ import annotations

from contextlib import nullcontext

import asyncio
import os
import time

import psycopg
from psycopg.types.json import Jsonb

from reglament_agent.admin.selfcheck import selfcheck
from reglament_agent.ingest.embedding import Embedder, open_embedder
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.quality.faq import recheck_faq_after_publication
from reglament_agent.storage.migrate import migrate
from reglament_agent.storage.publish import Publication, publish


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=5)


def ensure_schema() -> None:
    with connect() as conn:
        migrate(conn)


def update_job(job_id: int, *, status=None, stage=None, done=None, total=None, error=None,
               selfcheck_result=None, stats: dict | None = None, connection: psycopg.Connection | None = None) -> None:
    """Persist progress independently, or record a publication inside its transaction."""
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
    with (connect() if connection is None else nullcontext(connection)) as conn:
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
        row = conn.execute("SELECT card, filename, data, created_by FROM ingest_jobs WHERE id=%s",
                           (job_id,)).fetchone()
    card_data, filename, data, created_by = row
    card = DocumentCard(card_data["doc_id"], card_data["title"], card_data["department"],
                        tuple(card_data["stages"]), filename)
    started = publishing_at = time.time()
    update_job(job_id, status="running", stage="embed", done=0, total=0, stats={"started": started})

    def start_publishing() -> None:
        nonlocal publishing_at
        publishing_at = time.time()
        update_job(job_id, stage="publish", stats={"publishing": publishing_at})

    def record_publication(conn: psycopg.Connection, pub: Publication) -> None:
        faq_review = recheck_faq_after_publication(conn, card.doc_id)
        done, total = conn.execute("SELECT done, total FROM ingest_jobs WHERE id=%s", (job_id,)).fetchone()
        published = time.time()
        update_job(job_id, stage="selfcheck", done=0, total=pub.stats["points"] - pub.stats["levels"].get("1", 0),
                   stats={"version": pub.number, "faq_review": faq_review, "embed": {"done": done, "total": total},
                          "publish_sec": round(published - publishing_at, 2), "published": published,
                          "publication": pub.stats},
                   connection=conn)

    try:
        with (open_embedder() if embedder is None else nullcontext(embedder)) as inner, connect() as conn:
            pub = publish(conn, card, bytes(data), ProgressEmbedder(inner, job_id), created_by,
                          on_publish_start=start_publishing, before_commit=record_publication)
            if not pub.created:
                update_job(job_id, status="duplicate", stage="publish",
                           stats={"version": pub.number, "finished": time.time()})
                return
            checking_at = time.time()
            result = selfcheck(conn, card.doc_id, lambda k, n: update_job(job_id, done=k, total=n))
        finished = time.time()
        update_job(job_id, status="ready", stage="ready", selfcheck_result=result,
                   stats={"finished": finished, "selfcheck_sec": round(finished - checking_at, 2)})
    except Exception as exc:  # noqa: BLE001 - stored for the expert
        update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}", stats={"finished": time.time()})


def claim_next() -> int | None:
    with connect() as conn:
        row = conn.execute("""UPDATE ingest_jobs SET status='running', updated_at=now() WHERE id=(
            SELECT id FROM ingest_jobs WHERE status='queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING id""").fetchone()
    return row[0] if row else None


async def runner_loop(stop: asyncio.Event, poll: float = 0.5) -> None:
    initialized = False
    while not stop.is_set():
        delay = poll
        try:
            if not initialized:
                await asyncio.to_thread(ensure_schema)
                # A restart leaves 'running' rows nobody works on. Queued rows remain eligible.
                await asyncio.to_thread(update_orphans)
                initialized = True
            if stop.is_set():
                return
            job_id = await asyncio.to_thread(claim_next)
            if job_id is not None:
                await asyncio.to_thread(run_job, job_id)
                continue
        except Exception:  # noqa: BLE001 - retry after transient database failures
            delay = 2
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


def update_orphans() -> None:
    with connect() as conn:
        conn.execute("UPDATE ingest_jobs SET status='failed', error='Прервано перезапуском сервера', "
                     "updated_at=now() WHERE status='running'")


_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None


def ensure_runner() -> None:
    """Start the background runner on the application loop."""
    global _task, _stop
    if os.environ.get("ADMIN_BACKGROUND") == "0" or not os.environ.get("DATABASE_URL"):
        return
    if _task is None or _task.done():
        _stop = asyncio.Event()
        _task = asyncio.create_task(runner_loop(_stop))


async def stop_runner() -> None:
    """Finish any current job before closing the application loop."""
    global _task, _stop
    if _task is not None and _stop is not None:
        _stop.set()
        await _task
        _task, _stop = None, None
