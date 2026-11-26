"""admin-b04 round 3: regressions from the independent review (stale version, tickets, validation, gold link)."""
import asyncio
import csv
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reglament_agent.admin import analytics
from reglament_agent.admin import checks
from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.storage.publish import publish
from reglament_agent.storage.retrieve import Candidates
from tests.admin.test_check_questions import (CARDS, DEMO, DOC_IDS, DOCX, EMB, ROOT, SECRET, ans, client, env,  # noqa: F401
                                              load_corpus, norm)
from tests.dialog.test_route import Model


def publish_version(conn, v2=False):
    cfg = next(card for card in CARDS if card["doc_id"] == "dbrct-project")
    path = DEMO if v2 else DOCX / cfg["file"]
    card = DocumentCard(cfg["doc_id"], cfg["title"], cfg["department"], tuple(cfg["stages"]), path.name)
    publish(conn, card, path.read_bytes(), EMB, "private-review")


# --- 1. outdated source must not pass -------------------------------------------------------------

def test_outdated_reference_cannot_pass_after_actual_document_replacement(env):
    publish_version(env)
    entry = checks.load_questions("dbrct-project").questions[0]
    publish_version(env, v2=True)
    old_answer = ans("1.5", version=1, text="До отправки предложения осталось 3 рабочих дня.")
    verdict = checks.evaluate(env, "dbrct-project", entry, old_answer, 0.1)
    assert not verdict["passed"] and verdict["status"] == "failed"
    assert "ссылка на устаревшую версию 1, активна 2" in verdict["reason"]


def test_changed_status_needs_a_source_from_the_active_version(env):
    publish_version(env)
    publish_version(env, v2=True)
    entry = checks.load_questions("dbrct-project").questions[0]
    current = checks.evaluate(env, "dbrct-project", entry, ans("1.5", version=2, text="5 рабочих дней"), 0)
    assert current["status"] == "changed" and not current["passed"]
    stale = checks.evaluate(env, "dbrct-project", entry, ans("1.5", version=1, text="3 рабочих дня"), 0)
    assert stale["status"] == "failed"


# --- 2. service runs never open tickets or mails ---------------------------------------------------

def _dialog(dsn):
    model = Model()
    model.result = {"outcome": "no_basis", "claims": []}
    return Dialog(dsn, model, search=lambda *a, **kw: Candidates([], []), shortlist=lambda *a, **kw: [])


def _counts(conn):
    return conn.execute("SELECT (SELECT count(*) FROM tickets),(SELECT count(*) FROM mail_outbox)").fetchone()


async def _status(_):
    pass


def test_service_account_no_basis_creates_no_ticket_and_no_mail(env):
    dialog = _dialog(os.environ["DATABASE_URL"])
    user = UserContext(checks.SERVICE_EMAIL, "expert", "checks-1", "checks-1")
    reply = asyncio.run(dialog.answer(user, "Как оформить отпуск?", _status))
    assert reply.outcome == Outcome.NO_BASIS
    assert reply.ticket is not None and "обращение не создано" in reply.ticket.reason
    assert _counts(env) == (0, 0)
    assert len(analytics.fetch(env, "all")) == 0


def test_ordinary_employee_no_basis_still_creates_ticket(env):
    dialog = _dialog(os.environ["DATABASE_URL"])
    user = UserContext("ivan@corp.kz", "employee", "d1", "m1")
    reply = asyncio.run(dialog.answer(user, "Как оформить отпуск?", _status))
    assert reply.ticket and "обращение не создано" not in reply.ticket.reason
    assert _counts(env) == (1, 1)


