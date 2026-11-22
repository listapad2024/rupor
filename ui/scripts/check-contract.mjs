// Живая проверка связки ядро ↔ интерфейс без Tauri.
//
//   node scripts/check-contract.mjs --port 18765 --token <токен> [--no-mic] [--no-ai-usage] [--quit] [--types файл.ts]
//
// Подключается к уже запущенному ядру (python -m rupor --sidecar --no-hook --no-mutex …),
// проходит те же сценарии, что и окна интерфейса (hello → get_state → stats_get →
// history_list → расход ИИ → словарь (и его обучение на правках)/сниппеты → импорт/экспорт →
// правила приложений и по названию окна → рассылка в RDP (белый список) → частые слова →
// set_settings (в т.ч. learn_mode, dict_fuzzy, duck_mode/duck_level, language/dictation_language) …),
// и сверяет КАЖДЫЙ ответ и КАЖДОЕ событие с типами из src/lib/types.ts. Типы разбираются
// TypeScript compiler API — отдельной схемы, которая может разойтись с types.ts, нет.
// Поля, которые ядро присылает, а в types.ts их нет, выводятся предупреждением.
//
// Безопасность: команды вставки (last_insert, history_reinsert с настоящей записью) не
// вызываются, ключи в Credential Manager не пишутся и не удаляются (ai_key_set/import_env/delete),
// open_logs (окно проводника) не вызывается, заголовки окон, слова словаря частот и ключи
// RDP-сессий не печатаются (только количество), токен не выводится. ai_balance с ключом
// не вызывается (это запрос в OpenRouter) — в итоге он значится непроверенным.
// Расход ИИ без сети: на время шага ai_provider=ollama с адресом локальной заглушки Ollama
// (node:http, 127.0.0.1) и ai_test — удачный и с ошибкой → событие ai_usage и прирост
// ai_usage_stats. Две проверочные записи kind=test по $0 остаются в расходе (удалить их
// через WS нельзя); --no-ai-usage — пропустить шаг.
// duck_mode/duck_level меняются только вне записи (ядро приглушает звук лишь во время
// записи), так что громкость других приложений проверка не трогает.
// Перевод встречи: звук не захватывается — meeting_start вызывается только с заведомо неверным
// источником (ошибка), и только если сеанс не идёт (идущий не трогаем, meeting_stop тогда тоже не
// зовётся); meeting_copy — только с неверным what (ошибка, буфер обмена не трогается),
// meeting_open_folder (окно проводника) не вызывается; заметки (meeting_notes — вызов ИИ) —
// только без встречи (ошибка), meeting_notes_get — как есть;
// названия приложений и заголовки окон источников не печатаются (только количество).
// Предложение встречи (meeting_suggest_answer) — только с неизвестным id и ответом later:
// встреча не начинается, meeting_ignore_apps не меняется. «Не останавливать»
// (meeting_autostop_cancel) — только без идущего сеанса (тогда это просто статус).
// Папка моделей: model_dir_set вызывается только так, что ничего не переносится и не
// качается (та же папка, неверные пути — ошибки; смена папки во временную и обратно — только
// на ядре первого запуска без модели, которое ждёт setup_download); пути (в них имя
// пользователя) не печатаются.
// model_recommend может спросить у Hugging Face размеры моделей (открытые метаданные, бюджет
// ядра ~5 с).
// Остальные изменения данных делаются на проверочных записях и откатываются в конце.
import { readFileSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const CYR = /[А-Яа-яЁё]/;
// Язык интерфейса ядра (State.ui_language): тексты ошибок, notice и стилей — на нём.
// Шаг «language» переключает его на en и ru и возвращает как было.
let UI_LANG = "ru";
/** Текст для человека на языке интерфейса: ru — с кириллицей, en — латиница без кириллицы. */
function humanText(s) {
  if (typeof s !== "string" || !s.trim()) return false;
  return UI_LANG === "en" ? /[A-Za-z]/.test(s) && !CYR.test(s) : CYR.test(s);
}

// ——— аргументы ———

function arg(name, def = null) {
  const i = process.argv.indexOf(name);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}
const PORT = Number(arg("--port", process.env.RUPOR_WS_PORT ?? ""));
const TOKEN = arg("--token", process.env.RUPOR_WS_TOKEN ?? "");
const MIC = !process.argv.includes("--no-mic");
const QUIT = process.argv.includes("--quit");
const AI_USAGE = !process.argv.includes("--no-ai-usage");
// --types — другой файл типов (например, чтобы убедиться, что проверка ловит расхождения)
const TYPES = arg("--types") ?? fileURLToPath(new URL("../src/lib/types.ts", import.meta.url));
if (!PORT || !TOKEN) {
  console.error("Нужны --port и --token (или RUPOR_WS_PORT / RUPOR_WS_TOKEN)");
  process.exit(2);
}

// ——— типы из types.ts ———

const sf = ts.createSourceFile(TYPES, readFileSync(TYPES, "utf-8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const decls = new Map();
for (const st of sf.statements) {
  if (ts.isInterfaceDeclaration(st) || ts.isTypeAliasDeclaration(st)) decls.set(st.name.text, st);
}

function propName(m) {
  const n = m.name;
  return ts.isIdentifier(n) || ts.isStringLiteral(n) ? n.text : n.getText(sf);
}

function membersOf(name) {
  const d = decls.get(name);
  if (!d) throw new Error(`В types.ts нет типа ${name}`);
  if (ts.isInterfaceDeclaration(d)) return d.members;
  if (ts.isTypeLiteralNode(d.type)) return d.type.members;
  throw new Error(`${name} — не объектный тип`);
}

function memberType(members, key) {
  const m = members.find((x) => ts.isPropertySignature(x) && propName(x) === key);
  return m?.type ?? null;
}

const commandMap = membersOf("CommandMap");
const eventMap = membersOf("EventMap");

function resultType(cmd) {
  const t = memberType(commandMap, cmd);
  if (!t || !ts.isTypeLiteralNode(t)) throw new Error(`Команды ${cmd} нет в CommandMap`);
  return memberType(t.members, "result");
}

const isObj = (v) => typeof v === "object" && v !== null && !Array.isArray(v);
const todayLocal = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const lower = (x) => String(x).toLowerCase();
/** Пути Windows равны без учёта регистра, вида разделителя и «\» в конце. */
const samePath = (a, b) => {
  const norm = (p) => String(p).trim().replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
  return norm(a) === norm(b);
};
/** Абсолютный путь Windows: буква диска или UNC. */
const absPath = (p) => typeof p === "string" && (/^[A-Za-z]:[\\/]/.test(p) || /^\\\\[^\\]+\\[^\\]+/.test(p));
const short = (v) => {
  const s = JSON.stringify(v);
  return s === undefined ? "undefined" : s.length > 60 ? `${s.slice(0, 57)}…` : s;
};

/** Проверить значение по узлу типа. Возвращает список ошибок; лишние поля — в extra. */
function check(v, node, path, extra) {
  if (!node) return [];
  if (ts.isInterfaceDeclaration(node)) return checkMembers(v, node.members, path, extra, false);
  if (ts.isTypeAliasDeclaration(node)) return check(v, node.type, path, extra);
  if (ts.isParenthesizedTypeNode(node)) return check(v, node.type, path, extra);
  if (ts.isTypeLiteralNode(node)) return checkMembers(v, node.members, path, extra, false);
  if (ts.isUnionTypeNode(node)) {
    for (const t of node.types) {
      const ex = new Set();
      const errs = check(v, t, path, ex);
      if (!errs.length) {
        for (const e of ex) extra.add(e);
        return [];
      }
    }
    return [`${path}: ${short(v)} не подходит под ${node.getText(sf)}`];
  }
  if (ts.isLiteralTypeNode(node)) {
    const lit = node.literal;
    let ok;
    if (lit.kind === ts.SyntaxKind.NullKeyword) ok = v === null;
    else if (lit.kind === ts.SyntaxKind.TrueKeyword) ok = v === true;
    else if (lit.kind === ts.SyntaxKind.FalseKeyword) ok = v === false;
    else if (ts.isStringLiteral(lit)) ok = v === lit.text;
    else if (ts.isNumericLiteral(lit)) ok = v === Number(lit.text);
    else ok = false;
    return ok ? [] : [`${path}: ожидалось ${node.getText(sf)}, пришло ${short(v)}`];
  }
  if (ts.isArrayTypeNode(node)) {
    if (!Array.isArray(v)) return [`${path}: ожидался массив, пришло ${short(v)}`];
    return v.flatMap((x) => check(x, node.elementType, `${path}[]`, extra));
  }
  if (ts.isTupleTypeNode(node)) {
    if (!Array.isArray(v) || v.length !== node.elements.length) {
      return [`${path}: ожидался кортеж ${node.getText(sf)}, пришло ${short(v)}`];
    }
    return node.elements.flatMap((t, i) => check(v[i], t, `${path}[${i}]`, extra));
  }
  if (ts.isTypeReferenceNode(node)) {
    const name = node.typeName.getText(sf);
    const targs = node.typeArguments ?? [];
    if (name === "Record") {
      if (!isObj(v)) return [`${path}: ожидался объект, пришло ${short(v)}`];
      return Object.entries(v).flatMap(([k, x]) => check(x, targs[1], `${path}.${k}`, extra));
    }
    if (name === "Array") {
      if (!Array.isArray(v)) return [`${path}: ожидался массив, пришло ${short(v)}`];
      return v.flatMap((x) => check(x, targs[0], `${path}[]`, extra));
    }
    if (name === "Partial") {
      const inner = targs[0];
      const ref = inner && ts.isTypeReferenceNode(inner) ? inner.typeName.getText(sf) : null;
      if (!ref) return [`${path}: не умею проверять ${node.getText(sf)}`];
      return checkMembers(v, membersOf(ref), path, extra, true);
    }
    const d = decls.get(name);
    if (!d) return [`${path}: неизвестный тип ${name}`];
    return check(v, d, path, extra);
  }
  switch (node.kind) {
    case ts.SyntaxKind.StringKeyword:
      return typeof v === "string" ? [] : [`${path}: ожидалась строка, пришло ${short(v)}`];
    case ts.SyntaxKind.NumberKeyword:
      return typeof v === "number" && Number.isFinite(v) ? [] : [`${path}: ожидалось число, пришло ${short(v)}`];
    case ts.SyntaxKind.BooleanKeyword:
      return typeof v === "boolean" ? [] : [`${path}: ожидалось true/false, пришло ${short(v)}`];
    case ts.SyntaxKind.NullKeyword:
      return v === null ? [] : [`${path}: ожидался null, пришло ${short(v)}`];
    case ts.SyntaxKind.VoidKeyword:
    case ts.SyntaxKind.UndefinedKeyword:
      return v === undefined || v === null ? [] : [`${path}: ожидалось пусто, пришло ${short(v)}`];
    case ts.SyntaxKind.AnyKeyword:
    case ts.SyntaxKind.UnknownKeyword:
      return [];
    default:
      return [`${path}: не умею проверять ${node.getText(sf)}`];
  }
}

function checkMembers(v, members, path, extra, partial) {
  if (!isObj(v)) return [`${path}: ожидался объект, пришло ${short(v)}`];
  const errs = [];
  const known = new Set();
  for (const m of members) {
    if (!ts.isPropertySignature(m)) continue;
    const name = propName(m);
    known.add(name);
    const optional = partial || Boolean(m.questionToken);
    if (!(name in v) || v[name] === undefined) {
      if (!optional) errs.push(`${path}.${name}: поля нет`);
      continue;
    }
    errs.push(...check(v[name], m.type, `${path}.${name}`, extra));
  }
  for (const k of Object.keys(v)) if (!known.has(k)) extra.add(`${path}.${k}`);
  return errs;
}

// ——— клиент ———

class Client {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    this.waiters = new Set();
    this.closeCode = null;
    this.closed = new Promise((res) => {
      this.ws.addEventListener("close", (ev) => {
        this.closeCode = ev.code;
        for (const p of this.pending.values()) p.reject(new Error(`соединение закрыто (${ev.code})`));
        this.pending.clear();
        res(ev.code);
      });
    });
    this.ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(String(ev.data));
      if (typeof msg.id === "number") {
        const p = this.pending.get(msg.id);
        if (p) {
          this.pending.delete(msg.id);
          clearTimeout(p.timer);
          p.resolve(msg);
        }
        return;
      }
      if (typeof msg.event === "string") {
        this.events.push({ event: msg.event, data: msg.data, t: Date.now() });
        for (const w of [...this.waiters]) w();
      }
    });
  }

  opened() {
    return new Promise((res, rej) => {
      this.ws.addEventListener("open", () => res(), { once: true });
      this.ws.addEventListener("error", () => rej(new Error("не удалось подключиться")), { once: true });
    });
  }

  /** Ответ как есть: {id, ok, result} или {id, ok: false, error}. */
  raw(cmd, args, timeoutMs = 30_000) {
    const id = this.nextId++;
    const msg = { id, cmd };
    if (args !== undefined) msg.args = args;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${cmd}: нет ответа за ${timeoutMs} мс`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify(msg));
    });
  }

  /** Дождаться события (среди пришедших после since). */
  waitEvent(name, since, timeoutMs = 5000, pred = () => true) {
    const find = () => this.events.find((e) => e.t >= since && e.event === name && pred(e.data));
    const hit = find();
    if (hit) return Promise.resolve(hit.data);
    return new Promise((resolve, reject) => {
      const w = () => {
        const e = find();
        if (e) {
          this.waiters.delete(w);
          clearTimeout(timer);
          resolve(e.data);
        }
      };
      const timer = setTimeout(() => {
        this.waiters.delete(w);
        reject(new Error(`событие ${name} не пришло за ${timeoutMs} мс`));
      }, timeoutMs);
      this.waiters.add(w);
    });
  }

  close() {
    this.ws.close();
  }
}

// ——— отчёт ———

const results = [];
const extras = new Set();
const called = new Set();
/** Части контракта, которые в этом прогоне не сверены с живым ядром (в итог). */
const unverified = [];

function assert(cond, text) {
  if (!cond) throw new Error(text);
}

async function step(name, fn) {
  const t0 = Date.now();
  try {
    const note = await fn();
    results.push({ name, ok: true, ms: Date.now() - t0, note });
    console.log(`  ok   ${name}${note ? ` — ${note}` : ""} (${Date.now() - t0} мс)`);
  } catch (e) {
    results.push({ name, ok: false, ms: Date.now() - t0, note: e.message });
    console.log(`  FAIL ${name} — ${e.message}`);
  }
}

let c;

/** Вызвать команду, проверить ok:true и тип result по CommandMap. */
async function call(cmd, args, timeoutMs) {
  called.add(cmd);
  const r = await c.raw(cmd, args, timeoutMs);
  assert(r.ok === true, `${cmd}: ok=false — ${r.error}`);
  const errs = check(r.result, resultType(cmd), `${cmd}.result`, extras);
  assert(!errs.length, errs.slice(0, 4).join("; "));
  return r.result;
}

/** Событие — по EventMap (лишние поля — в итог). */
function checkEvent(name, data) {
  const errs = check(data, memberType(eventMap, name), `event.${name}`, extras);
  assert(!errs.length, errs.slice(0, 4).join("; "));
}

/** Команда должна вернуть ошибку на языке интерфейса (ok:false, error — текст для человека). */
async function callErr(cmd, args) {
  called.add(cmd);
  const r = await c.raw(cmd, args);
  assert(r.ok === false, `${cmd}: ожидалась ошибка, пришло ok=true`);
  assert(humanText(r.error), `${cmd}: ошибка не на языке интерфейса (${UI_LANG}): ${short(r.error)}`);
  return r.error;
}

// ——— сценарий ———

const base = `ws://127.0.0.1:${PORT}/`;
console.log(`Проверка контракта ядро ↔ UI (порт ${PORT}, типы: src/lib/types.ts)`);

await step("без верного токена — закрытие 4401", async () => {
  const bad = new Client(`${base}?token=wrong-${Date.now()}`);
  const code = await Promise.race([bad.closed, new Promise((r) => setTimeout(() => r("timeout"), 5000))]);
  assert(code === 4401, `код закрытия ${code}`);
});

c = new Client(`${base}?token=${encodeURIComponent(TOKEN)}`);
await c.opened();
const t0 = Date.now();

let state;
await step("hello сразу после подключения (EventMap.hello)", async () => {
  const hello = await c.waitEvent("hello", 0, 5000);
  state = hello;
  return `state=${hello.state}, device=${hello.device}, версия ${hello.version}`;
});

// Главное окно при старте (store.tsx)
let settings;
await step("get_state", async () => {
  state = await call("get_state");
  if (state.ui_language === "ru" || state.ui_language === "en") UI_LANG = state.ui_language;
  return `state=${state.state}, first_run_done=${state.first_run_done}, ui_language=${state.ui_language}`;
});
await step("get_settings", async () => {
  settings = await call("get_settings");
  return `${Object.keys(settings).length} ключей`;
});
let styles = [];
await step("styles_list — 5 встроенных стилей", async () => {
  styles = await call("styles_list");
  const ids = styles.map((s) => s.id).sort().join(",");
  assert(ids === "as_is,business,code,messenger,normal", `стили: ${ids}`);
  return ids;
});

// Главная
await step("stats_get (Stats, daily = 30 дней)", async () => {
  const s = await call("stats_get");
  assert(s.daily.length === 30, `daily: ${s.daily.length}`);
  return `слов всего ${s.words_total}, серия ${s.streak_days}`;
});
await step("history_list {limit:5, offset:0} (Главная)", async () => {
  const h = await call("history_list", { limit: 5, offset: 0 });
  return `total=${h.total}`;
});
await step("cuda_check", async () => {
  const r = await call("cuda_check", undefined, 60_000);
  return `ok=${r.ok}, device=${r.device}, VRAM ${r.vram_total_mb ?? "?"} МБ`;
});
let hasKey = false;
await step("ai_key_status openrouter/anthropic (маска, без ключа)", async () => {
  const a = await call("ai_key_status", { provider: "openrouter" });
  const b = await call("ai_key_status", { provider: "anthropic" });
  hasKey = a.has_key;
  assert(!a.masked || a.masked.includes("…"), "ключ не замаскирован");
  return `openrouter: ${a.has_key}, anthropic: ${b.has_key}`;
});
if (!hasKey) {
  await step("ai_key_check без ключа — ответ KeyCheck с ошибкой, без сети", async () => {
    const r = await call("ai_key_check");
    assert(r.valid === false && typeof r.error === "string", "ожидался valid=false с текстом");
    return r.error;
  });
  await step("ai_test без ключа — ошибка на языке интерфейса (текст не теряется)", async () =>
    callErr("ai_test", { text: "ну короче проверка связи раз два три четыре", style: "normal" }),
  );
} else {
  console.log("  (ключ OpenRouter уже в Credential Manager — ai_key_check/ai_test пропущены, чтобы не ходить в сеть)");
  unverified.push("ai_key_check и ai_test без ключа (есть ключ OpenRouter — это запрос в сеть)");
}

// Расход ИИ (Главная, «Настройки → ИИ → Расход», строка трея)
await step("ai_usage_stats: итоги, daily ровно 30 дней по порядку, by_kind/by_model", async () => {
  const s = await call("ai_usage_stats");
  assert(s.daily.length === 30, `daily: ${s.daily.length}`);
  const dates = s.daily.map((d) => d.date);
  assert(dates.every((d) => /^\d{4}-\d{2}-\d{2}$/.test(d)), "даты не YYYY-MM-DD");
  assert(new Set(dates).size === 30 && [...dates].sort().join() === dates.join(), "даты повторяются или не по порядку");
  assert(dates[29] === todayLocal(), `последний день ${dates[29]} — не сегодня`);
  for (const p of ["today", "week", "month", "total"]) {
    const v = s[p];
    assert(v.calls >= 0 && v.tokens_in >= 0 && v.tokens_out >= 0 && v.cost_usd >= 0, `${p}: отрицательные значения`);
  }
  assert(s.total.calls >= s.month.calls && s.month.calls >= s.week.calls && s.week.calls >= s.today.calls, "итоги не вложены (всё ≥ месяц ≥ неделя ≥ сегодня)");
  const todayDay = s.daily[29];
  assert(Math.abs(todayDay.cost_usd - s.today.cost_usd) < 1e-9 && todayDay.calls === s.today.calls, "сегодняшний день в daily ≠ today");
  const kinds = ["cleanup", "command", "test", "translate", "title", "notes"];
  assert(s.by_kind.every((k) => kinds.includes(k.kind)), `неизвестный kind: ${short(s.by_kind.map((k) => k.kind))}`);
  return `вызовов всего ${s.total.calls}, за 30 дней ${s.daily.reduce((a, d) => a + d.calls, 0)}, моделей ${s.by_model.length}`;
});
if (!hasKey) {
  await step("ai_balance без ключа — error на языке интерфейса, баланса нет (без сети)", async () => {
    const b = await call("ai_balance", {});
    assert(humanText(b.error), `error: ${short(b.error)}`);
    assert(b.balance_usd === null, "баланс без ключа");
    const f = await call("ai_balance", { force: true });
    assert(humanText(f.error), "force без ключа — нет ошибки");
    return b.error;
  });
} else {
  console.log("  (ключ OpenRouter есть — ai_balance пропущен: это запрос в OpenRouter)");
  unverified.push("ai_balance (есть ключ OpenRouter — ответ {balance_usd, usage_usd, limit_usd, checked_at, error} не сверен)");
}

// Событие ai_usage и прирост ai_usage_stats — без сети и без ключа: заглушка Ollama
const STUB_IN = 111;
const STUB_OUT = 22;
/** Заглушка Ollama /api/chat: возвращает текст из <dictation> или (fail) HTTP 500. */
function ollamaStub() {
  let fail = false;
  let hits = 0;
  const server = http.createServer((req, res) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (ch) => (body += ch));
    req.on("end", () => {
      if (req.method !== "POST" || req.url !== "/api/chat") {
        res.writeHead(404).end();
        return;
      }
      hits += 1;
      if (fail) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "проверочная ошибка заглушки" }));
        return;
      }
      let text = "";
      try {
        const user = JSON.parse(body).messages?.find((m) => m.role === "user")?.content ?? "";
        text = /<dictation>\s*([\s\S]*?)\s*<\/dictation>/.exec(user)?.[1] ?? user;
      } catch {
        text = "";
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          model: "contract-stub",
          message: { role: "assistant", content: text },
          done: true,
          done_reason: "stop",
          prompt_eval_count: STUB_IN,
          eval_count: STUB_OUT,
        }),
      );
    });
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () =>
      resolve({ port: server.address().port, fail: (v) => (fail = v), hits: () => hits, close: () => server.close() }),
    );
  });
}
if (AI_USAGE) {
  await step("ai_usage: ai_test через заглушку Ollama (успех и ошибка) → событие по EventMap, прирост ai_usage_stats", async () => {
    const stub = await ollamaStub();
    const cur = await call("get_settings");
    const prev = { ai_provider: cur.ai_provider, ollama_url: cur.ollama_url };
    const model = cur.ollama_model;
    const kindCalls = (s, k) => s.by_kind.find((x) => x.kind === k)?.calls ?? 0;
    const modelCalls = (s, m) => s.by_model.find((x) => x.model === m)?.calls ?? 0;
    const text = "проверка контракта расхода раз два три четыре";
    try {
      await call("set_settings", { patch: { ai_provider: "ollama", ollama_url: `http://127.0.0.1:${stub.port}` } });
      const s0 = await call("ai_usage_stats");

      // удачный вызов: токены — от «провайдера», цена Ollama — $0
      let since = Date.now();
      const r = await call("ai_test", { text, style: "normal" });
      assert(r.provider === "ollama" && r.model === model, `provider=${r.provider}, model=${r.model}`);
      assert(stub.hits() === 1, `запросов в заглушку: ${stub.hits()}`);
      const ok = await c.waitEvent("ai_usage", since, 5000, (d) => d.kind === "test");
      checkEvent("ai_usage", ok);
      assert(ok.cost_usd === 0 && ok.tokens_in === STUB_IN && ok.tokens_out === STUB_OUT, `событие: ${short(ok)}`);
      const s1 = await call("ai_usage_stats");
      assert(s1.today.calls === s0.today.calls + 1, `today.calls ${s0.today.calls} → ${s1.today.calls}`);
      assert(
        s1.today.tokens_in === s0.today.tokens_in + STUB_IN && s1.today.tokens_out === s0.today.tokens_out + STUB_OUT,
        "токены за сегодня не прибавились",
      );
      assert(s1.total.calls === s0.total.calls + 1 && s1.daily[29].calls === s0.daily[29].calls + 1, "total/daily не прибавились");
      assert(kindCalls(s1, "test") === kindCalls(s0, "test") + 1, "by_kind.test не прибавился");
      assert(modelCalls(s1, model) === modelCalls(s0, model) + 1, "by_model: модель Ollama не прибавилась");
      assert(
        Math.abs(ok.today_cost_usd - s1.today.cost_usd) < 1e-9,
        `today_cost_usd ${ok.today_cost_usd} ≠ today.cost_usd ${s1.today.cost_usd}`,
      );

      // неудачный вызов: тоже запись и событие, цена и токены — 0, средняя цена не меняется
      stub.fail(true);
      since = Date.now();
      const err = await callErr("ai_test", { text, style: "normal" });
      // d !== ok: событие удачного вызова могло прийти в ту же миллисекунду, что и since
      const bad = await c.waitEvent("ai_usage", since, 5000, (d) => d.kind === "test" && d !== ok);
      checkEvent("ai_usage", bad);
      assert(bad.cost_usd === 0 && bad.tokens_in === 0 && bad.tokens_out === 0, `событие ошибки: ${short(bad)}`);
      const s2 = await call("ai_usage_stats");
      assert(s2.today.calls === s1.today.calls + 1, `ошибка не записана: today.calls ${s1.today.calls} → ${s2.today.calls}`);
      assert(kindCalls(s2, "test") === kindCalls(s1, "test") + 1, "by_kind.test: ошибка не посчитана");
      assert(Math.abs(s2.avg_cost_per_call - s1.avg_cost_per_call) < 1e-9, "неудачный вызов изменил avg_cost_per_call");
      return `событие ×2 по EventMap, today.calls +2; ошибка: ${err}`;
    } finally {
      await call("set_settings", { patch: prev });
      stub.close();
    }
  });
} else {
  unverified.push("событие ai_usage и прирост ai_usage_stats (--no-ai-usage)");
}

