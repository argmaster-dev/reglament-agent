"""admin-p01: сводка изменений вверху страницы сравнения (добавлено, удалено, изменено, перенумеровано)."""
import re
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.admin import jobs, routes
from reglament_agent.chat import session as S
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.storage.publish import publish

ROOT = Path(__file__).resolve().parents[2]
DBRCT = ROOT / "docx" / "Проектная деятельность ДБРЦТ.docx"
DEMO = ROOT / "docx" / "demo" / "ДБРЦТ v2 (демо-правка п. 1.5).docx"
SECRET = "test-secret"
CARD = DocumentCard("doc-s", "Вымышленный регламент", "Отдел", ("project",), "s.docx")


@pytest.fixture
def env(isolated, monkeypatch):
    conn, dsn = isolated
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    monkeypatch.setenv("EMBEDDER", "fake")
    monkeypatch.setenv("ADMIN_BACKGROUND", "0")
    return conn


def expert():
    app = FastAPI()
    routes.install(app)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(S.COOKIE_NAME, S.sign_identity(S.Identity("expert@x.local", "expert"), SECRET))
    return c


def docx(*paragraphs):
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    xml += "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs)
    out = BytesIO()
    with ZipFile(out, "w") as z:
        z.writestr("word/document.xml", xml + "</w:body></w:document>")
    return out.getvalue()


def summary(page):
    """Сводка: счётчики и строки со списком пунктов (текст блока без тегов)."""
    block = re.search(r'id="summary".*?</ul></div>', page, re.S).group(0)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", block))


V1 = ("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет три дня.",
      "1.2. Согласование выполняет руководитель отдела.", "1.3. Архив хранится пять лет.",
      "1.4. Отчёт сдаётся ежеквартально.")
V2 = ("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет пять дней.",  # изменён
      "1.2. Согласование выполняет руководитель отдела.",                            # без изменений
      "1.5. Отчёт сдаётся ежеквартально.",                                           # перенумерован 1.4 -> 1.5
      "1.6. Новый пункт о резервном копировании.")                                   # добавлен; 1.3 удалён


def test_summary_on_comparison_page_for_known_differences(env):
    for version in (V1, V2):
        publish(env, CARD, docx(*version), FakeEmbedder(), "tester")
    page = expert().get("/admin/documents/doc-s/versions/diff?old=1&new=2").text
    text = summary(page)
    assert "Сводка изменений" in text
    assert "Добавлено: 1 · удалено: 1 · изменено: 1 · перенумеровано: 1" in text
    assert "Добавлены: п. 1.6" in text and "Удалены: п. 1.3" in text
    assert "Изменены: п. 1.1" in text and "Перенумерованы: п. 1.4 → п. 1.5" in text
    for anchor in ("add-1.6", "del-1.3", "chg-1.1", "ren-1.4"):  # якоря ведут на детали
        assert f'href="#{anchor}"' in page and f'id="{anchor}"' in page
    assert "<del>три</del>" in page and "<ins>пять</ins>" in page


def test_each_counter_counts_its_own_kind(env):
    """Разные числа по видам (1, 2, 3, 4): ошибка в одном счётчике не прячется за совпадением."""
    old = ["Регламент", "1. Раздел"] + [f"1.{i}. {t}." for i, t in enumerate(
        ["Первый срок три дня", "Второй срок три дня", "Третий срок три дня", "Альфа правило",
         "Бета правило", "Гамма правило", "Дельта правило", "Удаляемый пункт"], 1)]
    new = ["Регламент", "1. Раздел", "1.1. Первый срок пять дней.", "1.2. Второй срок пять дней.",
           "1.3. Третий срок пять дней.", "1.14. Альфа правило.", "1.15. Бета правило.", "1.16. Гамма правило.",
           "1.17. Дельта правило.", "1.18. Новый пункт один.", "1.19. Новый пункт два."]
    for version in (old, new):
        publish(env, CARD, docx(*version), FakeEmbedder(), "tester")
    text = summary(expert().get("/admin/documents/doc-s/versions/diff?old=1&new=2").text)
    assert "Добавлено: 2 · удалено: 1 · изменено: 3 · перенумеровано: 4" in text
    assert "Добавлены: п. 1.18, п. 1.19" in text and "Удалены: п. 1.8" in text
    assert "Изменены: п. 1.1, п. 1.2, п. 1.3" in text
    assert "Перенумерованы: п. 1.4 → п. 1.14, п. 1.5 → п. 1.15, п. 1.6 → п. 1.16, п. 1.7 → п. 1.17" in text


def test_dbrct_v1_to_demo_v2_summary_has_only_point_1_5(env):
    c = expert()
    data = {"target": "new", "department_select": "__new__", "department_new": "ДБРЦТ", "stages": ["presale", "project"]}

    def upload(path, **extra):
        return c.post("/admin/documents/upload", data={**data, **extra},
                      files={"file": (path.name, path.read_bytes(), "application/octet-stream")})

    def job_of(r):
        return int(re.search(r"/admin/jobs/(\d+)/publish", r.text).group(1))

    first = job_of(upload(DBRCT))
    c.post(f"/admin/jobs/{first}/publish", data={})
    jobs.run_job(first)
    doc_id = env.execute("SELECT doc_id FROM documents").fetchone()[0]
    second = job_of(upload(DEMO, target=doc_id))
    preview = c.get(f"/admin/jobs/{second}/changes").text  # предпросмотр замены: тот же расчёт
    assert "Добавлено: 0 · удалено: 0 · изменено: 1 · перенумеровано: 0" in summary(preview)
    c.post(f"/admin/jobs/{second}/publish", data={})
    jobs.run_job(second)

    page = c.get(f"/admin/documents/{doc_id}/versions/diff?old=1&new=2").text
    text = summary(page)
    assert "Добавлено: 0 · удалено: 0 · изменено: 1 · перенумеровано: 0" in text
    assert "Изменены: п. 1.5" in text and "Добавлены" not in text and "Удалены" not in text
    assert "<del>3</del>" in page and "<ins>5</ins>" in page
    detail = page[page.index('id="chg-1.5"'):]
    old_col, new_col = re.split(r"Стало", detail, maxsplit=1)
    plain = lambda h: re.sub(r"<[^>]+>", "", h)  # noqa: E731
    assert "3 рабочих дня" in plain(old_col) and "5 рабочих дней" in plain(new_col)
    if out := __import__("os").environ.get("DIFF_HTML_OUT"):  # сохранить HTML как доказательство
        Path(out).write_text(page, encoding="utf-8")
