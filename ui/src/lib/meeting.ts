// Перевод встречи: общее для окна перевода (meeting.html), главного окна и пилюли.
import { useEffect, useState } from "react";
import { getLocale, t } from "../i18n";
import { useEvent } from "./hooks";
import { meetingWindowShow } from "./tauri";
import type {
  AudioSource,
  MeetingLang,
  MeetingLine,
  MeetingLineStatus,
  MeetingStatus,
  MeetingSuggest,
} from "./types";
import { call } from "./ws";

/** Языки перевода — как валидатор ядра (meeting_target_lang / meeting_source_lang). */
export const MEETING_LANGS: readonly MeetingLang[] = [
  "ru",
  "en",
  "de",
  "fr",
  "es",
  "it",
  "pt",
  "pl",
  "uk",
  "zh",
  "ja",
  "ko",
  "tr",
];

/** Раздел настроек «Встречи». */
export const MEETING_SETTINGS_ROUTE = "settings/meeting";

/** meeting_start ждёт загрузки модели, если её ещё нет (как перед диктовкой). */
const START_TIMEOUT_MS = 120_000;

export function isMeetingLang(code: string): code is MeetingLang {
  return (MEETING_LANGS as readonly string[]).includes(code);
}

/** Название языка на языке интерфейса; незнакомый код Whisper — как есть, заглавными. */
export function meetingLangName(code: string): string {
  return isMeetingLang(code) ? t(`meeting.langs.${code}`) : code.toUpperCase();
}

/**
 * Название языка внутри фразы («Перевод на {lang}»): по-русски — со строчной («на
 * английский»), по-английски названия языков пишутся с заглавной.
 */
export function meetingLangInSentence(code: string): string {
  const name = meetingLangName(code);
  return getLocale() === "ru" && isMeetingLang(code) ? name.toLocaleLowerCase("ru-RU") : name;
}

/** Статус, пока ядро его не прислало (или ядро без перевода встречи). */
export const IDLE_MEETING: MeetingStatus = {
  running: false,
  source: null,
  started_at: null,
  lines: 0,
  level: 0,
  state: "idle",
  error: null,
  translate: true,
  target_lang: "ru",
  file: null,
  title: null,
  latency: { asr_ms: null, first_token_ms: null, done_ms: null },
  hint: null,
  ending_in_s: null,
  notes: null,
};

// ——— Заметки к встрече ———

/** Время строки «ЧЧ:ММ:СС» → секунды от полуночи; не время — null. */
export function clockSeconds(ts: string): number | null {
  const m = /^(\d{1,2}):(\d{2}):(\d{2})$/.exec(ts.trim());
  if (!m) return null;
  const [h, mi, se] = [Number(m[1]), Number(m[2]), Number(m[3])];
  return h < 24 && mi < 60 && se < 60 ? h * 3600 + mi * 60 + se : null;
}

/**
 * Строка расшифровки по времени из заметок («14:05:09»): с тем же временем, а нет — ближайшая
 * до него (заметки ссылаются на начало фразы, модель может округлить); раньше всех — первая.
 */
export function lineAt(lines: readonly MeetingLine[], ts: string): MeetingLine | null {
  const want = clockSeconds(ts);
  if (want === null || !lines.length) return null;
  let best: MeetingLine | null = null;
  let bestAt = -1;
  for (const l of lines) {
    const at = clockSeconds(l.ts);
    if (at === null) continue;
    if (at === want) return l;
    if (at < want && at >= bestAt) {
      best = l;
      bestAt = at;
    }
  }
  return best ?? lines[0] ?? null;
}

/**
 * Включить перевод встречи (sourceId: "proc:<pid>" | "system"; без него — ядро выберет
 * встречу само) и показать окно перевода. Окно показывается, даже если старт не удался:
 * там видно, почему.
 */
export async function startMeeting(sourceId?: string): Promise<MeetingStatus> {
  const shown = meetingWindowShow().catch((e: unknown) => console.warn("meeting_window_show", e));
  try {
    return await call("meeting_start", sourceId ? { source_id: sourceId } : {}, { timeoutMs: START_TIMEOUT_MS });
  } finally {
    await shown;
  }
}

/** Предложение встречи без ответа пропадает само через столько (как у оболочки, meeting.rs). */
export const SUGGEST_TTL_MS = 45_000;

/** Источник «весь звук компьютера». */
export const SYSTEM_SOURCE = "system";

/**
 * Звук одной программы захватить нельзя (Windows старше Windows 11 / Server 2022): ядро
 * говорит это полем MeetingStatus.loopback_available или available=false у каждой программы
 * в списке источников. Ядро полей не присылает — можно.
 */
