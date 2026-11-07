"""Похожие написания терминов словаря: термин вносится один раз — искажения система
узнаёт сама.

Распознавание пишет один и тот же термин по-разному: «ZennoBrowser» приходит как
«Zenobowser», «Xena браузер», «Xeno-браузер», «Зенно браузере». Перечислять всё это
вариантами не нужно: такие написания находятся по звучанию и заменяются термином.

Звучание (``sound_key``) — грубая фонетическая запись латиницей: кириллица
транслитерируется («браузер» → brauzer), «кс»/x в начале слова — «з» (Xenon, «Ксено»),
латиница читается по-английски (немая и «магическая» e: node → nod/noud, ow/ou → au,
au → o, -ange/-age → «ейндж/едж», ph → f, ck → k…), части слова по camelCase, пробелу и
дефису склеиваются; звонкие и глухие не различаются (кроме первого звука), гласные —
классами (о/а, е/и), двойные — одна. У латинского термина есть и другие прочтения —
побуквенное («Метабаза», «Геонод»), «u» как «а» («гитхаб»), мягкое g («Джемини»), «a» в
начале как «э» («Энтропик»), «i» перед ld/nd как «ай» («Вайлд Дак»): кириллица
сравнивается со всеми.

Когда фрагмент текста (1–3 слова без знаков препинания между ними) — искажение термина:

- **одно слово** звучит как термин или его вариант с точностью до правок (4 звука — буква в
  букву, 5–7 — точно, 8–11 — одна правка, от 12 — две; 3 и короче — не ищем) **и такого
  слова нет в языке** — проверка орфографии Windows (spell.py): ошибки распознавания почти
  всегда не-слова («Xenoposter», «кубернетис», «графане»), а настоящие слова («курсор»,
  «редис», «телеграмма», cloud) не трогаются. Слова, которые пользователь сам часто
  диктует («ноды»), — тоже настоящие. Словаря языка нет — похожих написаний нет (только
  точный словарь и фрагменты со словом-термином: «Zenno браузер»). Имя собственное, которое словарь знает лишь с заглавной («Телеграм» в
  начале фразы), — не-слово только для длинного термина (от 8 звуков) при точном совпадении;
- **несколько слов** совпадают с частями термина по одной («Xena браузер» = Zenno + Browser;
  у каждой части свой запас правок) или звучат как термин целиком, причём хотя бы одна часть
  фрагмента в точности равна части термина, — и среди слов есть не-слово или слово-термин
  («Zenno Bowser», «Xeno poster»). Так «купи редис» не станет Kubernetes, «зона браузера» —
  ZennoBrowser, «Зина браузер» — тоже. Короткая часть термина (до 4 звуков) совпадает
  только точно («Зен браузер» ≠ ZennoBrowser), частое имя — настоящее слово («Ксена
  браузер»), своё частое слово пользователя последним или не частью термина — обычная речь
  («гео ноды», «гео-ноды»), а термин из двух слов по одному слову-термину и настоящему слову
  не склеивается («Claude код» ≠ Claude Code).

После кириллического слова допускается падежное окончание: кириллический термин его
получает («ксенопостера» → «Зеннопостера»), латинский — нет («в зенно браузере» →
«в ZennoBrowser»). Отпадающая гласная — только у склоняемых терминов на -а/-я/-и/-ы и
только с окончаниями женского рода («в графане», «в джире»; «Мирой» ≠ Miro, «Асаном» ≠
Asana). Кириллица против кириллического термина сравнивается ещё и побуквенно, с мягким
знаком («Ксенька» ≠ «Зенка»), «кс» как «з» — только у длинных терминов («Ксенка» ≠
«Зенка», «ксенопостера» → «Зеннопостера»). Окончание или частица через дефис/апостроф у
исправленного слова сохраняются («Zenobowser'е» → «ZennoBrowser'е», «Графане-то» →
«Grafana-то»). Не заменяются: латиница кириллическим термином (Sberbank),
слово на «ь» латинским термином («Суперсеть»), фамилия на -ов/-ев с заглавной («Слаков»),
уже термины (в т. ч. с окончанием через дефис или апостроф — «Postgres'е»,
«Zennoposter-е», и слова многословного термина — «Grafana Cloud»), слова с цифрами, части
путей, адресов и имён файлов, слова, склеенные ударением или невидимым знаком
(«Ксено́постер»), слова короче 3 букв в составном фрагменте; тире между словами их не
склеивает («Zenno – браузер»). Термин, записанный
раздельно или через дефис («Zenno Poster», «Cloud Flare», «Proxy-pool»), пишется как в
словаре — если части с заглавной или среди них есть не-слово («air flow», «air-flow»
остаются).

Точные варианты словаря важнее: сначала ``textproc.apply_dictionary``, потом похожие
написания — в оставшемся тексте. Слова из текста модуль не логирует.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from functools import lru_cache
from typing import Any, NamedTuple

from . import learn, spell, textproc, vocab
from .textproc import _ENDINGS, _WORD_RE, fold

# ---------- звучание ----------

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "o", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "\u00e7", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "u",
    "я": "a", "і": "i", "ї": "i", "є": "e", "ґ": "g", "ў": "u",
}
CH = "\u00e7"           # звук «ч» (Latin ch, j, «дж», «ч») — отдельный символ ключа
_CYR = re.compile(r"[\u0400-\u04ff]")
_LAT = re.compile(r"[A-Za-z]")
_PIECE_SPLIT = re.compile(r"[-\u2010\u2011'\u2019_]+")
# Долгие гласные — заглавными, чтобы правила гласных сочетаний их не переписали
# (site → «сайт», а не «сейт»); в конце английского прочтения — строчными
_LONG = {"a": "EI", "o": "OU", "i": "AI", "u": "IU", "e": "I"}
# «Магическая e»: одиночная гласная + согласная + немая e на конце (note, base, site)
_MAGIC_E = re.compile(r"(?<![aeiouy])([aeiou])([bcdfgklmnprstvz])e$")
_SILENT_E = re.compile(r"(?<=[^aeiouy])e$")
# Английские окончания с «дж» — до «магической e»: change → чейндж, message → меседж
_EN_ENDINGS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), r) for p, r in (
        (r"ange$", "eindzh"), (r"(?<=[^aeiou])(?<=.{3})age$", "edzh"), (r"dge$", "dzh"),
        (r"(?<=[^aeiou])ge$", "dzh"),
    ))
_CLOSED_U = re.compile(r"(?<=[^aeiou])u(?=[^aeiouy](?:[^aeiouy]|$))")   # hub, cluster
_SOFT_G = re.compile(r"g(?=[eiy])")                                       # Gemini
_SHORT_A = re.compile(r"^a(?=[^aeiouyr])")                                # Anthropic, any
_LONG_I = re.compile(r"i(?=[ln]d(?:[^aeiou]|$))")                         # wild, kind

# Согласные — одинаково во всех прочтениях
_CONS_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), r) for p, r in (
        (r"sch", "sh"), (r"tch", CH), (r"ch(?=r)", "h"), (r"ch", CH), (r"tion", "shn"),
        (r"ph", "f"), (r"th", "t"), (r"ck", "k"), (r"qu", "kv"), (r"wh", "v"), (r"x", "ks"),
        (r"j", CH), (r"c(?=[eiy])", "s"), (r"c", "k"), (r"q", "k"), (r"w", "v"),
    ))
# Английские гласные сочетания — только в «английском» прочтении
_VOWEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), r) for p, r in (
        (r"igh", "ai"), (r"ow(?=[^aeiou]|$)", "au"), (r"ou", "au"), (r"ea", "i"),
        (r"ee", "i"), (r"oo", "u"), (r"a[iy]", "ei"), (r"ey", "ei"),
    ))
_AU = re.compile(r"au")         # Claude, sauce → «о» (после ou → au: cloud остаётся «ау»)
# Классы: звонкие = глухие, гласные как в безударной речи
_CLASS = str.maketrans({"b": "p", "d": "t", "g": "k", "v": "f", "z": "s", "o": "a",
                        "i": "e", "y": "e"})
_RUN = re.compile(r"(.)\1+")
READINGS = ("en", "letters", "closed_u", "soft_g", "short_a", "long_i")


class Key(NamedTuple):
    """Звучание: ``head`` — первый звук без слияния звонких/глухих, ``body`` — вся запись
    по классам (с ней и сравнивают)."""
    head: str
    body: str


def _norm_tail(s: str) -> str:
    return (s.replace("dzh", CH).replace("d" + CH, CH).replace("zh", "sh")
            .replace("kh", "h"))


def _cyr_piece(p: str) -> str:
    s = "".join(_TRANSLIT.get(ch, ch) for ch in p)
    s = re.sub(r"[^a-z0-9\u00e7]", "", s)
    if s.startswith("ks"):
        s = "z" + s[2:]                           # «Ксено…» ← Xeno…
    return _norm_tail(s)


def _lat_piece(p: str, reading: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", p)
    if not s:
        return s
    if s.startswith("x"):
        s = "z" + s[1:]                           # Xenon, Xena — «з»
    s = re.sub(r"c(?=[aou])", "k", s)             # scale, cat — «к» до «магической e»
    if reading == "soft_g":
        s = _SOFT_G.sub("dzh", s)
    if reading == "short_a":
        s = _SHORT_A.sub("e", s)
    if reading == "letters":
        if len(s) >= 4:
            s = _SILENT_E.sub("", s)
        s = _AU.sub("o", s)
    else:
        if reading in ("closed_u", "long_i"):
            s = _CLOSED_U.sub("a", s)
        if reading == "long_i":
            s = _LONG_I.sub("AI", s)
        for pat, rep in _EN_ENDINGS:
            s = pat.sub(rep, s)
        if len(s) >= 4:
            m = _MAGIC_E.search(s)
            if m:
                cons = "s" if m.group(2) == "c" else m.group(2)     # ice, face — «с»
                s = s[:m.start()] + _LONG[m.group(1)] + cons
            else:
                s = _SILENT_E.sub("", s)
        s = _AU.sub("o", s)
        for pat, rep in _VOWEL_RULES:
            s = pat.sub(rep, s)
        s = s.lower()
    s = s.replace("dzh", CH)
    for pat, rep in _CONS_RULES:
        s = pat.sub(rep, s)
    return _norm_tail(s.replace("y", "i"))


def _piece_raw(p: str, reading: str = "en") -> str:
    """Одна часть слова → запись латиницей до слияния в классы."""
    f = fold(p)
    return _cyr_piece(f) if _CYR.search(f) else _lat_piece(f, reading)


def _pieces(word: str) -> list[str]:
    """Части слова: по дефису/апострофу/«_» и по границе camelCase («ZennoBrowser»)."""
    out: list[str] = []
    for seg in _PIECE_SPLIT.split(word):
        start = 0
        for i in range(1, len(seg)):
            if seg[i - 1].islower() and seg[i].isupper():
                out.append(seg[start:i])
                start = i
        if seg[start:]:
            out.append(seg[start:])
    return out


def _collapse(s: str) -> str:
    return _RUN.sub(lambda m: m.group(1), s)


def _classes(s: str) -> str:
    return _collapse(s.translate(_CLASS))


def _key(raw: str) -> Key:
    if not raw:
        return Key("", "")
    return Key(raw[0].translate(str.maketrans("oiy", "aee")), _classes(raw))


def _all_pieces(text: str) -> list[str]:
    return [p for w in unicodedata.normalize("NFC", text).split() for p in _pieces(w)]


@lru_cache(maxsize=8192)
def sound_key(text: str, reading: str = "en") -> Key:
    """Звучание слова или фразы («Xena браузер» == «ZennoBrowser»)."""
    return _key("".join(_piece_raw(p, reading) for p in _all_pieces(text)))


@lru_cache(maxsize=8192)
def raw_key(text: str, reading: str = "en") -> str:
    """Запись без слияния звуков в классы (двойные — одна): для коротких терминов
    («логи» ≠ Loki)."""
    return _collapse("".join(_piece_raw(p, reading) for p in _all_pieces(text)))


@lru_cache(maxsize=8192)
def piece_keys(text: str, reading: str = "en") -> tuple[str, ...]:
    """Звучание каждой части по отдельности (для сравнения по частям)."""
    return tuple(_classes(_piece_raw(p, reading)) for p in _all_pieces(text))


@lru_cache(maxsize=8192)
def cyr_letters(text: str, ks: bool = True) -> str:
    """Кириллица буква в букву (для кириллицы против кириллического термина): без регистра
    и «ё», с «ь», двойные — одна; ks — «кс» в начале как «з» (для длинных терминов)."""
    s = "".join(ch for ch in fold(unicodedata.normalize("NFC", text)) if ch.isalpha())
    if ks and s.startswith("кс"):
        s = "з" + s[2:]
    return _collapse(s)


def _max_edits(n: int) -> int:
    """Сколько правок допустимо для звучания такой длины (-1 — похожих не искать)."""
    if n <= 3:
        return -1
    if n <= 7:
        return 0
    if n <= 11:
        return 1
    return 2


def _piece_edits(n: int) -> int:
    """Запас правок одной части при сравнении по частям: короткие — только точно."""
    return 0 if n <= 7 else 1 if n <= 11 else 2


def _distance(a: str, b: str, limit: int) -> int:
    """Расстояние Левенштейна, если оно ≤ limit, иначе limit + 1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if a == b:
        return 0
    if limit == 0:
        return 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1] if prev[-1] <= limit else limit + 1


