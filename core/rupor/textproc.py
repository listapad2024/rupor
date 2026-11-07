"""Обработка текста диктовки: словарь, сниппеты, подсказка для распознавания, подсчёт слов.

Модуль без состояния и без зависимостей от Windows — его зовёт движок после ASR:
``apply_dictionary`` → ``expand_snippets`` (если сниппет сработал — ИИ не нужен), и ещё раз
``apply_dictionary`` после ИИ-очистки: словарь пользователя — последнее слово.

Сравнение везде без учёта регистра (Unicode, в т. ч. кириллица) и без различия «ё»/«е»:
распознавание пишет то «всё», то «все», а пользователь в словаре — как привык.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any, NamedTuple

# ---------- общие примитивы ----------

# Слово — буквы/цифры; дефис, апостроф и «_» внутри слова его не рвут
# («ClickHouse-запрос», «во-первых», «don't», «user_id»), точка/запятая/двоеточие —
# только между цифрами («3,5», «10:30»).
_WORD_RE = re.compile(r"[^\W_]+(?:(?:[-\u2010\u2011'\u2019_]|(?<=\d)[.,:](?=\d))[^\W_]+)*")

# Разделители частей многословного варианта: пробелы (без переводов строки) и дефисы.
_SEP_CLASS = r"[\t \u00a0\u2009\u202f\-\u2010-\u2013]"
_JOIN = _SEP_CLASS + "*"          # в тексте между частями — сколько угодно, в т. ч. ноль
_SPLIT_RE = re.compile(r"[\s\-\u2010-\u2013]+")
_EDGE_RE = re.compile(r"^[\W_]+|[\W_]+$")

# Падежные окончания, которые допускаются после кириллического варианта
# («в клик хаусе», «из метабейса», «с графаной»). «е» покрывает и «ё» (см. _lit).
_ENDINGS = ("ами", "ями", "ом", "ем", "ой", "ей", "ою", "ею", "ам", "ям", "ах", "ях",
            "ов", "ев", "а", "я", "у", "ю", "е", "ы", "и")
# Буквы, которые в косвенных падежах отпадают: «графана» → «графан|е», «кафка» → «кафк|и»,
# «эксель» → «эксел|е». Заимствования на -о/-е/-и/-ы не склоняются (Miro, Wiki, Vite):
# иначе «в мире» стало бы «в Miro», «позвони Вике» — «позвони Wiki»
_DROPPABLE = frozenset("аяьй")
_MIN_STEM = 3   # короче — окончания не ищем («ос» → «осы» не должно стать «OS»)


def fold(s: str) -> str:
    """Ключ сравнения: casefold (Unicode, кириллица) и «ё» → «е»."""
    return s.casefold().replace("ё", "е")


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _is_cyr(ch: str) -> bool:
    return "\u0400" <= ch <= "\u04ff"


def normalize_phrase(s: str) -> str:
    """Нормальная форма фразы/триггера: без регистра и «ё», без пунктуации по краям,
    пробелы схлопнуты. «  Мой  имейл! » → «мой имейл»."""
    s = _EDGE_RE.sub("", " ".join(_nfc(s).split()))
    return fold(" ".join(s.split()))


def count_words(text: str) -> int:
    """Число слов: последовательности букв/цифр (кириллица, латиница, любые алфавиты).

    Составные через дефис считаются одним словом («ClickHouse-запрос», «во-первых»),
    числа с разделителем — одним («3,5», «10:30»), эмодзи и пунктуация — не слова."""
    if not text:
        return 0
    return len(_WORD_RE.findall(_nfc(text)))


def _lit(s: str) -> str:
    """Литерал для регулярного выражения, где «е» и «ё» взаимозаменяемы."""
    return "".join("[её]" if ch in "еёЕЁ" else re.escape(ch) for ch in s)


def _pieces(phrase: str) -> list[str]:
    """Части варианта: по пробелам/дефисам и по границе camelCase («ClickHouse» → Click, House)."""
    out: list[str] = []
    for seg in _SPLIT_RE.split(phrase.strip()):
        if not seg:
            continue
        start = 0
        for i in range(1, len(seg)):
            if seg[i - 1].islower() and seg[i].isupper():
                out.append(seg[start:i])
                start = i
        out.append(seg[start:])
    return out


def _core(pieces: list[str]) -> str:
    return _JOIN.join(_lit(p) for p in pieces)


_ENDINGS_RE = "|".join(_lit(e) for e in sorted(_ENDINGS, key=len, reverse=True))


# ---------- словарь ----------

DictKey = tuple[tuple[str, tuple[str, ...]], ...]


def _dict_key(items: Iterable[Any]) -> DictKey:
    """Хэшируемый снимок словаря (для кэша скомпилированного выражения); битое — мимо."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        term = it.get("term")
        if not isinstance(term, str) or not term.strip():
            continue
        raw_vars = it.get("variants") or []
        if isinstance(raw_vars, str):
            raw_vars = [raw_vars]
        if not isinstance(raw_vars, (list, tuple)):
            raw_vars = []
        variants = tuple(_nfc(v).strip() for v in raw_vars if isinstance(v, str) and v.strip())
        out.append((_nfc(term).strip(), variants))
    return tuple(out)