// Мастер
await step("setup_status: модель, CUDA, папка модели (model_dir — абсолютная, при пустой настройке — кэш HF)", async () => {
  const s = await call("setup_status");
  const cur = await call("get_settings");
  assert(absPath(s.model_dir) && absPath(s.model_dir_default), "model_dir / model_dir_default — не абсолютные пути");
  assert(typeof cur.model_dir === "string", `settings.model_dir=${short(cur.model_dir)}`);
  if (cur.model_dir === "") {
    assert(samePath(s.model_dir, s.model_dir_default), "settings.model_dir пуст, а папка — не кэш Hugging Face по умолчанию");
  } else {
    assert(samePath(s.model_dir, cur.model_dir), "setup_status.model_dir ≠ settings.model_dir");
  }
  return `модель: ${s.model_ready}, CUDA: ${s.cuda_ready}, папка: ${cur.model_dir === "" ? "по умолчанию (кэш HF)" : "своя"}`;
});
await step("model_recommend: модель под железо, список без distil-*, причина и пояснения на языке интерфейса", async () => {
  const t1 = Date.now();
  const r = await call("model_recommend", undefined, 20_000);
  const ms = Date.now() - t1;
  const names = r.options.map((o) => o.model);
  const need = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"];
  assert(need.every((m) => names.includes(m)), `модели: ${names.join(", ")}`);
  assert(!names.some((m) => m.startsWith("distil")), "в списке distil-* (они только для английского)");
  assert(new Set(names).size === names.length, "модели повторяются");
  const recd = r.options.filter((o) => o.recommended);
  assert(recd.length === 1 && recd[0].model === r.recommended.model, "recommended в options ≠ recommended.model");
  for (const o of r.options) {
    assert(o.size_mb > 0 && o.repo.includes("/"), `${o.model}: size_mb=${o.size_mb}, repo=${short(o.repo)}`);
    assert(humanText(o.note), `${o.model}: note не на языке интерфейса: ${short(o.note)}`);
  }
  assert(humanText(r.reason), `reason не на языке интерфейса: ${short(r.reason)}`);
  // ram_gb 0 — память не определилась
  assert(Number.isInteger(r.cpu_threads) && r.cpu_threads >= 1 && r.ram_gb >= 0, `потоков ${r.cpu_threads}, памяти ${r.ram_gb} ГБ`);
  // Правила подбора (docs/ARCHITECTURE.md): видеокарта NVIDIA есть (CUDA её видит или есть
  // драйвер, а библиотеки ещё не скачаны) — по видеопамяти, нет — по процессору и памяти.
  // У границ (2 и 4 ГБ видеопамяти, 8 ГБ памяти) ядро берёт допуск («8 ГБ» — это 7,9) —
  // там не сверяем
  const rec = r.recommended;
  const vram = r.vram_mb === null ? null : r.vram_mb / 1024;
  const gpu = r.gpu_name !== null || r.cuda_ok;
  const near = (v, edge) => Math.abs(v - edge) < 0.5;
  let want = null;
  if (gpu && vram === null) {
    // объём видеопамяти неизвестен — правилами не задано, только что это видеокарта
    assert(rec.device === "cuda", `видеокарта есть, а рекомендация на ${rec.device}`);
  } else if (gpu && !near(vram, 4) && !near(vram, 2)) {
    want = vram >= 4 ? ["large-v3-turbo", "cuda", "float16"] : vram >= 2 ? ["large-v3-turbo", "cuda", "int8_float16"] : ["small", "cuda", "int8_float16"];
  } else if (!gpu && !near(r.ram_gb, 8)) {
    const ramOk = r.ram_gb >= 8 || r.ram_gb === 0;
    want = r.cpu_threads >= 8 && ramOk ? ["small", "cpu", "int8"] : ["base", "cpu", "int8"];
  }
  if (want) {
    const got = [rec.model, rec.device, rec.compute_type];
    assert(got.join("/") === want.join("/"), `рекомендация ${got.join("/")}, по правилам ${want.join("/")}`);
  }
  assert(ms < 10_000, `ответ за ${ms} мс (бюджет ядра ~5 с)`);
  const t2 = Date.now();
  await call("model_recommend", undefined, 20_000);
  return `${rec.model} / ${rec.device} / ${rec.compute_type}; ${r.gpu_name ?? "без видеокарты"}, ${r.cpu_threads} потоков, ${r.ram_gb} ГБ; ${ms} мс, повтор ${Date.now() - t2} мс`;
});
await step("model_dir в set_settings: относительный путь и не строка — ошибки на языке интерфейса, папка не меняется", async () => {
  const before = (await call("get_settings")).model_dir;
  const err = await callErr("set_settings", { patch: { model_dir: "models\\whisper" } });
  await callErr("set_settings", { patch: { model_dir: 5 } });
  assert((await call("get_settings")).model_dir === before, "неверная папка применилась");
  return err;
});
await step("model_dir_set: та же папка — {ok, moving:false} без перемен; относительный путь, путь-файл — ошибки", async () => {
  const s0 = await call("get_settings");
  const st0 = await call("setup_status");
  // та же папка, что сейчас (и явным путём, и как в настройках) — ничего не переносится и не качается
  for (const path of [st0.model_dir, s0.model_dir]) {
    const r = await call("model_dir_set", { path, move: true });
    assert(r.ok === true && r.moving === false, `та же папка: ${short(r)}`);
  }
  const err = await callErr("model_dir_set", { path: "relative\\models", move: false });
  await callErr("model_dir_set", { path: 5, move: false });
  await callErr("model_dir_set", { path: st0.model_dir, move: "yes" });
  // на месте папки — файл: создать её нельзя
  const file = join(tmpdir(), `rupor-contract-${process.pid}-${Date.now()}.txt`);
  writeFileSync(file, "x");
  try {
    await callErr("model_dir_set", { path: file, move: false });
  } finally {
    unlinkSync(file);
  }
  const s1 = await call("get_settings");
  const st1 = await call("setup_status");
  assert(s1.model_dir === s0.model_dir && samePath(st1.model_dir, st0.model_dir), "папка модели сменилась");
  return err;
});
// Смена папки по-настоящему — только на ядре, которое после неё ничего не станет качать:
// первый запуск (мастер не пройден), модели нет, ядро ждёт setup_download (state=error).
// Ядро после мастера или со скачанной моделью начало бы загрузку (или перенос) гигабайтов.
await step("model_dir_set и set_settings со сменой папки: событие settings, setup_status — новая папка, возврат", async () => {
  const st0 = await call("setup_status");
  const s0 = await call("get_settings");
  const live = await call("get_state");
  if (st0.first_run_done || st0.model_ready || live.state !== "error" || live.model_loaded) {
    unverified.push("model_dir_set и set_settings {model_dir} со сменой папки (ядро не на первом запуске или модель скачана — смена папки начала бы загрузку)");
    return "пропуск: ядро не ждёт мастера";
  }
  const dir = join(tmpdir(), `rupor-contract-models-${process.pid}-${Date.now()}`);
  try {
    // модели нет — переносить нечего: move=true сводится к простой смене папки
    let since = Date.now();
    const r = await call("model_dir_set", { path: dir, move: true });
    assert(r.ok === true && r.moving === false, `смена папки: ${short(r)}`);
    const ev = await c.waitEvent("settings", since, 3000, (d) => samePath(d.model_dir, dir));
    checkEvent("settings", ev);
    const st1 = await call("setup_status");
    assert(samePath(st1.model_dir, dir) && st1.model_ready === false, "setup_status.model_dir — не новая папка");
    assert(samePath(st1.model_dir_default, st0.model_dir_default), "model_dir_default сменился");
    // повтор той же папки — ничего не делает
    const again = await call("model_dir_set", { path: dir, move: false });
    assert(again.moving === false, short(again));
    // возврат обычной set_settings (тот же валидатор, без переноса)
    since = Date.now();
    const s1 = await call("set_settings", { patch: { model_dir: s0.model_dir } });
    assert(s1.model_dir === s0.model_dir, `model_dir=${short(s1.model_dir)}`);
    await c.waitEvent("settings", since, 3000, (d) => d.model_dir === s0.model_dir);
  } finally {
    const back = await c.raw("model_dir_set", { path: s0.model_dir, move: false });
    if (!back.ok) console.log("  !!   папку модели вернуть не удалось");
    rmSync(dir, { recursive: true, force: true });
  }
  const st2 = await call("setup_status");
  assert(samePath(st2.model_dir, st0.model_dir), "папка модели не вернулась");
  return "смена туда и обратно, перенос не понадобился";
});
unverified.push("model_dir_set с переносом скачанной модели (move=true): перенос и загрузка модели — гигабайты, на живом ядре не запускаются");
await step("list_devices", async () => `${(await call("list_devices")).length} устройств ввода`);