# ---------- термины ----------

SHORT_SOUNDS = 4        # термин из стольких звуков сравнивается буква в букву
LONG_SOUNDS = 8         # «длинный» термин: имя собственное в начале фразы, целый ключ из слов
# Окончания после отпавшей гласной склоняемого термина — только женского рода
_FEM_ENDINGS = frozenset(("е", "у", "ю", "ой", "ою", "ей", "ею", "ы", "и", "ам", "ям", "ами",
                          "ями", "ах", "ях"))
_SURNAME_ENDINGS = frozenset(("ов", "ев"))


class _Anchor(NamedTuple):
    key: Key
    edits: int                  # допустимо правок для звучания целиком
    pieces: tuple[str, ...]     # звучание частей (то же прочтение)
    raw: str                    # запись без классов: у коротких (≤ SHORT_SOUNDS) — только она
    letters: str                # кириллический источник буква в букву ("" — латиница)
    letters_as_is: str          # то же без «кс» → «з» (короткие термины)
    term: str
    order: int
    cyr_term: bool              # термин кириллический — окончание переносится
    need_ending: bool           # без последней гласной: окончание (женского рода) обязательно


class _Index(NamedTuple):
    by_head: dict[str, tuple[_Anchor, ...]]
    spaced: frozenset[str]      # fold терминов как написаны («grafana cloud»)
    joined: dict[str, str]      # fold терминов без пробелов/дефисов → термин
    max_words: int              # сколько слов текста сравнивать с термином
    term_words: int             # самый длинный термин в словах (защита «Proxy Pool Admin Panel»)


