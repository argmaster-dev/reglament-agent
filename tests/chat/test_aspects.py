"""К10: блок «На что обратить внимание». Каталог и законы в тестах вымышленные; БД и модель не нужны."""

import logging
from pathlib import Path

import pytest

from reglament_agent.answer import Answer, Outcome, SourceRef, Statement, Variant
from reglament_agent.chat import aspects as A
from reglament_agent.chat import render as R
from reglament_agent.ingest.parser import load_cards, parse_docx

ROOT = Path(__file__).resolve().parents[2]
LAWS = """
[[law]]
id = "zk-1"
act = "Закон о вымышленных договорах"
short = "ЗВД"
number = "1-ФЗ"
article = "5"
title = "Форма договора"
text = "Договор заключается   в письменной форме.\\nСтороны вправе изменить условия."
revision = "от 01.02.2026"
source_url = "https://example.test/zk-1"
retrieved = "2026-11-20"

[[law]]
id = "zk-2"
act = "Закон о тайнах"
article = "статья 9"
title = "Режим"
text = "Режим тайны устанавливается обладателем информации."
revision = "от 03.04.2025"
source_url = "javascript:alert(1)"
retrieved = "2026-11-21"
"""
CAT = """
[[rubric]]
id = "r1"
title = "Договор"
sections = ["doc-a:7", "doc-b:2"]

  [[rubric.aspect]]
  id = "general-1"
  text = "Общий совет один."
  laws = ["zk-1"]
  quote = "в письменной форме"

  [[rubric.aspect]]
  id = "general-2"
  text = "Общий совет два."
  laws = ["zk-1"]
  quote = "Стороны вправе изменить условия"

  [[rubric.aspect]]
  id = "secret"
  when = ["nda", "тайн"]
  text = "Тайна <b>важна</b>."
  laws = ["zk-2", "zk-1"]
  quote = "Режим тайны устанавливается"

[[rubric]]
id = "r2"
title = "Другая"
sections = ["doc-c:1"]

  [[rubric.aspect]]
  id = "other"
  text = "Совет другой рубрики."
  laws = ["zk-1"]
  quote = "в письменной форме"
"""


def ref(point, doc="doc-a", faq=None):
    return SourceRef(doc, "Док", "ОЗ", 1, point, faq_id=faq)


def answer(*points, outcome=Outcome.ANSWER, doc="doc-a"):
    return Answer(outcome, "Итог.", [Statement("Утверждение.", [ref(p, doc) for p in points])])


@pytest.fixture
def cat():
    return A.parse_catalog(CAT, LAWS)


def ids(advice):
    return [a.aspect.id for a in advice]


# ---------- загрузчик ----------
def write(tmp_path, aspects=CAT, laws=LAWS):
    (tmp_path / "aspects.toml").write_text(aspects, encoding="utf-8")
    (tmp_path / "laws.toml").write_text(laws, encoding="utf-8")
    return tmp_path


def test_valid_files_load(tmp_path):
    c = A.load_catalog(write(tmp_path), env={})
    assert c and [r.id for r in c.rubrics] == ["r1", "r2"] and set(c.laws) == {"zk-1", "zk-2"}


def test_unknown_law_id_switches_feature_off_and_logs(tmp_path, caplog):
    bad = CAT.replace('laws = ["zk-2", "zk-1"]', 'laws = ["zk-2", "nope"]')
    with caplog.at_level(logging.ERROR):
        assert A.load_catalog(write(tmp_path, bad), env={}) is None
    assert "nope" in caplog.text


@pytest.mark.parametrize("files", [(), ("aspects.toml",), ("laws.toml",)])
def test_missing_files_silently_off(tmp_path, caplog, files):
    for f in files:
        (tmp_path / f).write_text("", encoding="utf-8")
    assert A.load_catalog(tmp_path, env={}) is None
    assert not caplog.records


def test_switch_and_directory_setting(tmp_path):
    write(tmp_path)
    assert A.load_catalog(env={"ASPECTS_DIR": str(tmp_path)})
    assert A.load_catalog(env={"ASPECTS_DIR": str(tmp_path), "ASPECTS_ENABLED": "0"}) is None