// Словарь
let dictId = null;
await step("dict_upsert (новый) → DictItem", async () => {
  const d = await call("dict_upsert", { term: "КонтрактТест", variants: ["контракт тест", "контракт-тест"] });
  dictId = d.id;
  assert(d.term === "КонтрактТест" && d.variants.length === 2, short(d));
  return `id=${d.id}`;
});
await step("dict_upsert (правка по id) → DictItem", async () => {
  const d = await call("dict_upsert", { id: dictId, term: "КонтрактТест", variants: ["контракт тест"] });
  assert(d.id === dictId && d.variants.length === 1, short(d));
});
await step("dict_list содержит запись", async () => {
  const list = await call("dict_list");
  assert(list.some((d) => d.id === dictId), "записи нет");
  return `${list.length} терминов`;
});
await step("dict_upsert с пустым термином — ошибка на языке интерфейса", async () =>
  callErr("dict_upsert", { term: "  ", variants: [] }),
);
await step("dict_delete → null, записи больше нет", async () => {
  assert((await call("dict_delete", { id: dictId })) === null, "не null");
  assert(!(await call("dict_list")).some((d) => d.id === dictId), "запись осталась");
});

// Словарь учится на правках: кнопки «Добавить» (dict_add) и «Отменить» (dict_undo).
// Проверочный термин, откат в конце. dict_learn_undo заносит правку в «не учить» (kv ядра).
const LT = "КонтрактОбучение";
const LV1 = "контракт обучение";
const LV2 = "контракт-обучение";
const learnCleanup = async () => {
  for (const d of await call("dict_list")) if (d.term === LT) await call("dict_delete", { id: d.id });
};
await step("dict_learn_accept (новый термин) → DictItem, без дублей при повторе", async () => {
  try {
    const a = await call("dict_learn_accept", { term: LT, variant: LV1 });
    assert(a.term === LT && a.variants.length === 1 && a.variants[0] === LV1, short(a));
    const b = await call("dict_learn_accept", { term: LT.toLowerCase(), variant: LV1.toUpperCase() });
    assert(b.id === a.id && b.term === LT && b.variants.length === 1, `повтор: ${short(b)}`);
    const c2 = await call("dict_learn_accept", { term: LT, variant: LV2 });
    assert(c2.id === a.id && c2.variants.length === 2, `второй вариант: ${short(c2)}`);
    const list = (await call("dict_list")).filter((d) => d.term === LT);
    assert(list.length === 1, `терминов «${LT}»: ${list.length}`);
    return `id=${a.id}, варианты: ${c2.variants.length}`;
  } catch (e) {
    await learnCleanup();
    throw e;
  }
});
// «Недавно добавлено» на странице «Словарь»: журнал обучения, свежие сверху
const learnJournalMine = async (limit) => {
  const d = (await call("dict_list")).find((x) => x.term === LT);
  assert(d, "термина нет");
  return (await call("dict_learn_journal", { limit })).filter((e) => e.id === d.id && e.term === LT);
};
await step("dict_learn_journal {limit} → свежие сверху, «Добавить» — source=accepted", async () => {
  try {
    const j = await call("dict_learn_journal", { limit: 10 });
    assert(j.length <= 10, `limit 10 → ${j.length}`);
    const ts = j.map((e) => e.ts).filter((x) => x !== null);
    assert(ts.every((x, i) => i === 0 || ts[i - 1] >= x), "не свежие сверху");
    // повтор той же правки словарь не меняет — в журнале две записи: второй вариант и новый термин
    const mine = await learnJournalMine(300);
    assert(mine.length === 2, `записей «${LT}»: ${short(mine)}`);
    assert(mine[0].variant === LV2 && mine[0].created === false, `свежая: ${short(mine[0])}`);
    assert(mine[1].variant === LV1 && mine[1].created === true, `первая: ${short(mine[1])}`);
    assert(mine.every((e) => e.source === "accepted"), `source: ${short(mine.map((e) => e.source))}`);
    const now = Date.now() / 1000;
    assert(mine.every((e) => typeof e.ts === "number" && Math.abs(now - e.ts) < 300), `ts: ${short(mine.map((e) => e.ts))}`);
    const all = await call("dict_learn_journal", {});
    assert(all.length <= 50, `по умолчанию не больше 50 → ${all.length}`);
    assert((await call("dict_learn_journal", { limit: 0 })).length === 0, "limit 0 — не пусто");
    return `записей ${all.length}, проверочных ${mine.length}`;
  } catch (e) {
    await learnCleanup();
    throw e;
  }
});
await step("dict_learn_journal с неверным limit — ошибка на языке интерфейса", async () => {
  await callErr("dict_learn_journal", { limit: -1 });
  await callErr("dict_learn_journal", { limit: 301 });
  return callErr("dict_learn_journal", { limit: "много" });
});
await step("dict_learn_undo (created=false) → null, убран только вариант", async () => {
  try {
    const d = (await call("dict_list")).find((x) => x.term === LT);
    assert(d, "термина нет");
    const r = await call("dict_learn_undo", { term: LT, variant: LV2, id: d.id, created: false });
    assert(r === null, "не null");
    const after = (await call("dict_list")).find((x) => x.id === d.id);
    assert(after && after.variants.length === 1 && after.variants[0] === LV1, short(after));
    // повторная отмена той же правки ничего не ломает
    assert((await call("dict_learn_undo", { term: LT, variant: LV2, id: d.id, created: false })) === null, "не null");
    // отменённая правка из «Недавно добавлено» пропала, первая осталась
    const mine = await learnJournalMine(300);
    assert(mine.length === 1 && mine[0].variant === LV1 && mine[0].created === true, `журнал: ${short(mine)}`);
  } catch (e) {
    await learnCleanup();
    throw e;
  }
});
await step("dict_learn_undo (created=true) → null, термин удалён целиком", async () => {
  try {
    const d = (await call("dict_list")).find((x) => x.term === LT);
    assert(d, "термина нет");
    assert((await call("dict_learn_undo", { term: LT, variant: LV1, id: d.id, created: true })) === null, "не null");
    assert(!(await call("dict_list")).some((x) => x.term === LT), "термин остался");
    assert((await call("dict_learn_undo", { term: LT, variant: LV1, id: d.id, created: true })) === null, "не null");
  } finally {
    await learnCleanup();
  }
});
await step("dict_learn_accept: пустой термин и чужой вариант — ошибки на языке интерфейса", async () => {
  const e1 = await callErr("dict_learn_accept", { term: "  ", variant: "что-то" });
  const own = await call("dict_upsert", { term: "КонтрактЧужой", variants: [LV1] });
  try {
    await callErr("dict_learn_accept", { term: LT, variant: LV1 });
    await callErr("dict_learn_undo", { term: LT, variant: 5, id: null, created: false });
  } finally {
    await call("dict_delete", { id: own.id });
    await learnCleanup();
  }
  return e1;
});

