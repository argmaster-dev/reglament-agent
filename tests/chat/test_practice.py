"""К3: подтверждённые ответы службы качества показаны как «Практика применения» пункта регламента.

Регламент и записи FAQ вымышленные; PostgreSQL настоящий, модель не нужна.
"""

from html.parser import HTMLParser

import pytest
from psycopg.types.json import Jsonb

from reglament_agent.answer import SourceRef
from reglament_agent.chat import render as R
from reglament_agent.quality import set_faq_status
from reglament_agent.storage.sources import point_view

ZERO = "(SELECT array_fill(0.01::real, ARRAY[1024])::vector)"
XSS = '<script>alert(1)</script><img src=x onerror=alert(2)>'


@pytest.fixture
def conn(isolated):
    return isolated[0]


def add_doc(conn, doc_id="zakup", title="Правила закупок", department="ОЗ", points=("2.1", "2.2")):
    conn.execute("INSERT INTO documents(doc_id,title,department,stages) VALUES (%s,%s,%s,'{project}')",
                 (doc_id, title, department))
    vid = conn.execute(
        "INSERT INTO document_versions(doc_id,version_number,sha256,filename,uploaded_by,source_docx,stats) "
        "VALUES (%s,1,%s,'x.docx','t',''::bytea,'{}') RETURNING version_id", (doc_id, "v1" + doc_id)).fetchone()[0]
    for pos, number in enumerate(points):
        conn.execute(
            f"INSERT INTO points(doc_id,version_id,number,level,section_number,section_title,heading,text,blocks,"
            f"sentences,position,sha256,embedding) VALUES (%s,%s,%s,2,'2','Закупки','Пункт',%s,'[]'::jsonb,%s,%s,%s,"
            f"{ZERO})", (doc_id, vid, number, f"Текст пункта {number}.", [f"Текст пункта {number}."], pos,
                         f"h{doc_id}{number}"))
    conn.execute("UPDATE documents SET active_version_id=%s WHERE doc_id=%s", (vid, doc_id))
    return vid


def add_faq(conn, vid, question, answer, numbers=("2.1",), status="active", doc_id="zakup", kind="points"):
    basis = [{"doc_id": doc_id, "doc_title": "Правила закупок", "number": n, "sha256": f"h{doc_id}{n}",
              "section_path": "2 Закупки", "column": None, "parent": None} for n in numbers]
    row = conn.execute(
        f"INSERT INTO faq_entries(question,answer,basis_kind,doc_id,version_id,basis,author_email,status,embedding,"
        f"created_at,reviewed_at) VALUES (%s,%s,%s,%s,%s,%s,'exp@corp.kz',%s,{ZERO},'2026-11-20T10:00:00+00',"
        f"'2026-11-25T09:00:00+00') RETURNING id", (question, answer, kind, doc_id, vid, Jsonb(basis), status))
    return row.fetchone()[0]


def view(conn, point="2.1", doc_id="zakup", version=1):
    return point_view(conn, SourceRef(doc_id, "Правила закупок", "ОЗ", version, point))


def questions(pv):
    return [p.question for p in pv.practice]


def test_published_faq_is_attached_to_its_point(conn):
    vid = add_doc(conn)
    add_faq(conn, vid, "Можно ли закупить у одного поставщика?", "Да, с согласия службы качества.")
    add_faq(conn, vid, "Про другой пункт", "Ответ про 2.2.", numbers=("2.2",))
    pv = view(conn)
    assert questions(pv) == ["Можно ли закупить у одного поставщика?"]
    entry = pv.practice[0]
    assert entry.answer == "Да, с согласия службы качества." and entry.confirmed_at == "2026-11-25"
    assert questions(view(conn, "2.2")) == ["Про другой пункт"]


@pytest.mark.parametrize("status", ["deleted", "review"])
def test_unpublished_faq_is_hidden(conn, status):
    vid = add_doc(conn)
    add_faq(conn, vid, "Скрытый вопрос", "Скрытый ответ.", status=status)
    assert view(conn).practice == []
    assert "Практика применения" not in R.practice_html(R.point_panel(view(conn)))


def test_withdrawn_through_service_disappears(conn):
    vid = add_doc(conn)
    fid = add_faq(conn, vid, "Вопрос", "Ответ.")
    assert questions(view(conn)) == ["Вопрос"]
    set_faq_status(conn, fid, "deleted")
    assert view(conn).practice == []


def test_faq_without_point_basis_is_not_practice(conn):
    vid = add_doc(conn)
    add_faq(conn, vid, "Про весь документ", "Ответ.", kind="document", numbers=(None,))
    add_faq(conn, vid, "Вне регламентов", "Ответ.", kind="outside", numbers=())
    assert view(conn).practice == []


def test_other_document_with_same_point_number_is_not_mixed(conn):
    vid = add_doc(conn)
    add_doc(conn, "kadry", "Кадровые правила", "ОК")
    add_faq(conn, vid, "Только закупки", "Ответ.")
    assert view(conn, doc_id="kadry").practice == []


def test_panel_block_has_question_answer_label_and_date(conn):
    vid = add_doc(conn)
    add_faq(conn, vid, "Нужен ли второй подписант?", "Нужен для сумм свыше лимита.")
    panel = R.point_panel(view(conn))
    (item,) = panel["practice"]["items"]
    assert item["question"] == "Нужен ли второй подписант?" and item["date"] == "25.11.2026"
    html = R.practice_html(panel)
    for text in ("Практика применения", "Нужен ли второй подписант?", "Нужен для сумм свыше лимита.",
                 "подтверждено службой качества", "25.11.2026"):
        assert text in html


def test_block_is_limited_with_count_of_the_rest(conn):
    vid = add_doc(conn)
    for i in range(5):
        add_faq(conn, vid, f"Вопрос {i}", f"Ответ {i}.")
    panel = R.point_panel(view(conn))
    assert len(panel["practice"]["items"]) == 3 and panel["practice"]["more"] == 2
    assert "ещё 2" in R.practice_html(panel)


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bad = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "img", "svg", "iframe"} or any(k.startswith("on") for k, _ in attrs):
            self.bad.append(tag)


def test_practice_html_is_escaped(conn):
    vid = add_doc(conn)
    add_faq(conn, vid, XSS, XSS)
    html = R.practice_html(R.point_panel(view(conn)))
    parser = Tags()
    parser.feed(html)
    assert not parser.bad and "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html


def test_point_without_practice_has_no_block(conn):
    add_doc(conn)
    panel = R.point_panel(view(conn))
    assert panel["practice"]["items"] == [] and R.practice_html(panel) == ""