export function loopbackOff(
  status: MeetingStatus | null | undefined,
  sources: readonly AudioSource[] | null | undefined,
): boolean {
  if (status?.loopback_available === false) return true;
  const procs = (sources ?? []).filter((s) => s.kind === "process");
  return procs.length > 0 && procs.every((s) => s.available === false);
}

/** Источник можно выбрать: весь звук — всегда, программу — если её звук можно захватить. */
export function sourceUsable(s: AudioSource, off: boolean): boolean {
  return s.kind === "system" || (!off && s.available !== false);
}

/**
 * Что стоит в выборе источника, пока сеанс не идёт: выбранное (picked: "auto" или id
 * источника), если его можно взять; иначе «Автоматически», а там, где программы захватить
 * нельзя (off), — «Весь звук компьютера».
 */
export function pickSource(picked: string, sources: readonly AudioSource[], off: boolean): string {
  const fallback = off ? SYSTEM_SOURCE : "auto";
  if (picked === "auto") return fallback;
  const s = sources.find((x) => x.id === picked);
  return s && !sourceUsable(s, off) ? fallback : picked;
}

/**
 * «Начать расшифровку» в предложении встречи: окно перевода — сразу в полный вид (showWindow;
 * вне Tauri окно перевода — та же вкладка, её не открывают заново), ядру — ответ start (ядро
 * само начинает перевод с источником предложения и отвечает MeetingStatus). Предложение ядро
 * уже забыло (устарело, перезапуск — ошибка) или ответило не идущей встречей — meeting_start
 * с источником предложения: встреча всё равно начинается. Звук одной программы захватить
 * нельзя (systemOnly, поле ядра) — источник предложения заменяет весь звук компьютера.
 */
export async function startSuggested(
  s: MeetingSuggest,
  { showWindow = true, systemOnly = false }: { showWindow?: boolean; systemOnly?: boolean } = {},
): Promise<MeetingStatus> {
  const shown = showWindow
    ? meetingWindowShow().catch((e: unknown) => console.warn("meeting_window_show", e))
    : Promise.resolve();
  try {
    let r: MeetingStatus | null = null;
    try {
      r = await call("meeting_suggest_answer", { id: s.id, answer: "start" }, { timeoutMs: START_TIMEOUT_MS });
    } catch (e) {
      console.warn("meeting_suggest_answer", e);
    }
    const off = systemOnly || r?.loopback_available === false || !sourceUsable(s.source, false);
    const want = off ? SYSTEM_SOURCE : s.source.id;
    if (r?.running && (r.source?.id === s.source.id || r.source?.id === want)) return r;
    return await call("meeting_start", { source_id: want }, { timeoutMs: START_TIMEOUT_MS });
  } finally {
    await shown;
  }
}

/**
 * Колесо вверх отпускает низ ленты, только если ей есть куда прокрутиться (переполнение больше
 * slack): у короткой ленты прокрутки нет, событие scroll не придёт — и снова «прилипнуть» к
 * низу было бы нечем.
 */
export function wheelUnpins(deltaY: number, scrollHeight: number, clientHeight: number, slack: number): boolean {
  return deltaY < 0 && scrollHeight - clientHeight > slack;
}

const CJK = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;

/**
 * Нужен ли пробел между подтверждённым текстом и хвостом (в китайском и японском пробела между
 * словами нет).
 */
export function needsSpace(text: string, partial: string): boolean {
  const a = text.slice(-1);
  const b = partial.slice(0, 1);
  if (/\s/.test(a) || /\s/.test(b)) return false;
  if (CJK.test(a) || CJK.test(b)) return false;
  // хвост начинается со знака препинания — «word» + «,»
  return !/^[,.;:!?…)\]»”%]/.test(b);
}

/**
 * Как текст сменился: [то, что осталось как было, новое]. Дописали в конец — новое только
 * дописанное; поменяли (уточнённый перевод, финальное распознавание) — с начала первого
 * изменившегося слова (без пробелов — CJK — весь текст).
 */
export function splitGrowth(prev: string, next: string): [string, string] {
  if (next.startsWith(prev)) return [prev, next.slice(prev.length)];
  const n = Math.min(prev.length, next.length);
  let i = 0;
  while (i < n && prev[i] === next[i]) i++;
  const cut = i > 0 ? next.lastIndexOf(" ", i - 1) + 1 : 0;
  return [next.slice(0, cut), next.slice(cut)];
}

/**
 * Строка выброшена ядром: фраза оказалась шумом или «субтитрами» Whisper (final без текста и
 * хвоста). В списке она остаётся — устаревший снимок из meeting_lines не вернёт её назад, —
 * но не рисуется и не считается.
 */