// Сниппеты
let snipId = null;
await step("snippet_upsert / snippets_list / snippet_delete", async () => {
  const s = await call("snippet_upsert", { trigger: "контракт подпись", text: "С уважением,\nПроверка" });
  snipId = s.id;
  const s2 = await call("snippet_upsert", { id: snipId, trigger: "контракт подпись", text: "С уважением" });
  assert(s2.id === snipId && s2.text === "С уважением", short(s2));
  assert((await call("snippets_list")).some((x) => x.id === snipId), "нет в списке");
  assert((await call("snippet_delete", { id: snipId })) === null, "не null");
  assert(!(await call("snippets_list")).some((x) => x.id === snipId), "осталась");
});

// Импорт / экспорт
await step("export_data → import_data (слияние) → export_data", async () => {
  const before = await call("export_data");
  const r = await call("import_data", {
    data: {
      version: 1,
      dictionary: [{ term: "КонтрактИмпорт", variants: ["контракт импорт"] }],
      snippets: [{ trigger: "контракт импорт", text: "импортированный текст" }],
    },
    replace: false,
  });
  assert(r.dictionary === 1 && r.snippets === 1, short(r));
  const after = await call("export_data");
  assert(after.dictionary.length === before.dictionary.length + 1, "термин не добавился");
  // откат
  for (const d of await call("dict_list")) if (d.term === "КонтрактИмпорт") await call("dict_delete", { id: d.id });
  for (const s of await call("snippets_list")) if (s.trigger === "контракт импорт") await call("snippet_delete", { id: s.id });
  return `импортировано ${r.dictionary}+${r.snippets}, откат сделан`;
});
await step("import_data с мусором — ошибка на языке интерфейса", async () =>
  callErr("import_data", { data: "не json", replace: false }),
);

