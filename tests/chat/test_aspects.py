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


def test_quote_fragments_joined_by_ellipsis():
    assert A.quote_is_grounded("Первое. … Второе.", ["Первое.", "а Второе."])
    assert not A.quote_is_grounded("Первое. … Третье.", ["Первое.", "а Второе."])
    assert not A.quote_is_grounded("…", ["x"])


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
    assert ids(A.select_advice(cat, answer("7.1"), "Нужно ли NDA?")) == ["secret", "general-1"]
    assert ids(A.select_advice(cat, answer("7.1"), "обычный вопрос")) == ["general-1", "general-2"]  # без when-аспект скрыт


@pytest.mark.parametrize("term,question,hit", [
    ("карточка", "Что в карточке и оплатить картой?", False),
    ("карточка", "Заполнить карточка проекта", True),
    ("чек ", "Нужен ли чек-лист проекта?", False),
    ("чек ", "Нужен ли чек?", True),
    ("чек ", "Выдать чек, пожалуйста", True),
    ("акт", "Как подписать контракт?", False),
    ("акт", "Когда подписывается акт сдачи", True),
    ("по карте", "можно оплатить по карте?", True),
    ("гос", "Как подписать договор с госзаказчиком?", True),
    ("44-фз", "Подходит ли 44-ФЗ?", True),
    ("кп ", "Подготовить КП", True),
    ("кп ", "кпд проекта", False),
])
def test_when_matches_from_word_start_only(term, question, hit):
    assert A.when_hits(term, question) is hit


def test_when_in_selection_uses_word_boundary(cat):
    spaced = A.parse_catalog(CAT.replace('when = ["nda", "тайн"]', 'when = ["чек "]'), LAWS)
    assert "secret" not in ids(A.select_advice(spaced, answer("7.1"), "Нужен чек-лист?"))
    assert "secret" in ids(A.select_advice(spaced, answer("7.1"), "Нужен чек?"))


def test_limit_of_two_and_no_duplicates(cat):
    extra = "".join(
        f'\n  [[rubric.aspect]]\n  id = "g{i}"\n  text = "t"\n  laws = ["zk-1"]\n  quote = "в письменной форме"\n'
        for i in range(5)
    )
    big = A.parse_catalog(CAT.replace('[[rubric]]\nid = "r2"', extra + '\n[[rubric]]\nid = "r2"'), LAWS)
    got = A.select_advice(big, answer("7.1"), "тайна")
    assert len(got) == 2 and got[0].aspect.id == "secret"
    two = A.parse_catalog(CAT.replace('"doc-b:2"', '"doc-a:7"'), LAWS)  # раздел дважды в одной рубрике
    got2 = ids(A.select_advice(two, answer("7.1"), "тайна"))
    assert len(set(got2)) == len(got2) == 2


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
DISCLAIMER = "Справочно, не юридическая консультация — применение к вашему случаю уточните у юристов."


def test_block_props_have_disclaimer_references_and_plain_text(cat):
    adv = A.select_advice(cat, answer("7.1"), "nda")
    props = R.advice_element(adv)
    assert props["title"] == "На что обратить внимание" and props["note"] == DISCLAIMER
    assert "Не из регламентов" not in props["note"]
    assert [i["laws"] for i in props["items"]][0] == [
        {"label": "Закон о тайнах, ст. 9", "law_id": "zk-2"}, {"label": "ЗВД, ст. 5", "law_id": "zk-1"}]
    assert props["items"][0]["text"] == "Тайна <b>важна</b>."  # элемент выводит текст как текст, не как разметку
    assert "На что обратить внимание" not in R.answer_markdown(answer("7.1"))  # блок не в markdown (HTML выключен)


def test_chat_uses_short_with_fallback_to_text():
    law = A.Law("x", "Акт", "1", "432", "T", "t", "r", "https://e.test", "2026-01-01", short="ГК РФ")
    full = A.Aspect("a", "r", "Полный совет.", (), ("x",), "t", short="Коротко.")
    plain = A.Aspect("b", "r", "Полный совет.", (), ("x",), "t")
    props = R.advice_element([A.Advice(full, (law,)), A.Advice(plain, (law,))])
    assert [i["text"] for i in props["items"]] == ["Коротко.", "Полный совет."]
    assert props["items"][0]["laws"][0]["label"] == "ГК РФ, ст. 432"
    c = A.parse_catalog(CAT.replace('  id = "general-1"', '  id = "general-1"\n  short = "Кратко один."'), LAWS)
    assert c.rubrics[0].aspects[0].short == "Кратко один."