class _Alt(NamedTuple):
    letters: int     # длина варианта без разделителей — длинные пробуются первыми
    order: int       # порядок появления (стабильность при равной длине)
    core: str        # регулярное выражение самого варианта
    anchors: tuple[str, ...]  # части варианта после fold (длинные первыми) — предфильтр
    term: str
    ending: str      # "" — окончаний нет, "?" — окончание допустимо, "!" — обязательно
    keep: bool       # переносить окончание на термин


def _anchors(pieces: list[str]) -> tuple[str, ...]:
    """Каждая часть варианта обязана встретиться в тексте подстрокой — дешёвая проверка
    до регулярного выражения. Длинные первыми: они отсеивают быстрее."""
    return tuple(sorted({fold(p) for p in pieces}, key=len, reverse=True))


def _present(anchors: tuple[str, ...], folded: str) -> bool:
    return all(a in folded for a in anchors)


def _alternation(parts: list[str]) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=32)
def _dictionary_alts(key: DictKey) -> tuple[_Alt, ...]:
    """Все варианты словаря в порядке проверки: длинные раньше коротких, при равной
    длине явные варианты пользователя раньше самих терминов (неявных вариантов)."""
    alts: list[_Alt] = []
    seen: set[tuple[str, bool]] = set()
    sources = [(v, term) for term, variants in key for v in variants]
    sources += [(term, term) for term, _variants in key]   # «clickhouse» → «ClickHouse»
    for v, term in sources:
        term_cyr_end = _is_cyr(term[-1]) and term[-1].isalpha()
        term_has_cyr = any(_is_cyr(ch) for ch in term)
        pieces = _pieces(v)
        if not pieces:
            continue
        letters = sum(len(p) for p in pieces)
        last = pieces[-1]
        inflect = _is_cyr(last[-1]) and last[-1].isalpha() and len(last) >= _MIN_STEM
        # Окончание переносится на термин, только если термин сам кириллический
        # («сбер банке» → «Сбербанке»); латинский термин не склоняется
        # («в клик хаусе» → «в ClickHouse»).
        keep = inflect and term_cyr_end
        k = (fold(" ".join(pieces)), False)
        if k not in seen:
            seen.add(k)
            alts.append(_Alt(letters, len(alts), _core(pieces), _anchors(pieces), term,
                             "?" if inflect else "", keep))
        # Отпадающая последняя буква: «графана» → «графан|е», «графан|ой» (только для
        # некириллических терминов). Окончание обязательно, иначе «графан» → Grafana.
        if (inflect and not term_has_cyr and fold(last[-1]) in _DROPPABLE
                and len(last) - 1 >= _MIN_STEM):
            stem = [*pieces[:-1], last[:-1]]
            k2 = (fold(" ".join(stem)), True)
            if k2 not in seen:
                seen.add(k2)
                alts.append(_Alt(letters - 1, len(alts), _core(stem), _anchors(stem), term,
                                 "!", False))
    alts.sort(key=lambda a: (-a.letters, a.order))
    return tuple(alts)


@lru_cache(maxsize=256)
def _compile_dictionary(alts: tuple[_Alt, ...]) -> tuple[re.Pattern[str],
                                                        dict[str, tuple[str, str | None, bool]]]:
    """Выражение-альтернатива для набора вариантов (уже отсортированного).

    meta[имя группы] = (термин, группа окончания или None, переносить ли окончание)."""
    parts: list[str] = []
    meta: dict[str, tuple[str, str | None, bool]] = {}
    for i, a in enumerate(alts):
        g = f"d{i}"
        if a.ending:
            q = "?" if a.ending == "?" else ""
            parts.append(f"(?P<{g}>{a.core}(?P<{g}e>{_ENDINGS_RE}){q})")
            meta[g] = (a.term, f"{g}e", a.keep)
        else:
            parts.append(f"(?P<{g}>{a.core})")
            meta[g] = (a.term, None, False)
    return _alternation(parts), meta