// История, черновик
await step("history_list с поиском и страницей", async () => {
  const a = await call("history_list", { query: "контракт", limit: 50, offset: 0 });
  const b = await call("history_list", { limit: 50, offset: 50 });
  return `найдено ${a.total}, вторая страница ${b.items.length}`;
});
await step("history_list с неверным limit — ошибка на языке интерфейса", async () =>
  callErr("history_list", { limit: "много" }),
);
await step("history_reinsert несуществующей записи — ошибка, ничего не вставлено", async () =>
  callErr("history_reinsert", { id: 987654321 }),
);
await step("history_delete несуществующей записи → null", async () => {
  assert((await call("history_delete", { id: 987654321 })) === null, "не null");
});
await step("history_clear {period:'day'} → {deleted}", async () => {
  const r = await call("history_clear", { period: "day" });
  return `удалено ${r.deleted}`;
});
await step("draft_set / draft_get (кириллица, переводы строк)", async () => {
  const orig = (await call("draft_get")).text;
  const text = "Черновик проверки контракта\nвторая строка — «ёлка»";
  assert((await call("draft_set", { text })) === null, "не null");
  assert((await call("draft_get")).text === text, "текст не совпал");
  await call("draft_set", { text: orig });
});

// Правила приложений
await step("app_rule_upsert / app_rules_list / app_rule_delete", async () => {
  const r = await call("app_rule_upsert", { process: "Contract-Check.exe", insert_mode: "type", style: "code" });
  assert(r.process === "contract-check.exe", `process=${r.process}`);
  const list = await call("app_rules_list");
  assert(list.some((x) => x.process === "contract-check.exe" && x.insert_mode === "type"), "нет в списке");
  assert((await call("app_rule_delete", { process: "contract-check.exe" })) === null, "не null");
  assert(!(await call("app_rules_list")).some((x) => x.process === "contract-check.exe"), "осталось");
  return `${list.length} правил (с проверочным)`;
});
await step("app_rule_upsert с неизвестным стилем — ошибка на языке интерфейса", async () =>
  callErr("app_rule_upsert", { process: "x.exe", insert_mode: null, style: "nope" }),
);
await step("recent_apps: {process, title, titles ≤ 10}", async () => {
  const list = await call("recent_apps");
  assert(list.every((a) => a.titles.length <= 10), "titles больше 10");
  return `${list.length} процессов, заголовков ${list.reduce((n, a) => n + a.titles.length, 0)}`;
});

// Правила по названию окна (проверочные записи, откат в конце)
const TR = "проверка контракта";
const trCleanup = async () => {
  for (const x of await call("title_rules_list")) {
    if (lower(x.title).startsWith(TR)) await call("title_rule_delete", { process: x.process, title: x.title });
  }
};
await step("title_rule_upsert / title_rules_list / title_rule_delete", async () => {
  try {
    const r = await call("title_rule_upsert", { process: "Contract-Check.exe", title: "Проверка контракта", insert_mode: "type", style: "code" });
    assert(r.process === "contract-check.exe", `process=${r.process}`);
    assert(r.title === "Проверка контракта" && r.insert_mode === "type" && r.style === "code", short(r));
    // тот же process+title в другом регистре — это то же правило (обновление, без дубля)
    await call("title_rule_upsert", { process: "contract-check.exe", title: "ПРОВЕРКА КОНТРАКТА", insert_mode: null, style: "business" });
    const list = await call("title_rules_list");
    const mine = list.filter((x) => x.process === "contract-check.exe" && lower(x.title) === TR);
    assert(mine.length === 1, `правил с этим ключом: ${mine.length}`);
    assert(mine[0].insert_mode === null && mine[0].style === "business", short(mine[0]));
    assert((await call("get_settings")).title_rules.some((x) => x.process === "contract-check.exe"), "нет в settings.title_rules");
    // "*" — любое приложение
    const any = await call("title_rule_upsert", { process: "*", title: "Проверка контракта *", insert_mode: null, style: "as_is" });
    assert(any.process === "*", `process=${any.process}`);
    assert((await call("title_rule_delete", { process: "contract-check.exe", title: "Проверка контракта" })) === null, "не null");
    assert(!(await call("title_rules_list")).some((x) => x.process === "contract-check.exe"), "осталось");
    return `${list.length} правил (с проверочными)`;
  } finally {
    await trCleanup();
  }
});
await step("title_rules: короткий title, неизвестный стиль, дубли в set_settings — ошибки на языке интерфейса", async () => {
  try {
    await callErr("title_rule_upsert", { process: "contract-check.exe", title: "п", insert_mode: null, style: "code" });
    await callErr("title_rule_upsert", { process: "contract-check.exe", title: "Проверка контракта", insert_mode: null, style: "nope" });
    const before = (await call("get_settings")).title_rules;
    const dup = { process: "contract-check.exe", title: "Проверка контракта", insert_mode: "type", style: null };
    const e = await callErr("set_settings", { patch: { title_rules: [...before, dup, { ...dup, title: "ПРОВЕРКА КОНТРАКТА" }] } });
    assert(!(await call("get_settings")).title_rules.some((x) => x.process === "contract-check.exe"), "дубли применились");
    return e;
  } finally {
    await trCleanup();
  }
});