def _is_cyr_word(w: str) -> bool:
    return bool(_CYR.search(w))


def _latin_only(w: str) -> bool:
    return bool(_LAT.search(w)) and not _CYR.search(w)


def _common_word(w: str) -> bool:
    """Частое слово языка (частотные списки): такой вариант словаря — только точно."""
    if _is_cyr_word(w):
        return vocab.is_common_ru(w) or learn._is_common(w, names=True)
    return vocab.is_everyday_en(w)


def _joined(s: str) -> str:
    return re.sub(r"[\s\-\u2010-\u2013'\u2019_]+", "", fold(s))


def _readings(src: str) -> set[str]:
    if not _latin_only(src):
        return {"en"}
    out = {"en", "letters"}
    f = fold(src)
    if _CLOSED_U.search(f):
        out.add("closed_u")
    if _SOFT_G.search(f):
        out.add("soft_g")
    if _SHORT_A.search(f):
        out.add("short_a")
    if _LONG_I.search(f):
        out.add("long_i")
    return out


def _declinable(src: str) -> bool:
    """Склоняется в русском с отпадающей гласной: латиница на -a (Grafana, Jira, Kafka),
    кириллица на -а/-я/-и/-ы. Miro, Loki, Avito, Zenno — не склоняются."""
    last = fold(src[-1:])
    return last == "a" if _latin_only(src) else last in "аяиы"


