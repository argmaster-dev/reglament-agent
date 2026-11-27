"""Workplace jargon dictionary docx/slang.tsv and its merge into the lexicon."""
from pathlib import Path

import pytest

from reglament_agent.dialog import lexicon
from reglament_agent.dialog.lexicon import expand_query, fold, word_key, words
from reglament_agent.ingest.parser import load_cards, parse_docx

DOCX = Path(__file__).resolve().parents[2] / "docx"
SLANG = DOCX / "slang.tsv"
GLOSSARY = DOCX / "glossary.tsv"


def _rows(path):
    lines = path.read_text(encoding="utf-8").splitlines()[1:]
    return [(tuple(v.strip() for v in a.split(",") if v.strip()), b.strip())
            for a, b in (line.split("\t", 1) for line in lines if line.strip())]


@pytest.fixture(autouse=True)
def default_paths(monkeypatch):
    monkeypatch.delenv("SLANG_PATH", raising=False)
    monkeypatch.delenv("GLOSSARY_PATH", raising=False)
    lexicon.set_glossary_path(GLOSSARY)
    lexicon.set_slang_path(None)
    yield
    lexicon.set_glossary_path(None)
    lexicon.set_slang_path(None)


def test_every_official_term_occurs_in_the_regulations():
    keyed = []
    for card in load_cards(DOCX):
        for point in parse_docx((DOCX / card.file).read_bytes()).points:
            keyed.append([word_key(w) for w in words(point.text)])
    missing = []
    for _, term in _rows(SLANG):
        seq = [word_key(w) for w in words(term)]
        n = len(seq)
        if not any(k[i:i + n] == seq for k in keyed for i in range(len(k) - n + 1)):
            missing.append(term)
    assert not missing


def test_slang_is_well_formed_and_does_not_overlap_the_glossary():
    rows = _rows(SLANG)
    assert 20 <= len(rows) <= 60
    taken = {fold(v) for variants, _ in _rows(GLOSSARY) for v in variants}
    seen = set()
    for variants, term in rows:
        assert variants and term
        for v in variants:
            assert fold(v) not in taken, v
            assert fold(v) not in seen, v
            seen.add(fold(v))


def test_loader_merges_both_files_and_glossary_wins(tmp_path):
    gl = tmp_path / "glossary.tsv"
    gl.write_text("abbreviation\texpansion\nТКП\tТехнико-коммерческое предложение\n", encoding="utf-8")
    sl = tmp_path / "slang.tsv"
    sl.write_text("slang\tofficial term\nкэпэ, ткп\tЦена из жаргона\nжелезо, железки\tоборудование\n"
                  "ткп\tещё один\n", encoding="utf-8")
    lexicon.set_glossary_path(gl)
    assert lexicon.slang_path() == sl  # next to the glossary by default
    merged = lexicon.glossary()
    assert merged["ТКП"] == "Технико-коммерческое предложение"
    assert merged["кэпэ"] == "Цена из жаргона" and merged["железки"] == "оборудование"
    assert "ткп" not in merged  # the glossary variant is not redefined by the jargon file
    assert len(lexicon._load()) == 3


def test_slang_file_is_optional(tmp_path):
    gl = tmp_path / "glossary.tsv"
    gl.write_text("abbreviation\texpansion\nТЗ\tТехническое задание\n", encoding="utf-8")
    lexicon.set_glossary_path(gl)
    assert not lexicon.slang_path().exists()
    assert lexicon.glossary() == {"ТЗ": "Техническое задание"}


def test_slang_path_override_by_env_and_setter(tmp_path, monkeypatch):
    other = tmp_path / "other.tsv"
    other.write_text("slang\tofficial term\nфуфел\tоборудование\n", encoding="utf-8")
    monkeypatch.setenv("SLANG_PATH", str(other))
    lexicon.set_slang_path(None)  # drops the caches
    assert lexicon.slang_path() == other and "фуфел" in lexicon.glossary()
    lexicon.set_slang_path(tmp_path / "missing.tsv")
    assert "фуфел" not in lexicon.glossary() and "ТКП" in lexicon.glossary()


@pytest.mark.parametrize(("query", "added"), [
    ("кто закрывает дебиторку по проекту?", "дебиторская задолженность"),
    ("кто готовит ндашки?", "NDA"),
    ("когда подписывать допник?", "ДС"),
    ("кто подписывает допники и допсоглашения", "ДС"),
    ("субчик не прошёл проверку", "субподрядчик"),
    ("договор с субчиками и субами", "субподрядчик"),
    ("кто проводит кикофф?", "установочное совещание"),
    ("назначили кик-офф на пятницу", "установочное совещание"),
    ("кто собирает закрывашки?", "закрывающие документы"),
    ("кто работает с эсбэшниками", "Департамент экономической безопасности"),
])
def test_expansion_of_slang_in_word_forms(query, added):
    assert added.lower() in expand_query(query).lower()


@pytest.mark.parametrize("phrase", [
    "Он покраснел от смущения, когда его похвалили.",
    "Красная зона на карте метро что значит?",
    "Фильм в стиле ретро идёт в кинотеатре.",
    "Детская площадка во дворе закрыта, выезд на площадку закрыт",
    "Сколько сдачи дали в магазине, сдачка мелочью?",
    "ПНР и другие страны Восточной Европы",
    "Письмо от госзаказчика пришло вчера, кто его разберёт?",
    "Скачал zip и зипы с архивами, дистр Linux поставил",
])
def test_everyday_homonyms_do_not_expand(phrase):
    lexicon.set_slang_path(DOCX / "missing.tsv")
    plain = expand_query(phrase)
    lexicon.set_slang_path(None)
    assert expand_query(phrase) == plain


@pytest.mark.parametrize("query", [
    "кто подписывает договор с субподрядчиком?",
    "как оформить дополнительное соглашение?",
    "кто проводит установочное совещание?",
    "когда проводится ретроспектива?",
    "что входит в закрывающие документы?",
    "кто работает с Департаментом экономической безопасности",
])
def test_official_terms_are_never_expanded_into_slang(query):
    with_slang = expand_query(query)
    lexicon.set_slang_path(DOCX / "missing.tsv")
    assert with_slang == expand_query(query)  # only the glossary may add words


def test_glossary_reverse_expansion_is_unchanged():
    out = expand_query("где взять технико-коммерческое предложение")
    assert "ТКП" in out and "КП" in out
    assert "ДС" in expand_query("подписать дополнительное соглашение")
    # the same direction is disabled for a jargon row whose official term is in the question
    assert "субчик" not in expand_query("договор с субподрядчиком")