// RDP: рассылка только в отмеченные (broadcast_targets, ключи без учёта регистра)
await step("rdp_windows {key,…} / rdp_toggle {key} и {title} → broadcast_targets", async () => {
  const w = await call("rdp_windows");
  assert(w.every((x) => x.key.trim().length > 0), "пустой ключ сессии");
  const before = (await call("get_settings")).broadcast_targets;
  const has = (list, k) => list.filter((x) => lower(x) === lower(k)).length;
  const key = "Проверка контракта (нет такого окна)";
  try {
    assert((await call("rdp_toggle", { key, included: true })) === null, "не null");
    assert(has((await call("get_settings")).broadcast_targets, key) === 1, "ключ не добавился");
    await call("rdp_toggle", { key: key.toUpperCase(), included: true });
    assert(has((await call("get_settings")).broadcast_targets, key) === 1, "ключ в другом регистре — дубль");
    await call("rdp_toggle", { key: key.toUpperCase(), included: false });
    assert(has((await call("get_settings")).broadcast_targets, key) === 0, "ключ не убрался (регистр)");
    // совместимость: {title, included}
    assert((await call("rdp_toggle", { title: key, included: true })) === null, "не null");
    assert(has((await call("get_settings")).broadcast_targets, key) === 1, "{title} не добавил ключ");
    await call("rdp_toggle", { title: key, included: false });
    assert(has((await call("get_settings")).broadcast_targets, key) === 0, "{title} не убрал ключ");
    const after = (await call("get_settings")).broadcast_targets;
    assert(after.map(lower).sort().join("\u0001") === before.map(lower).sort().join("\u0001"), "белый список не вернулся к исходному");
    return `${w.length} RDP-окон, отмечено ${w.filter((x) => x.included).length}, ключей в белом списке ${before.length}`;
  } finally {
    const now = (await call("get_settings")).broadcast_targets;
    for (const k of now) if (lower(k) === lower(key)) await call("rdp_toggle", { key: k, included: false });
  }
});

// Частые слова (личный словарь частот) и контекст окна
await step("vocab_list {limit, process} → [{word, count, excluded}]", async () => {
  const a = await call("vocab_list", { limit: 5 });
  assert(a.length <= 5, `limit 5 → ${a.length}`);
  const b = await call("vocab_list", {});
  assert(b.length <= 50, `по умолчанию не больше 50 → ${b.length}`);
  assert(b.every((x, i) => i === 0 || b[i - 1].count >= x.count), "не по убыванию частоты");
  const c = await call("vocab_list", { process: "contract-check.exe", limit: 10 });
  return `${b.length} слов, для проверочного процесса ${c.length}`;
});
await step("vocab_exclude {word, excluded} → null, флаг виден в vocab_list", async () => {
  assert((await call("vocab_exclude", { word: "КонтрактСлово", excluded: true })) === null, "не null");
  assert((await call("vocab_exclude", { word: "КонтрактСлово", excluded: false })) === null, "не null");
  const list = await call("vocab_list", { limit: 5 });
  if (!list.length) return "словарь частот пуст — флаг проверен только на проверочном слове";
  const first = list[0];
  try {
    await call("vocab_exclude", { word: first.word, excluded: !first.excluded });
    const again = (await call("vocab_list", { limit: 200 })).find((x) => x.word === first.word);
    assert(again && again.excluded === !first.excluded, "флаг excluded не изменился");
  } finally {
    await call("vocab_exclude", { word: first.word, excluded: first.excluded });
  }
  return "флаг меняется и возвращается";
});
await step("vocab_enabled / context_enabled: true/false, неверное — ошибка на языке интерфейса", async () => {
  const prev = { vocab_enabled: settings.vocab_enabled, context_enabled: settings.context_enabled };
  assert(typeof prev.vocab_enabled === "boolean" && typeof prev.context_enabled === "boolean", short(prev));
  try {
    const s = await call("set_settings", { patch: { vocab_enabled: !prev.vocab_enabled, context_enabled: !prev.context_enabled } });
    assert(s.vocab_enabled === !prev.vocab_enabled && s.context_enabled === !prev.context_enabled, "не применилось");
    const err = await callErr("set_settings", { patch: { vocab_enabled: "да" } });
    return `сейчас vocab=${prev.vocab_enabled}, context=${prev.context_enabled}; ${err}`;
  } finally {
    await call("set_settings", { patch: prev });
  }
});
await step("dict_fuzzy (похожие написания): true/false, неверное — ошибка на языке интерфейса", async () => {
  const prev = settings.dict_fuzzy;
  assert(typeof prev === "boolean", short({ dict_fuzzy: prev }));
  try {
    const s = await call("set_settings", { patch: { dict_fuzzy: !prev } });
    assert(s.dict_fuzzy === !prev, "не применилось");
    const err = await callErr("set_settings", { patch: { dict_fuzzy: "да" } });
    return `сейчас dict_fuzzy=${prev}; ${err}`;
  } finally {
    await call("set_settings", { patch: { dict_fuzzy: prev } });
  }
});

