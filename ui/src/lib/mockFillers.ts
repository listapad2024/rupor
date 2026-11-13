// Демо-режим: статистика слов-паразитов по сырому тексту истории демо-ядра (упрощённо:
// без сведения «э-э-э» → «эээ» и прочих тонкостей ядра — только чтобы экран «Речь» жил).
import type { FillerConfig, FillerStats, FillerSuggestion, HistoryItem } from "./types";

let phrases: string[] | null = null;
let enabled = true;
let resetAt = 0;

const fold = (s: string) => s.toLocaleLowerCase().replace(/\u0451/g, "\u0435");
const words = (s: string) => fold(s).match(/[\p{L}\p{N}]+(?:[-'][\p{L}\p{N}]+)*/gu) ?? [];

function cleanList(xs: string[]): string[] {
  const out: string[] = [];
  for (const x of xs) {
    const p = words(x).join(" ");
    if (p && !out.includes(p)) out.push(p);
  }
  return out;
}

function count(text: string, list: string[]): [Map<string, number>, number] {
  const toks = words(text);
  const seqs = list.map((p) => p.split(" ")).sort((a, b) => b.length - a.length);
  const found = new Map<string, number>();
  for (let i = 0; i < toks.length; ) {
    const hit = seqs.find((s) => s.every((w, k) => toks[i + k] === w));
    if (hit) {
      const p = hit.join(" ");
      found.set(p, (found.get(p) ?? 0) + 1);
      i += hit.length;
    } else i++;
  }
  return [found, toks.length];
}

const iso = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const per100 = (n: number, w: number) => (w > 0 ? Math.round((n * 10000) / w) / 100 : 0);

function stats(history: HistoryItem[], defaults: string[], days: number): FillerStats {
  const list = phrases ?? cleanList(defaults);
  const now = new Date();
  const dayMs = 86400000;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const first = days ? today - (days - 1) * dayMs : Math.min(today, ...history.map((h) => h.ts * 1000));
  const prevFirst = first - days * dayMs;
  const cur = new Map<string, number>();
  const prev = new Map<string, number>();
  const byDay = new Map<string, { w: number; f: number }>();
  let w = 0;
  let pw = 0;
  let n = 0;
  for (const h of history) {
    const t = h.ts * 1000;
    if (t < resetAt || h.target === "test") continue;
    const [found, cnt] = count(h.raw, list);
    const fsum = [...found.values()].reduce((a, b) => a + b, 0);
    if (t >= first) {
      w += cnt;
      n++;
      for (const [p, c] of found) cur.set(p, (cur.get(p) ?? 0) + c);
      const k = iso(new Date(t));
      const d = byDay.get(k) ?? { w: 0, f: 0 };
      byDay.set(k, { w: d.w + cnt, f: d.f + fsum });
    } else if (days && t >= prevFirst) {
      pw += cnt;
      for (const [p, c] of found) prev.set(p, (prev.get(p) ?? 0) + c);
    }
  }
  const total = [...cur.values()].reduce((a, b) => a + b, 0);
  const ptotal = [...prev.values()].reduce((a, b) => a + b, 0);
  const daily: FillerStats["daily"] = [];
  for (let t = Math.max(first, today - 89 * dayMs); t <= today; t += dayMs) {
    const k = iso(new Date(t));
    const d = byDay.get(k) ?? { w: 0, f: 0 };
    daily.push({ date: k, words: d.w, fillers: d.f, per100: per100(d.f, d.w) });
  }
  return {
    enabled,
    days,
    words: w,
    dictations: n,
    fillers: total,
    per100: per100(total, w),
    prev_per100: days && pw ? per100(ptotal, pw) : null,
    top: [...cur.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([phrase, c]) => ({
        phrase,
        count: c,
        per100: per100(c, w),
        prev_per100: days && pw ? per100(prev.get(phrase) ?? 0, pw) : null,
      })),
    daily,
    since: history.length ? iso(new Date(Math.min(...history.map((h) => h.ts * 1000)))) : null,
  };
}

function config(defaults: string[]): FillerConfig {
  return { enabled, phrases: phrases ?? cleanList(defaults), defaults: cleanList(defaults) };
}

export function fillersMock(cmd: string, args: unknown, history: HistoryItem[], defaults: string[]): unknown {
  const a = (args ?? {}) as { days?: number; phrases?: string[]; enabled?: boolean };
  switch (cmd) {
    case "fillers_stats":
      return stats(history, defaults, a.days ?? 30);
    case "fillers_config":
      return config(defaults);
    case "fillers_config_set":
      if (a.phrases) phrases = cleanList(a.phrases);
      if (typeof a.enabled === "boolean") enabled = a.enabled;
      return config(defaults);
    case "fillers_defaults":
      phrases = null;
      return config(defaults);
    case "fillers_rebuild":
      resetAt = 0;
      return { days: 30, dictations: history.length };
    case "fillers_reset":
      resetAt = Date.now();
      return null;
    case "fillers_ai_suggest": {
      const list = phrases ?? cleanList(defaults);
      const seen = new Map<string, number>();
      for (const h of history) {
        const ws = words(h.raw);
        for (let i = 0; i + 1 < ws.length; i++) {
          const p = `${ws[i]} ${ws[i + 1]}`;
          seen.set(p, (seen.get(p) ?? 0) + 1);
        }
      }
      const items: FillerSuggestion[] = [...seen.entries()]
        .filter(([p, c]) => c >= 3 && !list.includes(p))
        .sort((x, y) => y[1] - x[1])
        .slice(0, 5)
        .map(([phrase, c]) => ({ phrase, count: c, example: phrase }));
      return { items, sampled: Math.min(80, history.length) };
    }
  }
  return undefined;
}
