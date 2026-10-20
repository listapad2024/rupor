"""Тесты textproc: подсчёт слов, словарь, сниппеты, подсказка для распознавания."""
from __future__ import annotations

from typing import Any

import pytest

from rupor.textproc import (apply_dictionary, asr_prompt, count_words, expand_snippets, fold,
                            misrecognition_filter, normalize_phrase)

DICT: list[dict[str, Any]] = [
    {"id": 1, "term": "ClickHouse", "variants": ["клик хаус"]},
    {"id": 2, "term": "Metabase", "variants": ["метабейс", "мета бейс"]},
    {"id": 3, "term": "Grafana", "variants": ["графана"]},
    {"id": 4, "term": "Сбербанк", "variants": ["сбер банк"]},
    {"id": 5, "term": "API", "variants": ["апи", "эй пи ай"]},
    {"id": 6, "term": "API Gateway", "variants": ["апи гейтвей"]},
]


# ---------- fold / normalize_phrase ----------

def test_fold_cyrillic_and_yo() -> None:
    assert fold("ЁЖИК В Тумане") == "ежик в тумане"
    assert fold("ClickHouse") == "clickhouse"
    assert fold("") == ""


def test_normalize_phrase() -> None:
    assert normalize_phrase("  Мой   имейл! ") == "мой имейл"
    assert normalize_phrase("«Подпись».") == "подпись"
    assert normalize_phrase("— Всё, хватит…") == "все, хватит"
    assert normalize_phrase("...") == ""
    assert normalize_phrase("") == ""


# ---------- count_words ----------

@pytest.mark.parametrize("text,n", [
    ("", 0),
    ("   \n\t ", 0),
    ("Привет, мир!", 2),
    ("Ёжик в тумане", 3),
    ("ClickHouse-запрос", 1),
    ("во-первых, во-вторых", 2),
    ("Встреча в 10:30, бюджет 3,5 млн и 2.5%", 8),
    ("user_id и snake_case_name", 3),
    ("don't stop", 2),
    ("🙂 привет 👍", 1),
    ("— ... !!!", 0),
    ("Python3 и API v2", 4),
    ("中文 текст", 2),
    ("строка один\nстрока два", 4),
])
def test_count_words(text: str, n: int) -> None:
    assert count_words(text) == n


def test_count_words_nfd() -> None:
    # «й» из двух кодовых точек (и + бреве) — одно слово, а не два
    assert count_words("мой край".replace("й", "и\u0306")) == 2


# ---------- apply_dictionary ----------

@pytest.mark.parametrize("src,expected", [
    ("клик хаус", "ClickHouse"),
    ("Клик-Хаус", "ClickHouse"),
    ("кликхаус", "ClickHouse"),
    ("КЛИК   ХАУС", "ClickHouse"),
    ("клик – хаус", "ClickHouse"),
    ("clickhouse", "ClickHouse"),            # сам термин нормализуется к своему регистру
    ("click house", "ClickHouse"),           # camelCase термина
    ("Click-House", "ClickHouse"),
    ("ClickHouse", "ClickHouse"),
])
def test_dictionary_variant_forms(src: str, expected: str) -> None:
    assert apply_dictionary(src, DICT) == expected


def test_dictionary_keeps_punctuation_and_context() -> None:
    assert apply_dictionary("(клик хаус), метабейс!", DICT) == "(ClickHouse), Metabase!"
    assert apply_dictionary("«кликхаус» — это СУБД.", DICT) == "«ClickHouse» — это СУБД."
    assert apply_dictionary("Данные: клик хаус; графики: мета бейс?", DICT) == \
        "Данные: ClickHouse; графики: Metabase?"


def test_dictionary_word_boundaries() -> None:
    assert apply_dictionary("кликхаусный движок", DICT) == "кликхаусный движок"
    assert apply_dictionary("метабейсовский дашборд", DICT) == "метабейсовский дашборд"
    assert apply_dictionary("my_clickhouse_table", DICT) == "my_clickhouse_table"
    assert apply_dictionary("clickhouse2", DICT) == "clickhouse2"
    assert apply_dictionary("капитал", DICT) == "капитал"   # «апи» внутри слова
    assert apply_dictionary("rapid", DICT) == "rapid"