def test_no_element_without_advice():
    assert R.advice_element([]) is None


def test_law_panel(cat):
    panel = R.law_panel(cat.laws["zk-1"])
    assert panel["act"].startswith("Закон о вымышленных") and panel["title"] == "Форма договора"
    assert "в письменной форме" in panel["text"]
    assert panel["revision"] == "редакция от 01.02.2026"
    assert panel["note"] == ("Текст статьи приведён справочно, редакция от 01.02.2026. "
                             "Применение к вашему случаю — с юридической службой.")
    assert panel["source_url"] == "https://example.test/zk-1" and panel["retrieved"] == "получено 20.11.2026"
    assert R.law_panel(cat.laws["zk-2"])["source_url"] == ""  # небезопасная схема не становится ссылкой


# ---------- согласование ----------
def test_approved_filter(tmp_path):
    approved = CAT.replace('  id = "general-2"', '  id = "general-2"\n  approved = "юридическая служба, 2026-11-30"')
    d = write(tmp_path, approved)
    demo = A.load_catalog(d, env={})
    strict = A.load_catalog(d, env={"ASPECTS_REQUIRE_APPROVED": "1"})
    assert ids(A.select_advice(demo, answer("7.1"), "в")) == ["general-1", "general-2"]
    assert ids(A.select_advice(strict, answer("7.1"), "в")) == ["general-2"]
    assert A.select_advice(strict, answer("2.1", doc="doc-c", ), "в") == []  # другая рубрика не согласована
    assert strict.rubrics[0].aspects[1].approved.startswith("юридическая служба")


def test_real_revision_reads_once_in_footer():
    c = A.load_catalog(ROOT / "docx" / "aspects", env={})
    note = R.law_panel(c.laws["gk-432"])["note"]
    assert "редакция от ред." not in note and "от от" not in note and note.startswith("Текст статьи приведён справочно, редакция от ")


def test_real_data_has_no_approval_yet():
    c = A.load_catalog(ROOT / "docx" / "aspects", env={})
    assert c and not any(a.approved for r in c.rubrics for a in r.aspects)


# ---------- перенаправление ----------
def redirected(kind, outcome=Outcome.OFF_TOPIC, ticket=None, summary="Общий текст."):
    a = Answer(outcome, summary, [Statement("x", [ref("7.1")])], ticket=ticket)
    a.redirect = kind
    return a


def test_legal_redirect_is_alone(cat):
    from reglament_agent.answer import TicketInfo
    a = redirected("legal", Outcome.NO_BASIS, TicketInfo("2026-1", "r", "sent", []),
                   "Нет.\nп. 2.2 «Док» (ОЗ): Текст.")
    md = R.answer_markdown(a)
    assert md == "Это вопрос законодательства, а не регламентов компании — по нему лучше обратиться к юристам."
    assert A.select_advice(cat, a, "в") == []
    assert R.refusal_element(a, lambda n: None) is None


@pytest.mark.parametrize("kind", ["legal", "manager"])
def test_redirect_suppresses_advice_even_with_cited_point(cat, kind):
    assert A.select_advice(cat, redirected(kind, Outcome.ANSWER), "в") == []


def test_manager_redirect_keeps_off_topic_hint():
    md = R.answer_markdown(redirected("manager"))
    assert "обратитесь к своему руководителю" in md and "юристам" not in md


def test_no_redirect_attribute_is_ignored(cat):
    assert not hasattr(answer("7.1"), "redirect")
    assert A.select_advice(cat, answer("7.1"), "в")


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
            fragments = [norm(f).strip(" .") for f in a["quote"].split("…") if f.strip(" .")]
            assert fragments, f"{a['id']}: пустая цитата"
            texts = [norm(laws[i]["text"]) for i in a["laws"]]
            for f in fragments:
                assert any(f in t for t in texts), f"{a['id']}: фрагмента нет в статье: {f[:60]}"
    assert A.load_catalog(directory, env={}) is not None
    assert len(raw["rubric"]) == 10 and sum(len(r["aspect"]) for r in raw["rubric"]) == 21
    assert all(len(a["short"]) <= 170 and a["short"].strip() for r in raw["rubric"] for a in r["aspect"])