export function isDroppedLine(l: MeetingLine): boolean {
  return l.final && !l.text && !l.partial;
}

/**
 * Строка в список (по возрастанию id): новая — в конец, обновление — на место. Возвращает
 * новый массив (или тот же, если строка не изменилась).
 */
export function upsertLine(lines: MeetingLine[], line: MeetingLine): MeetingLine[] {
  const last = lines[lines.length - 1];
  if (!last || line.id > last.id) return [...lines, line];
  let lo = 0;
  let hi = lines.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const cur = lines[mid];
    if (!cur) break;
    if (cur.id === line.id) {
      if (sameLine(cur, line)) return lines;
      const next = lines.slice();
      next[mid] = line;
      return next;
    }
    if (cur.id < line.id) lo = mid + 1;
    else hi = mid - 1;
  }
  // строки с таким id нет (пришла раньше пачки из meeting_lines) — вставить по порядку
  const next = lines.slice();
  next.splice(lo, 0, line);
  return next;
}

function sameLine(a: MeetingLine, b: MeetingLine): boolean {
  return (
    a.status === b.status &&
    a.text === b.text &&
    a.partial === b.partial &&
    a.final === b.final &&
    a.refined === b.refined &&
    a.translation === b.translation &&
    a.error === b.error &&
    a.lang === b.lang
  );
}

/** Порядок статусов строки: дальше по списку — строка «продвинулась». */
const STATUS_RANK: Record<MeetingLineStatus, number> = { pending: 0, streaming: 1, done: 2, original: 2, error: 2 };

/**
 * Насколько строка продвинулась (сравнение — лексикографически): уточнена, кончилась, статус
 * перевода, длина подтверждённого текста, длина перевода. Пока фраза идёт, текст и перевод
 * только растут; в конце финальное распознавание и уточнение могут их сократить, но тогда
 * растут final и refined.
 */
export function lineProgress(l: MeetingLine): number[] {
  return [l.refined ? 1 : 0, l.final ? 1 : 0, STATUS_RANK[l.status] ?? 0, l.text.length, (l.translation ?? "").length];
}

function behind(a: MeetingLine, b: MeetingLine): boolean {
  const pa = lineProgress(a);
  const pb = lineProgress(b);
  for (let i = 0; i < pa.length; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x < y;
  }
  return false;
}

/**
 * Слить пачку строк из meeting_lines со строками, пришедшими событиями. Событие бывает новее
 * ответа: строку, которая уже продвинулась дальше (больше слов, начался или пришёл перевод,
 * фраза кончилась, перевод уточнён), ответ со старым снимком не откатывает.
 */
export function mergeFetchedLines(lines: MeetingLine[], batch: MeetingLine[]): MeetingLine[] {
  if (!lines.length) return batch.slice().sort((a, b) => a.id - b.id);
  const known = new Map(lines.map((l) => [l.id, l]));
  let out = lines;
  for (const l of batch) {
    const cur = known.get(l.id);
    if (cur && behind(l, cur)) continue;
    out = upsertLine(out, l);
  }
  return out;
}

/** Ключ сеанса: строки другого сеанса (новый старт) не смешиваются. */
export function sessionKey(s: MeetingStatus | null | undefined): string {
  return s?.started_at ?? "";
}

/** Статусы различаются не только уровнем звука (level приходит 4 раза в секунду). */
function sameButLevel(a: MeetingStatus, b: MeetingStatus): boolean {
  return (
    a.running === b.running &&
    a.state === b.state &&
    a.lines === b.lines &&
    a.started_at === b.started_at &&
    a.error === b.error &&
    a.translate === b.translate &&
    a.target_lang === b.target_lang &&
    a.file === b.file &&
    a.title === b.title &&
    a.notes?.status === b.notes?.status &&
    a.notes?.generated_at === b.notes?.generated_at &&
    a.source?.id === b.source?.id
  );
}

/**
 * Перевод встречи для главного окна (Главная, настройки): fromState — State.meeting из
 * hello/get_state (useCore), дальше — события meeting. Изменения одного уровня звука окно не
 * перерисовывают.
 */
export function useMeetingStatus(fromState: MeetingStatus | null | undefined): MeetingStatus | null {
  const [status, setStatus] = useState<MeetingStatus | null>(fromState ?? null);
  // новый снимок State (подключение, переподключение к новому ядру) — он и действует
  useEffect(() => {
    if (fromState) setStatus((prev) => (prev && sameButLevel(prev, fromState) ? prev : fromState));
  }, [fromState]);
  useEvent("meeting", (m) => setStatus((prev) => (prev && sameButLevel(prev, m) ? prev : m)));
  return status;
}