def test_dictionary_newline_does_not_join() -> None:
    assert apply_dictionary("клик\nхаус", DICT) == "клик\nхаус"


def test_dictionary_russian_case_endings() -> None:
    # латинский термин не склоняется — окончание отбрасывается
    assert apply_dictionary("в клик хаусе", DICT) == "в ClickHouse"
    assert apply_dictionary("из клик-хауса", DICT) == "из ClickHouse"
    assert apply_dictionary("кликхаусом", DICT) == "ClickHouse"
    assert apply_dictionary("из метабейса", DICT) == "из Metabase"
    assert apply_dictionary("к метабейсу", DICT) == "к Metabase"
    # отпадающая буква: «графана» → «графаной», «графане»
    assert apply_dictionary("с графаной и в графане", DICT) == "с Grafana и в Grafana"
    assert apply_dictionary("графан", DICT) == "графан"      # голая основа без окончания
    # заимствования на -о/-е/-и не склоняются: обычные слова с той же основой не трогаем
    miro = [{"term": "Miro", "variants": ["миро"]}, {"term": "Wiki", "variants": ["вики"]},
            {"term": "Vite", "variants": ["вите"]}]
    assert apply_dictionary("лучший в мире, с миром, миру мир", miro) == \
        "лучший в мире, с миром, миру мир"
    assert apply_dictionary("Вика пришла, позвони Вике, с Витей", miro) == \
        "Вика пришла, позвони Вике, с Витей"
    assert apply_dictionary("доска в миро, в вики", miro) == "доска в Miro, в Wiki"
    assert apply_dictionary("в экселе", [{"term": "Excel", "variants": ["эксель"]}]) == "в Excel"
    # кириллический термин получает окончание
    assert apply_dictionary("в сбер банке", DICT) == "в Сбербанке"
    assert apply_dictionary("сбербанком", DICT) == "Сбербанком"


def test_dictionary_short_variant_has_no_endings() -> None:
    items = [{"term": "OS", "variants": ["ос"]}]
    assert apply_dictionary("ос", items) == "OS"
    assert apply_dictionary("осы", items) == "осы"


def test_dictionary_latin_variant_no_endings() -> None:
    items = [{"term": "ClickHouse", "variants": ["click haus"]}]
    assert apply_dictionary("click haus", items) == "ClickHouse"
    assert apply_dictionary("click hausa", items) == "click hausa"


def test_dictionary_exact_user_form_wins() -> None:
    items = [{"term": "ClickHouse", "variants": ["клик хаус", "клик хаусом"]}]
    assert apply_dictionary("с клик хаусом", items) == "с ClickHouse"


def test_dictionary_longest_variant_first() -> None:
    assert apply_dictionary("через апи гейтвей", DICT) == "через API Gateway"
    assert apply_dictionary("через апи", DICT) == "через API"
    assert apply_dictionary("эй пи ай", DICT) == "API"


def test_dictionary_single_pass_no_cascade() -> None:
    items = [{"term": "бета", "variants": ["альфа"]}, {"term": "гамма", "variants": ["бета"]}]
    assert apply_dictionary("альфа бета", items) == "бета гамма"


def test_dictionary_yo_insensitive() -> None:
    items = [{"term": "Алёша", "variants": ["алеша попович"]}]
    assert apply_dictionary("Алёша Попович пришёл", items) == "Алёша пришёл"
    items = [{"term": "Ёлка", "variants": []}]
    assert apply_dictionary("елка", items) == "Ёлка"


def test_dictionary_capitalizes_lowercase_term_at_sentence_start() -> None:
    items = [{"term": "эквайринг", "variants": ["экваринг"]}]
    assert apply_dictionary("Экваринг работает. А экваринг нет", items) == \
        "Эквайринг работает. А эквайринг нет"