@lru_cache(maxsize=16)
def _index(key: textproc.DictKey) -> _Index:
    anchors: dict[str, list[_Anchor]] = {}
    seen: set[tuple[Key, str, str, bool]] = set()
    max_words = 1
    order = 0

    def add(k: Key, edits: int, pieces: tuple[str, ...], raw: str, letters: tuple[str, str],
            term: str, cyr_term: bool, need_ending: bool) -> None:
        nonlocal order
        if (k, raw, term, need_ending) in seen:
            return
        seen.add((k, raw, term, need_ending))
        anchors.setdefault(k.head, []).append(
            _Anchor(k, edits, pieces, raw, letters[0], letters[1], term, order, cyr_term,
                    need_ending))
        order += 1

    for term, variants in key:
        cyr_term = _is_cyr_word(term) and term[-1:].isalpha() and _is_cyr_word(term[-1])
        for src in (term, *variants):
            words = src.split()
            if len(words) == 1 and src != term and _common_word(src):
                continue                          # вариант-обычное слово — только точно
            cyr_src = _is_cyr_word(src) and not _LAT.search(src)
            letters = (cyr_letters(src), cyr_letters(src, ks=False)) if cyr_src else ("", "")
            for reading in sorted(_readings(src)):
                k = sound_key(src, reading)
                pieces = piece_keys(src, reading)
                edits = _max_edits(len(k.body))
                if edits < 0 or not all(pieces):
                    continue
                max_words = max(max_words, min(3, len(pieces) + 1))
                raw = raw_key(src, reading)
                add(k, edits, pieces, raw, letters, term, cyr_term, False)
                # Отпадающая гласная на конце: «Grafana» ↔ «графан|е», «Jira» ↔ «джир|е»
                if (_declinable(src) and k.body[-1:] in "aeu" and len(k.body) - 1 >= 3
                        and len(pieces[-1]) > 1):
                    short = Key(k.head, k.body[:-1])
                    add(short, max(0, _max_edits(len(short.body))),
                        (*pieces[:-1], pieces[-1][:-1]),
                        raw[:-1] if raw[-1:] in "aeiouy" else raw,
                        (letters[0][:-1], letters[1][:-1]), term, cyr_term, True)
    return _Index({h: tuple(v) for h, v in anchors.items()},
                  frozenset(fold(" ".join(t.split())) for t, _ in key),
                  {_joined(t): t for t, _ in key}, max_words,
                  max((len(t.split()) for t, _ in key), default=1))