// Настройки
await step("set_settings {theme} → Settings + событие settings", async () => {
  const since = Date.now();
  const prev = settings.theme;
  const next = prev === "dark" ? "light" : "dark";
  const s = await call("set_settings", { patch: { theme: next } });
  assert(s.theme === next, `theme=${s.theme}`);
  const ev = await c.waitEvent("settings", since, 3000, (d) => d.theme === next);
  await call("set_settings", { patch: { theme: prev } });
  return `событие settings пришло, theme=${ev.theme}`;
});
await step("set_settings {pill_position:[x,y]} и null", async () => {
  const s = await call("set_settings", { patch: { pill_position: [120, 640] } });
  assert(Array.isArray(s.pill_position) && s.pill_position[0] === 120, short(s.pill_position));
  const s2 = await call("set_settings", { patch: { pill_position: null } });
  assert(s2.pill_position === null, short(s2.pill_position));
});
await step("set_settings {insert_last_hotkey} → State.hotkeys.insert_last", async () => {
  const since = Date.now();
  const prev = settings.insert_last_hotkey;
  await call("set_settings", { patch: { insert_last_hotkey: "ctrl+alt+shift+b" } });
  const st = await call("get_state");
  assert(st.hotkeys.insert_last === "ctrl+alt+shift+b", short(st.hotkeys));
  const ev = await c.waitEvent("state", since, 3000, (d) => d.hotkeys.insert_last === "ctrl+alt+shift+b");
  await call("set_settings", { patch: { insert_last_hotkey: prev } });
  return `событие state: insert_last=${ev.hotkeys.insert_last}`;
});
await step("learn_mode: auto/suggest/off, неверное — ошибка на языке интерфейса", async () => {
  const prev = settings.learn_mode;
  assert(["auto", "suggest", "off"].includes(prev), `learn_mode=${short(prev)}`);
  try {
    for (const m of ["suggest", "off", "auto"]) {
      const s = await call("set_settings", { patch: { learn_mode: m } });
      assert(s.learn_mode === m, `learn_mode=${s.learn_mode}`);
    }
    const err = await callErr("set_settings", { patch: { learn_mode: "always" } });
    assert((await call("get_settings")).learn_mode === "auto", "неверный режим применился");
    return `сейчас ${prev}; ${err}`;
  } finally {
    await call("set_settings", { patch: { learn_mode: prev } });
  }
});
await step("duck_mode: off/duck/mute, duck_level целое 0–100, неверное — ошибка на языке интерфейса", async () => {
  const prev = { duck_mode: settings.duck_mode, duck_level: settings.duck_level };
  assert(["off", "duck", "mute"].includes(prev.duck_mode), `duck_mode=${short(prev.duck_mode)}`);
  assert(Number.isInteger(prev.duck_level) && prev.duck_level >= 0 && prev.duck_level <= 100, `duck_level=${short(prev.duck_level)}`);
  try {
    for (const m of ["mute", "off", "duck"]) {
      const s = await call("set_settings", { patch: { duck_mode: m } });
      assert(s.duck_mode === m, `duck_mode=${s.duck_mode}`);
    }
    for (const n of [0, 100, 35]) {
      const s = await call("set_settings", { patch: { duck_level: n } });
      assert(s.duck_level === n, `duck_level=${s.duck_level}`);
    }
    const errMode = await callErr("set_settings", { patch: { duck_mode: "loud" } });
    const errLevel = await callErr("set_settings", { patch: { duck_level: 101 } });
    await callErr("set_settings", { patch: { duck_level: -1 } });
    await callErr("set_settings", { patch: { duck_level: "15" } });
    const now = await call("get_settings");
    assert(now.duck_mode === "duck" && now.duck_level === 35, `неверное значение применилось: ${short([now.duck_mode, now.duck_level])}`);
    return `сейчас ${prev.duck_mode}, ${prev.duck_level} %; ${errMode}; ${errLevel}`;
  } finally {
    await call("set_settings", { patch: prev });
  }
});
await step("language: en/ru/system → State.ui_language + событие state; ошибки и стили — на языке интерфейса", async () => {
  const prev = settings.language;
  assert(["system", "ru", "en"].includes(prev), `language=${short(prev)}`);
  assert(["ru", "en"].includes(state.ui_language), `State.ui_language=${short(state.ui_language)}`);
  const notes = [];
  try {
    for (const lang of ["en", "ru"]) {
      const since = Date.now();
      const s = await call("set_settings", { patch: { language: lang } });
      assert(s.language === lang, `language=${s.language}`);
      const ev = await c.waitEvent("state", since, 3000, (d) => d.ui_language === lang);
      checkEvent("state", ev);
      UI_LANG = lang;
      const st = await call("styles_list");
      const text = st.map((x) => `${x.name} ${x.description}`).join(" ");
      assert(humanText(text), `styles_list не на ${lang}: ${short(text)}`);
      notes.push(`${lang}: «${st.find((x) => x.id === "business")?.name}», ${await callErr("no_such_command", {})}`);
    }
    await callErr("set_settings", { patch: { language: "de" } });
    assert((await call("get_settings")).language === "ru", "неверный язык применился");
    const since = Date.now();
    await call("set_settings", { patch: { language: "system" } });
    const ev = await c.waitEvent("state", since, 3000);
    assert(["ru", "en"].includes(ev.ui_language), `system → ui_language=${short(ev.ui_language)}`);
    notes.push(`system → ${ev.ui_language}`);
    return notes.join("; ");
  } finally {
    await call("set_settings", { patch: { language: prev } });
    const now = await call("get_state");
    if (now.ui_language === "ru" || now.ui_language === "en") UI_LANG = now.ui_language;
  }
});
await step("dictation_language: auto/ru/en, неверное — ошибка", async () => {
  const prev = settings.dictation_language;
  assert(["auto", "ru", "en"].includes(prev), `dictation_language=${short(prev)}`);
  try {
    for (const m of ["ru", "en", "auto"]) {
      const s = await call("set_settings", { patch: { dictation_language: m } });
      assert(s.dictation_language === m, `dictation_language=${s.dictation_language}`);
    }
    const err = await callErr("set_settings", { patch: { dictation_language: "de" } });
    assert((await call("get_settings")).dictation_language === "auto", "неверное значение применилось");
    return `сейчас ${prev}; ${err}`;
  } finally {
    await call("set_settings", { patch: { dictation_language: prev } });
  }
});
// Перевод встречи (окно перевода и «Настройки → Встречи»)
let meetingRunning = false;
/** Сеанс встречи на ядре был (идёт или остановлен): заметки по нему — вызов ИИ. */
let meetingSession = false;
/** MeetingStatus.loopback_available: можно ли захватить звук одной программы. */
let meetingLoopback = true;
await step("meeting_status → MeetingStatus, как State.meeting", async () => {
  const m = await call("meeting_status");
  const st = await call("get_state");
  assert(st.meeting.running === m.running && st.meeting.state === m.state, `State.meeting ${short(st.meeting)} ≠ ${short(m)}`);
  assert(m.running || m.state === "idle" || m.state === "error", `не идёт, а state=${m.state}`);
  for (const k of ["asr_ms", "first_token_ms", "done_ms"]) {
    const v = m.latency[k];
    assert(v === null || (Number.isFinite(v) && v >= 0), `latency.${k}=${v}`);
  }
  meetingRunning = m.running;
  // в types.ts поле необязательно (интерфейс без него считает «можно»), но ядро шлёт его всегда
  assert(typeof m.loopback_available === "boolean", `loopback_available: ${short(m.loopback_available)}`);
  assert(st.meeting.loopback_available === m.loopback_available, "State.meeting.loopback_available ≠ meeting_status");
  meetingLoopback = m.loopback_available;
  // заметки в статусе — без текста (он в событии meeting_notes и meeting_notes_get)
  assert(m.notes === null || (typeof m.notes === "object" && !("markdown" in m.notes)), `notes: ${short(m.notes)}`);
  meetingSession = m.started_at !== null;
  return `running=${m.running}, state=${m.state}, строк ${m.lines}, заметки: ${m.notes?.status ?? "нет"}, звук одной программы: ${meetingLoopback ? "можно" : "нельзя (весь звук компьютера)"}`;
});
await step("meeting_notes_get → MeetingNotes | null, как MeetingStatus.notes", async () => {
  const n = await call("meeting_notes_get");
  const m = await call("meeting_status");
  assert((n === null) === (m.notes === null), `meeting_notes_get ${short(n)} при notes=${short(m.notes)}`);
  if (n) assert(n.status === m.notes.status, `status ${n.status} ≠ ${m.notes.status}`);
  return n === null ? "null (заметок не было)" : `${n.status}, ${n.markdown ? n.markdown.length + " симв." : "без текста"}`;
});
if (meetingSession) {
  unverified.push("meeting_notes (на ядре был сеанс встречи — заметки стали бы вызовом ИИ)");
} else {
  await step("meeting_notes без встречи — ошибка на языке интерфейса", async () => callErr("meeting_notes"));
}
await step("meeting_copy с неверным what — ошибка (буфер обмена не трогается)", async () =>
  callErr("meeting_copy", { what: "bogus" }),
);
await step("meeting_sources → AudioSource[]: процессы proc:<pid> и в конце system", async () => {
  const list = await call("meeting_sources");
  const sys = list.filter((s) => s.kind === "system");
  assert(sys.length === 1 && sys[0].id === "system" && sys[0].pid === null, `system: ${sys.length}`);
  assert(list[list.length - 1].id === "system", "system не последний");
  // available — у каждого источника (ядро шлёт всегда): весь звук компьютера — всегда true,
  // программы — как loopback_available
  assert(sys[0].available === true, `system: available=${short(sys[0].available)}`);
  const procs = list.filter((x) => x.kind === "process");
  for (const s of procs) {
    assert(s.id === `proc:${s.pid}`, `id ${short(s.id)} ≠ proc:${s.pid}`);
    assert(s.available === meetingLoopback, `${s.id}: available=${short(s.available)} при loopback_available=${meetingLoopback}`);
  }
  // встречи — первыми (сортировка ядра)
  const firstNonMeeting = list.findIndex((s) => !s.meeting);
  assert(list.slice(firstNonMeeting < 0 ? list.length : firstNonMeeting).every((s) => !s.meeting), "встречи не первыми");
  const blocked = procs.filter((s) => s.available === false).length;
  return `${list.length - 1} процессов со звуком (встреч ${list.filter((s) => s.meeting).length}, играют ${list.filter((s) => s.active && s.kind === "process").length}${blocked ? `, захватить нельзя ${blocked}` : ""})`;
});
await step("meeting_lines {after_id: 0, limit: 5} → MeetingLine[] по возрастанию id", async () => {
  const lines = await call("meeting_lines", { after_id: 0, limit: 5 });
  assert(lines.length <= 5, `строк ${lines.length}`);
  for (let i = 0; i < lines.length; i++) {
    assert(lines[i].id >= 1 && (i === 0 || lines[i].id > lines[i - 1].id), "id не по возрастанию");
    // кончившаяся фраза — без неподтверждённого хвоста
    assert(!lines[i].final || lines[i].partial === "", `строка ${lines[i].id}: final, а partial не пуст`);
  }
  const all = await call("meeting_lines");
  assert(Array.isArray(all) && all.length <= 200, `без аргументов — ${all.length} строк`);
  return `${lines.length} строк`;
});
await step("meeting_lines с неверным limit — ошибка на языке интерфейса", async () =>
  callErr("meeting_lines", { limit: 0 }),
);
if (meetingRunning) {
  unverified.push("meeting_start с неверным источником и meeting_stop (на ядре идёт перевод встречи — его не трогаем)");
} else {
  await step("meeting_start с несуществующим источником — ошибка, сеанс не начат", async () => {
    try {
      const err = await callErr("meeting_start", { source_id: "nope:0" });
      const m = await call("meeting_status");
      assert(!m.running, "сеанс начался");
      return err;
    } finally {
      const m = await call("meeting_status");
      if (m.running) await call("meeting_stop");
    }
  });
  await step("meeting_stop без сеанса → MeetingStatus (running=false)", async () => {
    const m = await call("meeting_stop");
    assert(!m.running, "running=true");
    return `state=${m.state}`;
  });
  await step("meeting_autostop_cancel без сеанса → MeetingStatus без отсчёта", async () => {
    const m = await call("meeting_autostop_cancel");
    assert(!m.running && m.ending_in_s === null && m.hint === null, short(m));
    return `hint=${m.hint}, ending_in_s=${m.ending_in_s}`;
  });
}
if (meetingRunning) {
  unverified.push("meeting_autostop_cancel (на ядре идёт перевод встречи — его автоостановку не трогаем)");
}
// Предложение встречи: только неизвестный id и ответ «later» (ничего не начинает и ничего не
// добавляет в meeting_ignore_apps); событие meeting_suggest без настоящей встречи не приходит.
await step("meeting_suggest_answer: неизвестный id → null/MeetingStatus или ошибка; неверный ответ — ошибка", async () => {
  called.add("meeting_suggest_answer");
  const r = await c.raw("meeting_suggest_answer", { id: `check-${Date.now()}`, answer: "later" });
  let note;
  if (r.ok) {
    const errs = check(r.result, resultType("meeting_suggest_answer"), "meeting_suggest_answer.result", extras);
    assert(!errs.length, errs.slice(0, 4).join("; "));
    assert(r.result === null || !r.result.running || meetingRunning, "ответ later начал встречу");
    note = r.result === null ? "null" : `MeetingStatus (running=${r.result.running})`;
  } else {
    assert(humanText(r.error), `ошибка не на языке интерфейса (${UI_LANG}): ${short(r.error)}`);
    note = `ошибка: ${r.error}`;
  }
  const err = await callErr("meeting_suggest_answer", { id: "check-0", answer: "bogus" });
  return `${note}; неверный ответ — ${err}`;
});
await step("настройки встреч: языки, размер текста, место окна и полоски, автоостановка, шаблоны, уточнение, подсказка, название по содержанию, заметки, таймаут; неверные — ошибки", async () => {
  const keys = [
    "meeting_target_lang",
    "meeting_source_lang",
    "meeting_font_size",
    "meeting_rect",
    "meeting_mini_pos",
    "meeting_autostop",
    "meeting_title_patterns",
    "meeting_refine",
    "meeting_detect_desktop_apps",
    "meeting_ignore_apps",
    "meeting_ai_title",
    "meeting_notes",
    "meeting_notes_model",
    "translate_timeout_s",
  ];
  const prev = Object.fromEntries(keys.map((k) => [k, settings[k]]));
  try {
    let s = await call("set_settings", {
      patch: {
        meeting_target_lang: "de",
        meeting_source_lang: "en",
        meeting_font_size: 20,
        meeting_rect: [100, 50, 420, 800],
        meeting_mini_pos: [1560.4, -8],
        meeting_autostop: !settings.meeting_autostop,
        meeting_title_patterns: ["Webex", "Google Meet"],
        meeting_refine: !settings.meeting_refine,
        meeting_detect_desktop_apps: !settings.meeting_detect_desktop_apps,
        meeting_ignore_apps: ["obs64.exe", "check-contract.exe"],
        meeting_ai_title: !settings.meeting_ai_title,
        meeting_notes: !settings.meeting_notes,
        meeting_notes_model: "anthropic/claude-opus-5.5",
        translate_timeout_s: 8,
      },
    });
    assert(s.meeting_target_lang === "de" && s.meeting_source_lang === "en", "языки не применились");
    assert(
      s.meeting_refine === !settings.meeting_refine && s.meeting_detect_desktop_apps === !settings.meeting_detect_desktop_apps,
      "meeting_refine / meeting_detect_desktop_apps не применились",
    );
    assert(
      Array.isArray(s.meeting_ignore_apps) && s.meeting_ignore_apps.some((x) => x.toLowerCase() === "check-contract.exe"),
      `meeting_ignore_apps: ${short(s.meeting_ignore_apps)}`,
    );
    assert(s.meeting_font_size === 20 && s.translate_timeout_s === 8, "размер/таймаут не применились");
    assert(s.meeting_ai_title === !settings.meeting_ai_title, "meeting_ai_title не применилось");
    assert(
      s.meeting_notes === !settings.meeting_notes && s.meeting_notes_model === "anthropic/claude-opus-5.5",
      "meeting_notes / meeting_notes_model не применились",
    );
    assert(Array.isArray(s.meeting_rect) && s.meeting_rect[2] === 420, short(s.meeting_rect));
    assert(
      Array.isArray(s.meeting_mini_pos) && s.meeting_mini_pos[0] === 1560 && s.meeting_mini_pos[1] === -8,
      `meeting_mini_pos: ${short(s.meeting_mini_pos)}`,
    );
    assert(s.meeting_autostop === !settings.meeting_autostop, "meeting_autostop не применилось");
    s = await call("set_settings", { patch: { meeting_rect: null, meeting_mini_pos: null, meeting_source_lang: "auto" } });
    assert(
      s.meeting_rect === null && s.meeting_mini_pos === null && s.meeting_source_lang === "auto",
      "null/auto не применились",
    );
    const errs = [];
    for (const patch of [
      { meeting_target_lang: "xx" },
      { meeting_source_lang: "klingon" },
      { meeting_font_size: 40 },
      { meeting_rect: [0, 0, 100, 100] },
      { meeting_mini_pos: [1, 2, 3] },
      { meeting_autostop: "yes" },
      { meeting_title_patterns: [""] },
      { meeting_ignore_apps: [""] },
      { meeting_refine: "yes" },
      { meeting_ai_title: "yes" },
      { meeting_notes: "yes" },
      { meeting_notes_model: "x".repeat(201) },
      { translate_timeout_s: 0 },
      { meeting_dir: "meetings" },
    ]) {
      errs.push(await callErr("set_settings", { patch }));
    }
    const now = await call("get_settings");
    assert(now.meeting_target_lang === "de" && now.meeting_font_size === 20, "неверное значение применилось");
    return `${errs.length} ошибок, например: ${errs[0]}`;
  } finally {
    await call("set_settings", { patch: prev });
  }
});