def test_dictionary_special_characters_are_literal() -> None:
    items = [{"term": "C++", "variants": ["си плюс плюс"]}, {"term": "Node.js", "variants": []}]
    assert apply_dictionary("пишу на си плюс плюс", items) == "пишу на C++"
    assert apply_dictionary("c and c++", items) == "c and C++"
    assert apply_dictionary("node.js и nodexjs", items) == "Node.js и nodexjs"


def test_dictionary_hyphenated_term() -> None:
    items = [{"term": "Wi-Fi", "variants": ["вай фай"]}]
    assert apply_dictionary("wifi, wi fi и вайфай", items) == "Wi-Fi, Wi-Fi и Wi-Fi"


def test_dictionary_tz_phrase() -> None:
    raw = ("ээ короче слушай ну надо значит завтра в клик хаусе ну пересобрать ту витрину "
           "по комиссиям мерчантов вот и это самое в метабейс дашборд обновить")
    out = apply_dictionary(raw, DICT)
    assert "в ClickHouse ну пересобрать" in out
    assert "в Metabase дашборд" in out
    assert out.startswith("ээ короче слушай")


def test_dictionary_empty_and_broken_input() -> None:
    assert apply_dictionary("", DICT) == ""
    assert apply_dictionary("клик хаус", []) == "клик хаус"
    broken: list[dict[str, Any]] = [
        {"term": None, "variants": ["клик хаус"]},
        {"term": "   ", "variants": ["x"]},
        {"variants": ["y"]},
        {"term": "Metabase", "variants": "метабейс"},       # строка вместо списка
        {"term": "Python", "variants": [None, 5, "", "  питон  "]},
        {"term": "Grafana", "variants": None},
    ]
    assert apply_dictionary("клик хаус, метабейс, питон, grafana", broken) == \
        "клик хаус, Metabase, Python, Grafana"


def test_dictionary_many_items() -> None:
    items = [{"term": f"Term{i}", "variants": [f"терм {i}"]} for i in range(600)]
    assert apply_dictionary("это терм 599 и терм 0", items) == "это Term599 и Term0"


def test_dictionary_multiline_text_preserved() -> None:
    assert apply_dictionary("строка про клик хаус\nи вторая про метабейс\n", DICT) == \
        "строка про ClickHouse\nи вторая про Metabase\n"


def test_dictionary_second_pass_is_stable() -> None:
    """Словарь применяется и после ИИ: повторный проход по уже исправленному тексту
    ничего не портит."""
    for src in ("в клик хаусе и сбер банке", "апи гейтвей и апи", "графану в Grafana",
                "ClickHouse, Metabase, Сбербанке, API Gateway"):
        once = apply_dictionary(src, DICT)
        assert apply_dictionary(once, DICT) == once


def test_misrecognition_filter() -> None:
    bad = misrecognition_filter([*DICT, {"term": "Zennoposter", "variants": ["Xenoposter"]}])
    assert bad is not None
    # варианты — ошибки распознавания, в любом регистре и падеже
    for w in ("Xenoposter", "xenoposter", "метабейс", "графане", "Клик-Хаус", "сбер-банке"):
        assert bad(w), w
    # сами термины (в любом регистре и с падежом у кириллического) и прочие слова — нет
    for w in ("Zennoposter", "zennoposter", "ClickHouse", "clickhouse", "Сбербанке", "сбербанке", "API",
              "Kubernetes", "Тимур", ""):
        assert not bad(w), w
    assert misrecognition_filter([]) is None
    assert misrecognition_filter([{"term": " "}, "мусор"]) is None  # type: ignore[list-item]


# ---------- expand_snippets ----------

SNIPS: list[dict[str, Any]] = [
    {"id": 1, "trigger": "мой имейл", "text": "ivan@example.com"},
    {"id": 2, "trigger": "Подпись", "text": "С уважением,\nИван"},
    {"id": 3, "trigger": "полная подпись", "text": "С уважением,\nИван Петров, CTO"},
    {"id": 4, "trigger": "моё имя", "text": "Иван"},
    {"id": 5, "trigger": "путь", "text": r"C:\new\1"},
]