def apply_dictionary(text: str, items: list[dict[str, Any]]) -> str:
    """Заменить варианты написания на термины словаря.

    Правила:
    - без учёта регистра и различия «ё»/«е»; только целые слова — «кликхаусный» и
      «my_clickhouse» не трогаются; окружающая пунктуация сохраняется;
    - части многословного варианта в тексте могут идти через пробел, дефис или слитно:
      вариант «клик хаус» ловит «клик хаус», «Клик-Хаус», «кликхаус»; перевод строки
      части не склеивает;
    - сам термин тоже нормализуется к своему регистру: «clickhouse», «click house» →
      «ClickHouse» (camelCase термина делится на части);
    - вариант пользователя задаёт точную форму: если нужна особая форма («клик хаусом»),
      её можно добавить отдельным вариантом, и она сработает как есть;
    - падежи: после кириллического варианта (от 3 букв) допускается русское окончание
      («в клик хаусе», «из метабейса», «с графаной» при варианте «графана»).
      Латинский термин в русском тексте не склоняется — окончание отбрасывается:
      «в клик хаусе» → «в ClickHouse». Кириллический термин получает окончание:
      «в сбер банке» → «в Сбербанке» (термин «Сбербанк», вариант «сбер банк»);
    - если термин записан со строчной, а в тексте слово начиналось с заглавной (начало
      предложения) — первая буква термина делается заглавной;
    - при совпадении в одном месте побеждает более длинный вариант («апи гейтвей»
      раньше «апи»), при равной длине — явный вариант раньше самого термина;
    - замена за один проход: результат одной замены не подменяется другой.

    Скорость: в выражение попадают только варианты, все части которых вообще
    встречаются в тексте, так что большой словарь не замедляет каждую диктовку.
    """
    if not text or not items:
        return text
    key = _dict_key(items)
    if not key:
        return text
    return _replace(text, _dictionary_alts(key))


def misrecognition_filter(items: list[dict[str, Any]]) -> Callable[[str], bool] | None:
    """Предикат «это слово — ошибка распознавания из словаря»: словарь заменил бы его
    другим термином (вариант «Xenoposter» у термина «Zennoposter»). Сам термин в любом
    регистре ошибкой не считается. Пустой словарь → None.

    Такие слова не должны попадать ни в подсказку распознаванию, ни в термины ИИ: ошибка,
    которая попала в историю до того, как её внесли в словарь, иначе закрепляет сама себя
    через частые слова — распознавание и ИИ снова пишут её.

    Зовётся перед каждой диктовкой на десятки частых слов, поэтому предикат на один и тот
    же словарь общий, а ответ по слову запоминается: пока словарь не меняется, слово
    проверяется один раз."""
    key = _dict_key(items or [])
    if not key:
        return None
    return _misrecognition_check(key)


_MISRECOGNITION_MEMO = 20_000   # слов на один словарь — больше не запоминаем


@lru_cache(maxsize=8)
def _misrecognition_check(key: DictKey) -> Callable[[str], bool]:
    alts = _dictionary_alts(key)
    memo: dict[str, bool] = {}

    def rewritten(word: str) -> bool:
        hit = memo.get(word)
        if hit is None:
            hit = bool(word) and fold(_replace(word, alts)) != fold(_nfc(word))
            if len(memo) < _MISRECOGNITION_MEMO:
                memo[word] = hit
        return hit
    return rewritten


def _replace(text: str, alts: tuple[_Alt, ...]) -> str:
    """Замена по готовому набору вариантов словаря (см. apply_dictionary)."""
    text = _nfc(text)
    folded = fold(text)
    active = tuple(a for a in alts if _present(a.anchors, folded))
    if not active:
        return text
    pattern, meta = _compile_dictionary(active)

    def repl(m: re.Match[str]) -> str:
        name = m.lastgroup
        if name is None or name not in meta:
            return m.group(0)
        term, end_group, keep = meta[name]
        out = term
        if end_group is not None and keep:
            out += m.group(end_group) or ""
        src = m.group(0)
        if out[:1].islower() and src[:1].isupper():
            out = out[:1].upper() + out[1:]
        return out

    return pattern.sub(repl, text)


