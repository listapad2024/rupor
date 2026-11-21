// Заметки к встрече (Markdown от ядра) → блоки для окна перевода. Только то, что бывает в
// заметках: заголовки, списки (и задачи «- [ ] кто — что — срок»), абзацы, черта, **жирный**,
// *курсив*, `код` и время строк «ЧЧ:ММ:СС» (в окне — кнопка к строке в расшифровке). Разметка
// разбирается в данные, а рисует их React: HTML из ответа модели в окно не попадает.

export type Inline =
  | { k: "text"; v: string }
  | { k: "b"; c: Inline[] }
  | { k: "i"; c: Inline[] }
  | { k: "code"; v: string }
  | { k: "time"; v: string };

export interface ListItem {
  /** Задача: true — отмечена («- [x]»), false — нет («- [ ]»); не задача — null. */
  check: boolean | null;
  /** Вложенность (0 — верхний уровень). */
  depth: number;
  c: Inline[];
}

export type Block =
  | { k: "h"; level: 1 | 2 | 3; c: Inline[] }
  | { k: "list"; items: ListItem[] }
  | { k: "p"; c: Inline[] }
  | { k: "hr" };

const TIME_RE = /(?<![\d:])(?:[01]?\d|2[0-3]):[0-5]\d:[0-5]\d(?![\d:])/g;
const INLINE_RE = /`([^`\n]+)`|\*\*([^*\n]+?)\*\*|__([^_\n]+?)__|(?<![\w*])\*(?![\s*])([^*\n]+?)\*(?![\w*])/g;
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const ITEM_RE = /^(\s*)(?:[-*+•]|\d{1,3}[.)])\s+(?:\[([ xX])\]\s+)?(.*)$/;
const HR_RE = /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/;

function withTimes(s: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of s.matchAll(TIME_RE)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ k: "text", v: s.slice(last, at) });
    out.push({ k: "time", v: m[0] });
    last = at + m[0].length;
  }
  if (last < s.length) out.push({ k: "text", v: s.slice(last) });
  return out;
}

/** Строка → куски: жирный, курсив, код, время строк, остальное — текст. */
export function parseInline(s: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of s.matchAll(INLINE_RE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(...withTimes(s.slice(last, at)));
    if (m[1] !== undefined) out.push({ k: "code", v: m[1] });
    else if (m[2] !== undefined || m[3] !== undefined) out.push({ k: "b", c: parseInline(m[2] ?? m[3] ?? "") });
    else out.push({ k: "i", c: parseInline(m[4] ?? "") });
    last = at + m[0].length;
  }
  if (last < s.length) out.push(...withTimes(s.slice(last)));
  return out;
}

/** Заметки → блоки. Строки абзаца склеиваются пробелом; строка с отступом после пункта — его продолжение. */
export function parseNotes(md: string): Block[] {
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: { k: "list"; items: ListItem[] } | null = null;
  const flushPara = () => {
    if (para.length) blocks.push({ k: "p", c: parseInline(para.join(" ")) });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push(list);
    list = null;
  };
  for (const raw of md.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim() || /^\s*```/.test(line)) {
      flushPara();
      flushList();
      continue;
    }
    const h = HEADING_RE.exec(line);
    if (h) {
      flushPara();
      flushList();
      const level = Math.min(3, h[1]?.length ?? 1) as 1 | 2 | 3;
      blocks.push({ k: "h", level, c: parseInline(h[2] ?? "") });
      continue;
    }
    if (HR_RE.test(line)) {
      flushPara();
      flushList();
      blocks.push({ k: "hr" });
      continue;
    }
    const it = ITEM_RE.exec(line);
    if (it) {
      flushPara();
      const cur: { k: "list"; items: ListItem[] } = list ?? { k: "list", items: [] };
      list = cur;
      const indent = (it[1] ?? "").replace(/\t/g, "  ").length;
      const mark = it[2];
      cur.items.push({
        check: mark === undefined ? null : mark !== " ",
        depth: Math.min(3, Math.floor(indent / 2)),
        c: parseInline(it[3] ?? ""),
      });
      continue;
    }
    const open: { k: "list"; items: ListItem[] } | null = list;
    const item = open ? open.items[open.items.length - 1] : undefined;
    if (item && /^\s{2,}\S/.test(raw)) {
      item.c.push({ k: "text", v: " " }, ...parseInline(line.trim()));
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flushPara();
  flushList();
  return blocks;
}

/** Сколько задач в заметках и сколько из них отмечено. */
export function countTasks(blocks: readonly Block[]): { total: number; done: number } {
  let total = 0;
  let done = 0;
  for (const b of blocks) {
    if (b.k !== "list") continue;
    for (const it of b.items) {
      if (it.check === null) continue;
      total++;
      if (it.check) done++;
    }
  }
  return { total, done };
}