@pytest.mark.parametrize("old,new", [
    ('"doc-a:7"', '"doc-a-7"'),
    ('"doc-a:7"', '"doc-a:7.3"'),
    ('quote = "в письменной форме"\n\n  [[rubric.aspect]]\n  id = "general-2"',
     'quote = "такого нет в статье"\n\n  [[rubric.aspect]]\n  id = "general-2"'),
    ('quote = "Режим тайны устанавливается"', 'quote = ""'),
    ('id = "general-2"', 'id = "general-1"'),
])
def test_invalid_data_is_rejected(tmp_path, old, new):
    assert A.load_catalog(write(tmp_path, CAT.replace(old, new, 1)), env={}) is None


def test_broken_toml_does_not_crash(tmp_path):
    assert A.load_catalog(write(tmp_path, "[[rubric"), env={}) is None


def test_quote_is_checked_with_whitespace_normalised(cat):
    assert cat.rubrics[0].aspects[0].quote == "в письменной форме"  # в тексте закона «в   письменной»


# ---------- выбор ----------
def test_matches_by_section_and_ignores_other_documents(cat):
    assert ids(A.select_advice(cat, answer("7.1"), "вопрос")) == ["general-1", "general-2"]
    assert ids(A.select_advice(cat, answer("2.4", doc="doc-b"), "вопрос")) == ["general-1", "general-2"]
    assert A.select_advice(cat, answer("7.1", doc="doc-zzz"), "вопрос") == []
    assert ids(A.select_advice(cat, answer("1.1", doc="doc-c"), "вопрос")) == ["other"]


def test_sub_points_map_to_top_level_section(cat):
    assert A.section_key("doc-a", "7.3.1.2") == "doc-a:7" == A.section_key("doc-a", "7")
    assert A.select_advice(cat, answer("7.3.1"), "в")
    assert A.select_advice(cat, answer("17.3"), "в") == []  # «17» не раздел «7»


def test_variants_statements_count_too(cat):
    a = Answer(Outcome.VARIANTS, "", variants=[Variant("ОЗ", [Statement("x", [ref("7.1")])])])
    assert A.select_advice(cat, a, "в")


def test_when_hit_goes_first_and_is_case_insensitive(cat):
    assert ids(A.select_advice(cat, answer("7.1"), "Нужно ли NDA?")) == ["secret", "general-1", "general-2"]
    assert ids(A.select_advice(cat, answer("7.1"), "обычный вопрос")) == ["general-1", "general-2"]  # без when-аспект скрыт


def test_limit_of_three_and_no_duplicates(cat):
    extra = "".join(
        f'\n  [[rubric.aspect]]\n  id = "g{i}"\n  text = "t"\n  laws = ["zk-1"]\n  quote = "в письменной форме"\n'
        for i in range(5)
    )
    big = A.parse_catalog(CAT.replace('[[rubric]]\nid = "r2"', extra + '\n[[rubric]]\nid = "r2"'), LAWS)
    got = A.select_advice(big, answer("7.1"), "тайна")
    assert len(got) == 3 and got[0].aspect.id == "secret"
    two = A.parse_catalog(CAT.replace('"doc-b:2"', '"doc-a:7"'), LAWS)  # раздел дважды в одной рубрике
    assert len(set(ids(A.select_advice(two, answer("7.1"), "тайна")))) == 3


@pytest.mark.parametrize("outcome", [Outcome.NO_BASIS, Outcome.OFF_TOPIC, Outcome.CLARIFY, Outcome.EXTERNAL_DOC,
                                      Outcome.CONFLICT, Outcome.ERROR, Outcome.EXPERT_ANSWER])
def test_no_block_without_regulation_answer(cat, outcome):
    assert A.select_advice(cat, answer("7.1", outcome=outcome), "тайна") == []


@pytest.mark.parametrize("outcome", [Outcome.ANSWER, Outcome.VARIANTS, Outcome.LOW_CONFIDENCE])
def test_block_for_regulation_outcomes(cat, outcome):
    assert A.select_advice(cat, answer("7.1", outcome=outcome), "в")


