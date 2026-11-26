from html.parser import HTMLParser

from tests.admin.test_versions import BASE, DBRCT, DEMO, V, client, docx, env, job_of, pub, publish_via_job, review_setup, upload


class Controls(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.targets = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for key in ("href", "formaction", "action"):
            if values.get(key):
                self.targets.append(values[key])


def test_expert_can_reach_review_from_list(env):
    item = review_setup(env)
    c = client('expert')
    listing = c.get('/admin/faq?status=review')
    card, review = f'/admin/faq/{item}', f'/admin/faq/{item}/review'
    controls = Controls(listing.text).targets
    assert card in controls or review in controls
    assert review in controls or review in Controls(c.get(card).text).targets


def test_confirm_from_visible_card_records_expert(env):
    item = review_setup(env)
    c = client('expert')
    card = c.get(f'/admin/faq/{item}')
    action = next(s for s in Controls(card.text).targets if s.endswith('/confirm'))
    assert c.post(action).status_code == 303
    assert any(row['action'] == 'confirm' and row['actor'] == 'expert@x.local' for row in V.history(env, item))


def test_review_changes_before_replace(env):
    c = client('expert')
    publish_via_job(c, upload(c, DBRCT))
    current_doc = env.execute('SELECT doc_id FROM documents').fetchone()[0]
    pending = upload(c, DEMO, target=current_doc)
    assert f'/admin/jobs/{job_of(pending)}/changes' in Controls(pending.text).targets


def test_withdraw_form_applies_only_to_previewed_version(env):
    pub(env, BASE)
    first = client('expert')
    path = '/admin/documents/doc-x/withdraw'
    assert first.get(path).status_code == 200
    pub(env, docx('Регламент', '1. Раздел', '1.1. Изменённый этап.'))
    first.post(path)
    assert env.execute('SELECT status FROM documents WHERE doc_id=%s', ('doc-x',)).fetchone()[0] == 'active'


# --- round 2: targeted regressions ---------------------------------------------------------------

import re  # noqa: E402

from tests.admin.test_versions import faq, status  # noqa: E402

V2 = docx("Регламент", "1. Раздел", "1.1. Изменённый этап.")


def hidden_expected(html):
    return re.search(r'name="expected_version_id" value="(\d+)"', html).group(1)


def active_number(env):
    return env.execute("SELECT v.version_number FROM documents d JOIN document_versions v "
                       "ON v.version_id=d.active_version_id").fetchone()[0]


def test_stale_withdraw_form_changes_nothing_and_recomputes(env):
    pub(env, BASE)
    c = client("expert")
    path = "/admin/documents/doc-x/withdraw"
    expected = hidden_expected(c.get(path).text)
    pub(env, V2)
    r = c.post(path, data={"expected_version_id": expected})
    assert r.status_code == 409 and "Версия изменилась, проверьте последствия заново" in r.text
    assert env.execute("SELECT status FROM documents").fetchone()[0] == "active" and active_number(env) == 2
    fresh = hidden_expected(r.text)
    assert fresh != expected
    assert c.post(path, data={"expected_version_id": fresh}).status_code == 303
    assert env.execute("SELECT status FROM documents").fetchone()[0] == "inactive"


def test_stale_rollback_and_return_forms_change_nothing(env):
    pub(env, BASE)
    pub(env, V2)
    c = client("expert")
    path = "/admin/documents/doc-x/versions/1/activate"
    expected = hidden_expected(c.get(path).text)
    pub(env, docx("Регламент", "1. Раздел", "1.1. Третья редакция."))
    r = c.post(path, data={"expected_version_id": expected})
    assert r.status_code == 409 and "Версия изменилась" in r.text and active_number(env) == 3
    assert c.post(path).status_code == 409  # a form without the field is never applied


def test_restore_updates_only_entries_still_under_review(env):
    f = review_setup(env)
    c = client("expert")
    from reglament_agent.quality import faq as F

    real = F._check_entry

    def racing(conn, entry):
        conn.execute("UPDATE faq_entries SET status='deleted' WHERE id=%s", (entry["id"],))
        return real(conn, entry)

    env.execute("UPDATE faq_entries SET version_id=(SELECT min(version_id) FROM document_versions)")
    pytest_monkey = __import__("pytest").MonkeyPatch()
    pytest_monkey.setattr(F, "_check_entry", racing)
    try:
        c.post("/admin/documents/doc-x/versions/1/activate",
               data={"expected_version_id": env.execute("SELECT active_version_id FROM documents").fetchone()[0]})
    finally:
        pytest_monkey.undo()
    assert status(env, f) == "deleted"
    assert "auto_restore" not in [h["action"] for h in V.history(env, f)]


def test_document_level_entry_cannot_be_confirmed_while_withdrawn(env):
    pub(env, BASE)
    f = faq(env, "Что регулирует документ?", kind="document", points=[("doc-x", None)])
    c = client("expert")
    exp = env.execute("SELECT active_version_id FROM documents").fetchone()[0]
    c.post("/admin/documents/doc-x/withdraw", data={"expected_version_id": exp})
    assert status(env, f) == "review"
    r = c.post(f"/admin/faq/{f}/review/confirm")
    assert r.status_code == 400 and "снят с публикации" in r.text and status(env, f) == "review"
    old = c.post(f"/admin/faq/{f}/confirm", follow_redirects=True)
    assert "снят с публикации" in old.text and status(env, f) == "review"


def test_system_history_written_at_replacement_time(env):
    f = review_setup(env)
    rows = V.history(env, f)  # nobody opened the card yet
    assert [(h["actor"], h["action"]) for h in rows] == [("система", "to_review")]
    assert "изменён" in rows[0]["detail"]["reasons"][0] and rows[0]["detail"]["version"] == 2


def test_rollback_sends_to_review_with_history(env):
    pub(env, BASE)
    pub(env, docx("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет три дня.",
                  "1.2. Согласование выполняет директор департамента.", "1.3. Архив хранится пять лет."))
    f = faq(env, "Кто согласует?")
    c = client("expert")
    exp = env.execute("SELECT active_version_id FROM documents").fetchone()[0]
    c.post("/admin/documents/doc-x/versions/1/activate", data={"expected_version_id": exp})
    assert status(env, f) == "review"
    assert [h["action"] for h in V.history(env, f)] == ["to_review"]


def test_all_status_routes_write_one_history_line_with_version(env):
    f = review_setup(env)
    c = client("expert")
    assert c.post(f"/admin/faq/{f}/delete").status_code == 303  # old plain-card route
    last = V.history(env, f)[-1]
    assert (last["action"], last["actor"], last["detail"]["version"]) == ("delete", "expert@x.local", 2)
    assert last["created_at"] is not None
