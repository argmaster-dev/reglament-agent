"""Round 4: one lock order (documents by doc_id, then FAQ rows) and checks over ALL basis documents."""
import threading

from reglament_agent.ingest.model import DocumentCard

from tests.admin.test_versions import V, client, docx, env, faq, pub, status  # noqa: F401

A = DocumentCard("doc-a", "Документ А", "Отдел", ("project",), "a.docx")
B = DocumentCard("doc-b", "Документ Б", "Отдел", ("project",), "b.docx")


def text(n):
    return docx("Регламент", "1. Раздел", f"1.1. Срок составляет {n} дня.")


def setup_pair(env):
    pub(env, text("три"), A)
    pub(env, text("три"), B)
    f1 = faq(env, "Первый вопрос?", points=[("doc-a", "1.1"), ("doc-b", "1.1")])
    f2 = faq(env, "Второй вопрос?", points=[("doc-b", "1.1"), ("doc-a", "1.1")])
    pub(env, text("пять"), A)
    pub(env, text("пять"), B)
    assert status(env, f1) == status(env, f2) == "review"
    return f1, f2


def test_token_covers_every_basis_document(env):
    f1, _ = setup_pair(env)
    token = V.shown_token(env, V.F.get_faq(env, f1))
    assert token["expected_version_id"].count(",") == 1 and token["expected_hashes"].count(",") == 1


def test_change_of_second_document_makes_form_stale(env):
    f1, _ = setup_pair(env)
    c = client("expert")
    old = V.shown_token(env, V.F.get_faq(env, f1))
    pub(env, text("семь"), B)  # only the SECOND basis document changes
    r = c.post(f"/admin/faq/{f1}/review/confirm", data=old)
    assert r.status_code == 409 and status(env, f1) == "review"


def test_parallel_operations_over_two_documents_do_not_deadlock(env):
    results, errors = [], []
    setup_pair(env)
    ids = [r[0] for r in env.execute("SELECT id FROM faq_entries ORDER BY id").fetchall()]
    c = client("expert")
    tokens = {i: V.shown_token(env, V.F.get_faq(env, i)) for i in ids}
    barrier = threading.Barrier(3)

    def confirm(i):
        barrier.wait()
        try:
            results.append(c.post(f"/admin/faq/{i}/review/confirm", data=tokens[i]).status_code)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def withdraw():
        barrier.wait()
        try:
            exp = env.execute("SELECT active_version_id FROM documents WHERE doc_id='doc-b'").fetchone()[0]
            results.append(c.post("/admin/documents/doc-b/withdraw", data={"expected_version_id": exp}).status_code)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=confirm, args=(ids[0],)), threading.Thread(target=confirm, args=(ids[1],)),
               threading.Thread(target=withdraw)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads) and errors == [] and len(results) == 3
    assert all(code in (303, 409, 400) for code in results), results