# ---------- поиск в тексте ----------

class FuzzyMatch(NamedTuple):
    start: int
    end: int
    replacement: str
    term: str
    edits: int


_SEP_OK = re.compile(r"[ \t\u00a0\u2009\u202f]+|[-\u2010\u2011]")   # пробелы или дефис без пробелов (тире — нет)
MIN_PART_LETTERS = 3    # слово короче не входит в составной фрагмент («Постгрес и»)
_PATH_BEFORE = frozenset("/\\@._#$%&=~")
_PATH_AFTER = frozenset("/\\@_#$%&=~")
_SUFFIXED = re.compile(r"^(.+?)[-'\u2019]([\u0400-\u04ff]{1,4}|s)$")   # Postgres'е, X's

# Что словарь языка знает о слове
WORD, NON_WORD, PROPER, UNKNOWN = "word", "non", "proper", "unknown"


def _near_path(text: str, start: int, end: int) -> bool:
    """Слово — часть пути, адреса, имени файла или кода («/opt/grafana», «a@b», «x.ini»)."""
    b = text[start - 1] if start > 0 else ""
    a = text[end] if end < len(text) else ""
    if b in _PATH_BEFORE or a in _PATH_AFTER:
        return True
    return a == "." and end + 1 < len(text) and text[end + 1].isalnum()


def _endings(word: str) -> list[tuple[str, str]]:
    """(основа, окончание) для кириллического слова: сначала без отрыва, потом окончания."""
    out = [(word, "")]
    if _is_cyr_word(word[-1:]):
        low = fold(word)
        for e in sorted(_ENDINGS, key=len, reverse=True):
            if low.endswith(e) and len(low) - len(e) >= 3:
                out.append((word[:-len(e)], word[-len(e):]))
    return out


def _letters(w: str) -> int:
    return sum(ch.isalpha() for ch in w)


def _first_letter_cyr(w: str) -> bool:
    """Письменность фрагмента — по первой букве: «Zenno постер» — латиница."""
    for ch in w:
        if ch.isalpha():
            return bool(_CYR.match(ch))
    return False


def _lexicon_words(words: set[str]) -> set[str]:
    """Что спросить у словаря языка: слова, их части через дефис и строчные формы слов с
    заглавной (имя собственное или нет)."""
    out = set(words)
    for w in words:
        out.update(p for p in _PIECE_SPLIT.split(w) if _letters(p) >= 3 and p != w)
        if w[:1].isupper():
            out.add(w.lower())
    return out


def _status(w: str, known: dict[str, bool | None]) -> str:
    """WORD / NON_WORD / PROPER (словарь знает только с заглавной) / UNKNOWN (словаря нет)."""
    k = known.get(w)
    if k is None and _PIECE_SPLIT.search(w):
        parts = [p for p in _PIECE_SPLIT.split(w) if _letters(p) >= 3]
        stats = [known.get(p) for p in parts]
        if any(s is False for s in stats):
            return NON_WORD
        return WORD if stats and all(s is True for s in stats) else UNKNOWN
    if k is None:
        return UNKNOWN
    if not k:
        if _PIECE_SPLIT.search(w):
            parts = [p for p in _PIECE_SPLIT.split(w) if _letters(p) >= 3]
            if parts and all(known.get(p) is True for p in parts):
                return WORD                       # «CC-консоли»: части — настоящие слова
        return NON_WORD
    if w[:1].isupper() and w.lower() != w and known.get(w.lower()) is False:
        return PROPER
    return WORD


class _Ctx(NamedTuple):
    idx: _Index
    known: dict[str, bool | None]
    user_words: frozenset[str]      # vocab.group_key частых слов пользователя


def _is_user_word(ctx: _Ctx, w: str) -> bool:
    """Слово, которое пользователь сам часто диктует («ноды»), — настоящее, если в одиночку
    оно не искажение термина (иначе частое искажение закрепилось бы)."""
    return (bool(ctx.user_words) and vocab.group_key(w) in ctx.user_words
            and _match(ctx._replace(user_words=frozenset()), [w]) is None)