await step("set_settings с неверным значением — ошибка, настройки не меняются", async () => {
  const err = await callErr("set_settings", { patch: { insert_mode: "bogus", theme: "light" } });
  const s = await call("get_settings");
  assert(s.insert_mode === settings.insert_mode && s.theme === settings.theme, "патч применился частично");
  await callErr("set_settings", { patch: { insert_last_hotkey: "v" } });
  return err;
});
await step("pause on/off → State.paused + событие state", async () => {
  const since = Date.now();
  const a = await call("pause", { on: true });
  assert(a.paused === true && a.state === "paused", `paused=${a.paused}, state=${a.state}`);
  await c.waitEvent("state", since, 3000, (d) => d.paused === true);
  const b = await call("pause", { on: false });
  assert(b.paused === false, "пауза не снялась");
  return `state при паузе: ${a.state}`;
});
if (MIC) {
  await step("mic_monitor → события level (микрофон ~1 с, без записи)", async () => {
    const since = Date.now();
    assert((await call("mic_monitor", { on: true })) === null, "не null");
    await new Promise((r) => setTimeout(r, 1000));
    await call("mic_monitor", { on: false });
    const n = c.events.filter((e) => e.t >= since && e.event === "level").length;
    assert(n > 5, `событий level: ${n}`);
    return `${n} событий level за 1 с`;
  });
}

// Протокол
await step("неизвестная команда — ошибка на языке интерфейса", async () => callErr("no_such_command", {}));
await step("20 параллельных запросов — ответы по своим id", async () => {
  const cmds = ["get_state", "stats_get", "dict_list", "styles_list", "get_settings"];
  const all = await Promise.all(Array.from({ length: 20 }, (_, i) => call(cmds[i % cmds.length])));
  return `${all.length} ответов`;
});

// Все события, пришедшие за сеанс, — по EventMap
await step("все события сеанса соответствуют EventMap", async () => {
  const counts = {};
  const errs = [];
  for (const e of c.events) {
    counts[e.event] = (counts[e.event] ?? 0) + 1;
    const t = memberType(eventMap, e.event);
    if (!t) {
      errs.push(`неизвестное событие ${e.event}`);
      continue;
    }
    errs.push(...check(e.data, t, `event.${e.event}`, extras));
  }
  assert(!errs.length, [...new Set(errs)].slice(0, 5).join("; "));
  return Object.entries(counts)
    .map(([k, v]) => `${k}×${v}`)
    .join(", ");
});

if (QUIT) {
  await step("quit → null, ядро закрывает соединение", async () => {
    assert((await call("quit")) === null, "не null");
    const code = await Promise.race([c.closed, new Promise((r) => setTimeout(() => r("timeout"), 5000))]);
    assert(code !== "timeout", "соединение не закрылось за 5 с");
    return `закрыто с кодом ${code}`;
  });
} else {
  c.close();
}

// ——— итог ———

const failed = results.filter((r) => !r.ok);
// События EventMap, которые за сеанс не пришли, — их форма не сверена. recording с адресатами
// и notice «Не выбраны RDP-сессии» бывают только при настоящей записи с хоткея.
const seen = new Set(c.events.map((e) => e.event));
for (const n of eventMap.filter(ts.isPropertySignature).map(propName)) {
  if (!seen.has(n)) unverified.push(`событие ${n} (не пришло за сеанс)`);
}
if (!c.events.some((e) => e.event === "recording" && Array.isArray(e.data?.targets) && e.data.targets.length > 0)) {
  unverified.push("recording.targets с адресатами (нужна запись с рассылкой)");
}
if (!c.events.some((e) => e.event === "notice" && e.data?.action === "open_settings_insert")) {
  unverified.push("notice с action open_settings_insert (нужна запись с рассылкой без отмеченных сессий)");
}
const notCalled = commandMap
  .filter(ts.isPropertySignature)
  .map(propName)
  .filter((n) => !called.has(n));
console.log("");
console.log(`Итог: ${results.length - failed.length} из ${results.length} проверок прошли за ${Date.now() - t0} мс`);
if (extras.size) {
  console.log(`Поля, которых нет в types.ts (${extras.size}):`);
  for (const e of [...extras].sort()) console.log(`  ${e}`);
} else {
  console.log("Лишних полей нет: ответы и события ядра совпадают с types.ts");
}
console.log(
  `Не вызывались (намеренно — вставка, микрофонная запись, загрузки, запись ключей, окно проводника, буфер обмена): ${notCalled.join(", ")}`,
);
if (unverified.length) {
  console.log(`Не сверено с живым ядром в этом прогоне (${unverified.length}):`);
  for (const u of unverified) console.log(`  ${u}`);
}
process.exit(failed.length ? 1 : 0);