@pytest.mark.parametrize("src", ["мой имейл", "Мой имейл.", "  МОЙ   ИМЕЙЛ!!! ", "«Мой имейл»",
                                 "— мой имейл?"])
def test_snippet_whole_phrase(src: str) -> None:
    assert expand_snippets(src, SNIPS) == ("ivan@example.com", True)


def test_snippet_whole_phrase_multiline_text() -> None:
    assert expand_snippets("Подпись.", SNIPS) == ("С уважением,\nИван", True)
    assert expand_snippets("Полная подпись", SNIPS) == ("С уважением,\nИван Петров, CTO", True)


def test_snippet_inside_phrase() -> None:
    assert expand_snippets("Отправь на мой имейл, пожалуйста", SNIPS) == \
        ("Отправь на ivan@example.com, пожалуйста", True)
    assert expand_snippets("мой имейл и снова мой имейл", SNIPS) == \
        ("ivan@example.com и снова ivan@example.com", True)
    # длинный триггер важнее короткого
    assert expand_snippets("вот полная подпись", SNIPS) == \
        ("вот С уважением,\nИван Петров, CTO", True)


def test_snippet_yo_and_spaces() -> None:
    assert expand_snippets("мое имя", SNIPS) == ("Иван", True)
    assert expand_snippets("Меня зовут: мое   имя.", SNIPS) == ("Меня зовут: Иван.", True)


def test_snippet_word_boundaries() -> None:
    assert expand_snippets("мой имейлер сломался", SNIPS) == ("мой имейлер сломался", False)
    assert expand_snippets("распутье", SNIPS) == ("распутье", False)


def test_snippet_text_inserted_literally() -> None:
    assert expand_snippets("открой путь", SNIPS) == (r"открой C:\new\1", True)


def test_snippet_no_match_returns_original() -> None:
    assert expand_snippets("Привет, как дела?", SNIPS) == ("Привет, как дела?", False)


def test_snippet_empty_and_broken() -> None:
    assert expand_snippets("", SNIPS) == ("", False)
    assert expand_snippets("мой имейл", []) == ("мой имейл", False)
    broken: list[dict[str, Any]] = [
        {"trigger": None, "text": "x"},
        {"trigger": "...", "text": "x"},
        {"trigger": "пусто", "text": "   "},
        {"trigger": "адрес", "text": None},
        {"trigger": "телефон", "text": "+7 900 000-00-00"},
    ]
    assert expand_snippets("пусто", broken) == ("пусто", False)
    assert expand_snippets("адрес", broken) == ("адрес", False)
    assert expand_snippets("Телефон!", broken) == ("+7 900 000-00-00", True)


# ---------- asr_prompt ----------

def test_asr_prompt_merges_and_dedupes() -> None:
    items = [{"term": "clickhouse"}, {"term": "Metabase"}, {"term": "Сбербанк"},
             {"term": "СБЕРБАНК"}]
    assert asr_prompt(["ClickHouse", "Python", " python "], items) == \
        "ClickHouse, Python, Metabase, Сбербанк."


def test_asr_prompt_limit() -> None:
    terms = [f"Термин{i:03d}" for i in range(200)]
    p = asr_prompt(terms, [], limit_chars=100)
    assert p is not None
    assert len(p) <= 100
    assert p.endswith(".")
    assert p.startswith("Термин000, Термин001")
    # длинный термин пропускается, короткий после него ещё влезает
    p = asr_prompt(["A" * 50, "B" * 500, "C"], [], limit_chars=60)
    assert p == "A" * 50 + ", C."


def test_asr_prompt_empty() -> None:
    assert asr_prompt([], []) is None
    assert asr_prompt(["  ", ""], [{"term": None}, {"x": 1}]) is None
    assert asr_prompt(["Очень длинный термин"], [], limit_chars=5) is None
    assert asr_prompt(["API"], [], limit_chars=0) is None


def test_asr_prompt_collapses_spaces() -> None:
    assert asr_prompt(["API   Gateway"], []) == "API Gateway."