def _match(ctx: _Ctx, words: list[str]) -> tuple[_Anchor, str, int] | None:
    """Лучший термин для фрагмента: (якорь, окончание, правок) или None."""
    idx = ctx.idx
    first_cyr = _first_letter_cyr(words[0])
    latin_frag = all(_latin_only(w) for w in words)
    cyr_frag = all(_is_cyr_word(w) and not _LAT.search(w) for w in words)
    stats = [WORD if fold(w) in idx.spaced else _status(w, ctx.known) for w in words]
    if len(words) > 1:
        # частое имя в составном фрагменте — настоящее слово: «Ксена браузер»
        stats = [WORD if s == NON_WORD and _is_cyr_word(w) and learn._is_common(w, names=True)
                 else s for s, w in zip(stats, words)]
    term_word = [fold(w) in idx.spaced for w in words]
    # Свои частые слова пользователя, которых нет в словаре Windows («гео», «ноды»)
    mine = [s == NON_WORD and _is_user_word(ctx, w) for s, w in zip(stats, words)]
    mine_parts: list[tuple[bool, str]] = []       # (последняя ли часть, часть) у «гео-ноды»
    if len(words) == 1 and ctx.user_words and _PIECE_SPLIT.search(words[0]):
        parts = [p for p in _PIECE_SPLIT.split(words[0]) if p]
        mine_parts = [(j == len(parts) - 1, p) for j, p in enumerate(parts)
                      if ctx.known.get(p) is False and _is_user_word(ctx, p)]
    if len(words) > 1:
        mine_parts = [(j == len(words) - 1, w) for j, (w, m) in enumerate(zip(words, mine)) if m]
    non = [s == NON_WORD and (len(words) > 1 or not m) for s, m in zip(stats, mine)]
    best: tuple[int, int, int, _Anchor, str] | None = None
    for stem, ending in _endings(words[-1]):
        text = " ".join([*words[:-1], stem])
        k = sound_key(text)
        if not k.body:
            continue
        frag_pieces = piece_keys(text)
        frag_raw = raw_key(text)
        frag_letters = cyr_letters(text) if cyr_frag else ""
        frag_letters_as_is = cyr_letters(text, ks=False) if cyr_frag else ""
        soft_end = fold(stem).endswith("ь")
        for a in idx.by_head.get(k.head, ()):
            if a.need_ending and (not ending or fold(ending) not in _FEM_ENDINGS):
                continue                          # «Мирой», «Асаном»
            if latin_frag and a.cyr_term:
                continue                          # Sberbank ≠ «Сбербанк»
            if not a.cyr_term:
                if soft_end:
                    continue                      # «Суперсеть» ≠ Superset
                if (len(words) == 1 and fold(ending) in _SURNAME_ENDINGS
                        and words[0][:1].isupper()):
                    continue                      # «Слаков» ≠ Slack
            if len(a.key.body) <= SHORT_SOUNDS and frag_raw != a.raw:
                continue                          # короткий термин — только буква в букву
            if a.letters and frag_letters:
                # кириллица буква в букву: «Ксенька» ≠ «Зенка»; «кс» → «з» — только у длинных
                # («ксенопостера» → «Зеннопостера», но «Ксенка» ≠ «Зенка»)
                long_term = len(a.key.body) >= LONG_SOUNDS
                fl, al = ((frag_letters, a.letters) if long_term
                          else (frag_letters_as_is, a.letters_as_is))
                if _distance(fl, al, max(0, a.edits)) > max(0, a.edits):
                    continue
            if mine_parts and any(last or not all(p in a.pieces for p in piece_keys(w))
                                  for last, w in mine_parts):
                continue                          # «гео ноды», «гео-ноды» — обычная речь
            gate_non = any(non)
            if not gate_non and any(term_word) and " " in a.term:
                continue                          # «Claude код» — не обязательно Claude Code
            gate = gate_non or any(term_word)
            d = _aligned(frag_pieces, a.pieces) if gate else None
            if d is None:
                d = _distance(k.body, a.key.body, a.edits)
                if d > a.edits:
                    continue
                if len(frag_pieces) == len(a.pieces) and any(
                        len(ap) <= SHORT_SOUNDS and fp != ap
                        for fp, ap in zip(frag_pieces, a.pieces)):
                    continue                      # правка — не в короткой части: «Зен браузер»
                if len(words) == 1:
                    proper_ok = (stats[0] == PROPER and first_cyr and d == 0
                                 and len(a.key.body) >= LONG_SOUNDS)   # «Телеграм» в начале
                    if not (non[0] or proper_ok):
                        continue                  # настоящее слово языка: «курсор», cloud
                else:
                    latin_proper = any(s == PROPER and _latin_only(w)
                                       for s, w in zip(stats, words)) and d == 0
                    if not (gate or latin_proper):
                        continue                  # «купи редис» ≠ Kubernetes
                    if not (d == 0 and len(a.key.body) >= LONG_SOUNDS) and not any(
                            len(p) >= 3 and p in a.pieces for p in frag_pieces):
                        continue                  # целиком точно или часть — в точности часть термина
            same_script = 0 if a.cyr_term == first_cyr else 1
            rank = (d, same_script, a.order)
            if best is None or rank < best[:3]:
                best = (d, same_script, a.order, a, ending)
    if best is None:
        return None
    return best[3], best[4], best[0]