def test_no_block_when_no_point_is_cited(cat):
    assert A.select_advice(cat, Answer(Outcome.ANSWER, "Итог."), "тайна") == []
    faq = Answer(Outcome.ANSWER, "Итог.", [Statement("x", [ref("7.1", faq=5)])])  # только FAQ
    assert A.select_advice(cat, faq, "тайна") == []
    assert A.select_advice(None, answer("7.1"), "в") == []


# ---------- отрисовка ----------
def test_block_renders_label_references_and_escaping(cat):
    adv = A.select_advice(cat, answer("7.1"), "nda")
    md = R.answer_markdown(answer("7.1"), advice=adv)
    assert "**На что обратить внимание**" in md
    assert "_Не из регламентов — общий совет. Уточните у юриста или в службе качества._" in md
    assert "(Закон о тайнах, ст. 9; ЗВД, ст. 5)" in md  # «статья 9» без дубля, короткое имя при наличии
    assert "&lt;b&gt;важна&lt;/b&gt;" in md and "<b>" not in md
    assert md.index("Утверждение") < md.index("На что обратить внимание")


def test_escaping_of_law_names():
    law = A.Law("x", "Акт <i>", "1", "1", "T", "t", "r", "https://e.test", "2026-01-01")
    adv = [A.Advice(A.Aspect("a", "r", "<script>alert(1)</script>", (), ("x",), "t"), (law,))]
    md = R.advice_md(adv)
    assert "<script>" not in md and "<i>" not in md and "&lt;script&gt;" in md


def test_no_block_markup_without_advice():
    assert "На что обратить внимание" not in R.answer_markdown(answer("7.1"))


def test_law_action_and_panel(cat):
    adv = A.select_advice(cat, answer("7.1"), "nda")
    acts = R.law_actions(adv)
    assert [(a.name, a.payload["law_id"]) for a in acts] == [("open_law", "zk-2"), ("open_law", "zk-1")]
    assert all("ст." in a.label for a in acts)
    panel = R.law_panel(cat.laws["zk-1"])
    assert panel["act"].startswith("Закон о вымышленных") and panel["title"] == "Форма договора"
    assert "в письменной форме" in panel["text"]
    assert panel["revision"] == "редакция от 01.02.2026"
    assert panel["source_url"] == "https://example.test/zk-1" and panel["retrieved"] == "получено 20.11.2026"
    assert R.law_panel(cat.laws["zk-2"])["source_url"] == ""  # небезопасная схема не становится ссылкой


# ---------- данные репозитория ----------
def test_real_catalogue_is_valid_and_grounded():
    """Каждый раздел существует в корпусе, у каждого совета есть цитата, дословно взятая из текста статьи."""
    directory = ROOT / "docx" / "aspects"
    if not (directory / "aspects.toml").is_file():
        pytest.skip("каталог ещё не добавлен")
    import tomllib

    raw = tomllib.loads((directory / "aspects.toml").read_text(encoding="utf-8"))
    laws = {l["id"]: l for l in tomllib.loads((directory / "laws.toml").read_text(encoding="utf-8")).get("law", [])}
    corpus = ROOT / "docx"
    sections = {}
    for card in load_cards(corpus):
        doc = parse_docx((corpus / card.file).read_bytes())
        sections[card.doc_id] = {p.section_number for p in doc.points}
    norm = lambda t: " ".join(t.split())
    assert raw.get("rubric")
    for r in raw["rubric"]:
        for s in r["sections"]:
            doc_id, _, number = s.partition(":")
            assert number in sections.get(doc_id, ()), f"{r['id']}: нет раздела {s}"
        for a in r.get("aspect", []):
            assert a.get("quote", "").strip(), f"{a['id']}: пустая цитата"
            assert a["laws"] and all(i in laws for i in a["laws"]), f"{a['id']}: закон не найден"
            assert any(norm(a["quote"]) in norm(laws[i]["text"]) for i in a["laws"]), f"{a['id']}: цитаты нет в статье"
    assert A.load_catalog(directory, env={}) is not None