def test_unsuccessful_check_is_logged_but_does_not_open_operational_ticket(env, monkeypatch):
    publish_version(env)
    qset = checks.load_questions("dbrct-project")
    monkeypatch.setattr(checks, "load_questions",
                        lambda *_: checks.CheckSet(qset.doc_id, qset.department, [qset.questions[0]]))
    dialog = _dialog(os.environ["DATABASE_URL"])
    services = SimpleNamespace(answerer=dialog, profiles=dialog.profiles)
    run = checks.start_run(qset.doc_id, "reviewer@qa.invalid")
    asyncio.run(checks.execute_run(run, services=services))
    saved = checks.last_runs(env, qset.doc_id)[0]
    logs = env.execute("SELECT question,answer->>'outcome' FROM dialog_requests WHERE user_email=%s",
                       (checks.SERVICE_EMAIL,)).fetchall()
    assert logs == [(qset.questions[0].question, "no_basis")]
    assert saved["results"][0]["status"] == "failed" and _counts(env) == (0, 0)
    assert len(analytics.fetch(env, "all")) == 0


# --- 3. the question set is validated before a run starts --------------------------------------------

BAD = {
    "other document": 'doc_id="td-project"\ndepartment="ДБРЦТ"\n[[question]]\nquestion="Срок?"\npoint="1.5"\n',
    "unknown document": 'doc_id="ghost"\ndepartment="ДБРЦТ"\n[[question]]\nquestion="Срок?"\npoint="1.5"\n',
    "unknown outcome": 'doc_id="dbrct-project"\ndepartment="ДБРЦТ"\n[[question]]\nquestion="Срок?"\npoint="1.5"\noutcome="answr"\n',
    "duplicate": 'doc_id="dbrct-project"\ndepartment="ДБРЦТ"\n[[question]]\nquestion="Срок?"\n[[question]]\nquestion=" срок? "\n',
    "empty question": 'doc_id="dbrct-project"\ndepartment="ДБРЦТ"\n[[question]]\nquestion="  "\n',
    "no department": 'doc_id="dbrct-project"\n[[question]]\nquestion="Срок?"\n',
    "no questions": 'doc_id="dbrct-project"\ndepartment="ДБРЦТ"\n',
}


@pytest.mark.parametrize("case", list(BAD))
def test_invalid_check_file_gives_400_and_no_run(env, monkeypatch, tmp_path, case):
    load_corpus(env)
    (tmp_path / "dbrct-project.toml").write_text(BAD[case], encoding="utf-8")
    monkeypatch.setenv("CHECKS_DIR", str(tmp_path))
    monkeypatch.setattr(checks, "spawn", lambda fn: None)
    r = client("expert").post("/admin/documents/dbrct-project/checks/run")
    assert r.status_code == 400 and "Набор проверочных вопросов не принят" in r.text
    assert env.execute("SELECT count(*) FROM check_runs").fetchone()[0] == 0
    page = client("expert").get("/admin/documents/dbrct-project/checks")
    assert page.status_code == 400 and "Набор проверочных вопросов не принят" in page.text


# --- 4. questions come verbatim from the approved gold set --------------------------------------------

GOLD = Path(os.environ.get("GOLD_CASES") or ROOT.parent / "preparation" / "runs" / "gold-suite" / "cases.tsv")


@pytest.mark.skipif(not GOLD.is_file(), reason="cases.tsv of the gold set is not available")
def test_every_check_question_is_a_verbatim_gold_question_with_gold_sources():
    gold = {r["id"]: r for r in csv.DictReader(GOLD.open(encoding="utf-8"), delimiter="\t")}
    for doc_id in DOC_IDS:
        cs = checks.load_questions(doc_id)
        assert cs.questions
        for q in cs.questions:
            assert q.gold_id in gold, (doc_id, q.question)
            row = gold[q.gold_id]
            assert row["outcome"] == "answer" and row["department"] == cs.department
            wording = row["paraphrase"] if q.kind == "перефраз" else row["question"]
            assert q.question == wording, (q.gold_id, q.question)
            sources = {s.split(":")[0] + ":" + s.split(":")[1] for s in row["sources"].split("|")}
            assert f"{doc_id}:{q.point}" in sources
            assert {q.phrase, *q.facts} == set(row["facts"].split("|"))
    assert any(q.kind == "перефраз" for doc in DOC_IDS for q in checks.load_questions(doc).questions)