def _aligned(frag: tuple[str, ...], anchor: tuple[str, ...]) -> int | None:
    """Сравнение по частям: правок всего или None (какая-то часть не совпала)."""
    if len(frag) != len(anchor) or len(frag) < 2:
        return None
    total = 0
    for f, a in zip(frag, anchor):
        d = _distance(f, a, _piece_edits(len(a)))
        if d > _piece_edits(len(a)):
            return None
        total += d
    return total


def _replacement(a: _Anchor, ending: str, src: str) -> str:
    out = a.term
    if ending and a.cyr_term:
        if fold(out[-1:]) in "аяоеиыйь":
            out = out[:-1]                        # «Госуслуг|и» + «ах», «Зенк|а» + «у»
        out += ending
    if out[:1].islower() and src[:1].isupper() and a.term == a.term.lower():
        out = out[:1].upper() + out[1:]  # «iPhone», «macOS» не поднимаем: регистр задан
    return out


def _protected(idx: _Index, toks: list[tuple[str, int, int]]) -> tuple[set[int], set[int]]:
    """Слова, которые уже термин. (жёстко — с окончанием через дефис/апостроф «Postgres'е»
    и слова многословного термина «Grafana Cloud», «Grafana Cloud'е»: их не трогаем совсем;
    слово-термин само по себе: «Zenno» не заменяем, но «Zenno Poster» → Zennoposter и
    «Zenno браузер» → ZennoBrowser — можно)."""
    hard: set[int] = set()
    single: set[int] = set()
    for i, (w, _s, _e) in enumerate(toks):
        m = _SUFFIXED.match(w)
        if m and (fold(m.group(1)) in idx.spaced or _joined(m.group(1)) in idx.joined):
            hard.add(i)
        elif fold(w) in idx.spaced:
            single.add(i)
        for n in range(2, idx.term_words + 1):
            if i + n > len(toks):
                break
            ws = [t[0] for t in toks[i:i + n]]
            m = _SUFFIXED.match(ws[-1])
            if fold(" ".join(ws)) in idx.spaced or (
                    m and fold(" ".join([*ws[:-1], m.group(1)])) in idx.spaced):
                hard.update(range(i, i + n))
    return hard, single - hard


def _joinable(ctx: _Ctx, words: list[str]) -> bool:
    """Раздельное написание термина склеивается, если части с заглавной («Cloud Flare»,
    «Zenno Poster») или среди них не-слово («Tail scale»): «air flow» остаётся. Через
    дефис — если хоть одна часть с заглавной или не-слово («Proxy-pool»; «air-flow» — нет)."""
    parts = [p for w in words for p in _PIECE_SPLIT.split(w) if p]
    if len(parts) < 2:
        return False
    caps = [p[:1].isupper() for p in parts]
    if all(caps) or (len(words) == 1 and any(caps)):
        return True
    return any(_status(p, ctx.known) == NON_WORD for p in parts)


_SUFFIX_TAILS = frozenset((*_ENDINGS, "s", "то", "ка", "таки"))


def _split_suffix(w: str) -> tuple[str, str]:
    """Окончание или частица через дефис/апостроф: «Zenobowser'е» → («Zenobowser», «'е»),
    «Графане-то» → («Графане», «-то»). Иначе (w, "")."""
    m = _SUFFIXED.match(w)
    if m and _letters(m.group(1)) >= MIN_PART_LETTERS and fold(m.group(2)) in _SUFFIX_TAILS:
        return m.group(1), w[len(m.group(1)):]
    return w, ""


def _glued(text: str, start: int, end: int) -> bool:
    """Слово склеено невидимым знаком или ударением («Ксено́постер», мягкий перенос, ZWJ):
    это часть другого слова — не трогаем."""
    for ch in (text[start - 1:start], text[end:end + 1]):
        if ch and unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            return True
    return False