# ---------- сниппеты ----------

SnipKey = tuple[tuple[str, str], ...]


class _Snip(NamedTuple):
    core: str
    anchors: tuple[str, ...]
    body: str


def _snip_key(snippets: Iterable[Any]) -> SnipKey:
    out: list[tuple[str, str]] = []
    for sn in snippets:
        if not isinstance(sn, dict):
            continue
        trig, body = sn.get("trigger"), sn.get("text")
        if not isinstance(trig, str) or not isinstance(body, str) or not body.strip():
            continue
        norm = normalize_phrase(trig)
        if norm:
            out.append((norm, body))
    return tuple(out)


@lru_cache(maxsize=32)
def _prepare_snippets(key: SnipKey) -> tuple[dict[str, str], tuple[_Snip, ...]]:
    """(нормальная форма триггера → текст, триггеры для поиска внутри фразы — длинные первыми)."""
    exact: dict[str, str] = {}
    for norm, body in key:
        exact.setdefault(norm, body)
    snips: list[_Snip] = []
    for norm, body in sorted(exact.items(), key=lambda kv: -len(kv[0])):
        pieces = [p for p in _SPLIT_RE.split(norm) if p]
        if pieces:
            snips.append(_Snip(_core(pieces), _anchors(pieces), body))
    return exact, tuple(snips)


@lru_cache(maxsize=256)
def _compile_snippets(snips: tuple[_Snip, ...]) -> tuple[re.Pattern[str], dict[str, str]]:
    groups = {f"s{i}": sn.body for i, sn in enumerate(snips)}
    return _alternation([f"(?P<s{i}>{sn.core})" for i, sn in enumerate(snips)]), groups


def expand_snippets(text: str, snippets: list[dict[str, Any]]) -> tuple[str, bool]:
    """Развернуть сниппеты.

    Вся фраза (без регистра, «ё», пунктуации по краям, с схлопнутыми пробелами) равна
    триггеру → текст сниппета как есть, True («Мой имейл.» → «ivan@example.com»).
    Иначе каждое вхождение триггера целым словом внутри фразы заменяется текстом
    сниппета, флаг True, если была хоть одна замена. Более длинные триггеры важнее.
    """
    if not text or not snippets:
        return text, False
    key = _snip_key(snippets)
    if not key:
        return text, False
    exact, snips = _prepare_snippets(key)
    whole = exact.get(normalize_phrase(text))
    if whole is not None:
        return whole, True
    norm_text = _nfc(text)
    folded = fold(norm_text)
    active = tuple(sn for sn in snips if _present(sn.anchors, folded))
    if not active:
        return text, False
    pattern, groups = _compile_snippets(active)
    changed = False

    def repl(m: re.Match[str]) -> str:
        nonlocal changed
        name = m.lastgroup
        if name is None or name not in groups:
            return m.group(0)
        changed = True
        return groups[name]

    out = pattern.sub(repl, norm_text)
    return (out, True) if changed else (text, False)


# ---------- подсказка распознаванию ----------

def asr_prompt(terms: list[str], items: list[dict[str, Any]], limit_chars: int = 400) -> str | None:
    """Строка для ``initial_prompt`` faster-whisper: «ClickHouse, Metabase, Python.»

    Сначала термины из настроек, затем термины словаря (варианты — это ошибки
    распознавания, в подсказку они не идут). Дубли без учёта регистра/«ё» убираются.
    Итоговая длина (с запятыми и точкой) не больше ``limit_chars``: термин, который уже
    не влезает, пропускается, более короткие после него ещё могут войти. Пусто → None."""
    if limit_chars <= 0:
        return None
    candidates: list[Any] = list(terms or [])
    for it in items or []:
        if isinstance(it, dict):
            candidates.append(it.get("term"))
    out: list[str] = []
    seen: set[str] = set()
    length = 1  # завершающая точка
    for t in candidates:
        if not isinstance(t, str):
            continue
        t = " ".join(_nfc(t).split())
        k = fold(t)
        if not t or k in seen:
            continue
        add = len(t) + (2 if out else 0)
        if length + add > limit_chars:
            continue
        seen.add(k)
        out.append(t)
        length += add
    return ", ".join(out) + "." if out else None