def _find(text: str, key: textproc.DictKey, user_words: frozenset[str]) -> list[FuzzyMatch]:
    idx = _index(key)
    if not idx.by_head:
        return []
    toks = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    if not toks:
        return []
    hard, single = _protected(idx, toks)
    ctx = _Ctx(idx, spell.known_words(_lexicon_words({t[0] for t in toks})), user_words)
    out: list[FuzzyMatch] = []
    i = 0
    while i < len(toks):
        # Фрагменты из 1…max_words слов, начиная с i: меньше правок важнее, при равенстве —
        # длиннее («Xena браузер» целиком)
        choice: tuple[int, int, FuzzyMatch | None] | None = None
        for n in range(1, min(idx.max_words, len(toks) - i) + 1):
            span = toks[i:i + n]
            if any(i + j in hard for j in range(n)):
                break                             # правильный термин не трогаем
            inner = [j for j in range(n) if i + j in single]
            if n > 1 and not _SEP_OK.fullmatch(text[span[-2][2]:span[-1][1]]):
                break                             # между словами знак препинания или строка
            if n > 1 and min(_letters(span[0][0]), _letters(span[-1][0])) < MIN_PART_LETTERS:
                break                             # «и», «в», «на» к термину не приклеиваются
            words = [t[0] for t in span]
            start, end = span[0][1], span[-1][2]
            if (any(ch.isdigit() for w in words for ch in w) or _near_path(text, start, end)
                    or _glued(text, start, end)):
                continue
            head, tail = _split_suffix(words[-1])   # «Zenobowser'е» — окончание сохраняется
            words = [*words[:-1], head]
            joined = _joined(" ".join(words))
            if joined in idx.joined and ((n == 1 and _PIECE_SPLIT.search(words[0])
                                          and _joinable(ctx, words))
                                         or (n > 1 and _joinable(ctx, words))):
                term = idx.joined[joined]         # «Zenno Poster», «Proxy-pool» — как в словаре
                rep = term + tail
                m = FuzzyMatch(start, end, rep, term, 0) if rep != text[start:end] else None
                if choice is None or (0, -n) < choice[:2]:
                    choice = (0, -n, m)
                continue
            if inner and n == 1:
                continue
            found = _match(ctx, words)
            if found is None:
                continue
            a, ending, d = found
            if inner and (inner != [0] or not sound_key(a.term).body.startswith(
                    sound_key(words[0]).body)):
                continue                          # термин не поглощается соседним словом
            rep = _replacement(a, ending, words[0]) + tail
            m = FuzzyMatch(start, end, rep, a.term, d) if rep != text[start:end] else None
            if choice is None or (d, -n) < choice[:2]:
                choice = (d, -n, m)
        if choice is not None and choice[2] is not None:
            found_m = choice[2]
            out.append(found_m)
            i += sum(1 for t in toks[i:] if t[1] < found_m.end)
        else:
            i += 1
    return out


def find(text: str, items: list[dict[str, Any]],
         user_words: frozenset[str] = frozenset()) -> list[FuzzyMatch]:
    """Похожие на термины словаря написания в тексте (без точных вариантов — их делает
    textproc.apply_dictionary). user_words — vocab.group_key частых слов пользователя."""
    if not text or not items:
        return []
    key = textproc._dict_key(items)
    return _find(text, key, user_words) if key else []


def _apply_key(text: str, key: textproc.DictKey, fuzzy: bool,
               user_words: frozenset[str] = frozenset()) -> tuple[str, int]:
    text = textproc._replace(text, textproc._dictionary_alts(key))
    if not fuzzy or not text:
        return text, 0
    hits = _find(text, key, user_words)
    if not hits:
        return text, 0
    parts: list[str] = []
    pos = 0
    for h in hits:
        parts += [text[pos:h.start], h.replacement]
        pos = h.end
    parts.append(text[pos:])
    return "".join(parts), len(hits)


def apply(text: str, items: list[dict[str, Any]], fuzzy: bool = True,
          user_words: frozenset[str] = frozenset()) -> tuple[str, int]:
    """Словарь целиком: точные варианты (textproc.apply_dictionary), затем, если fuzzy,
    похожие написания. → (текст, сколько похожих написаний заменено)."""
    if not text or not items:
        return text, 0
    key = textproc._dict_key(items)
    if not key:
        return text, 0
    return _apply_key(text, key, fuzzy, user_words)


def apply_text(text: str, items: list[dict[str, Any]], fuzzy: bool = True,
               user_words: frozenset[str] = frozenset()) -> str:
    return apply(text, items, fuzzy, user_words)[0]


# ---------- ошибки распознавания для подсказок ----------

_MEMO = 20_000


def misrecognition_filter(items: list[dict[str, Any]],
                          fuzzy: bool = True) -> Callable[[str], bool] | None:
    """Предикат «это слово — ошибка распознавания»: словарь (точно или по звучанию)
    заменил бы его другим термином. Такие слова не идут ни в подсказку Whisper, ни в
    термины ИИ (см. textproc.misrecognition_filter). Пустой словарь → None."""
    if not fuzzy:
        return textproc.misrecognition_filter(items)
    key = textproc._dict_key(items or [])
    if not key:
        return None
    return _check(key)


@lru_cache(maxsize=8)
def _check(key: textproc.DictKey) -> Callable[[str], bool]:
    memo: dict[str, bool] = {}

    def rewritten(word: str) -> bool:
        hit = memo.get(word)
        if hit is None:
            hit = bool(word) and fold(_apply_key(word, key, True)[0]) != fold(
                unicodedata.normalize("NFC", word))
            if len(memo) < _MEMO and spell.ready():
                memo[word] = hit                  # без словарей Windows — не запоминаем
        return hit
    return rewritten
