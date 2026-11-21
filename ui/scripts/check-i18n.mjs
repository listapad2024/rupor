// Проверка строк интерфейса (npm run check:i18n):
//  1. src/i18n/ru.json и en.json — одинаковые ключи (паритет). Узлы множественного числа
//     сверяются по категориям Intl.PluralRules своей локали: ru — one/few/many (+other),
//     en — one/other; недостающие и невозможные формы — ошибка.
//  2. Подстановки {name} в переводе те же, что в русской строке (и во всех формах).
//  3. В en.json нет кириллицы (самоназвание «Русский» живёт в src/i18n/index.ts).
//  4. В src/**/*.ts(x), кроме src/i18n, нет кириллицы в строках, шаблонах и JSX-тексте.
//     Комментарии не проверяются — они по правилам проекта пишутся по-русски.
//  5. Демо-режим говорит текстами ядра: стили и уведомления в src/lib/mock-data.json и
//     mock-data.en.json совпадают с core/rupor/locales/ru.json и en.json (проверки вёрстки и
//     скриншоты в демо-режиме должны видеть настоящие длины строк). Ядра рядом нет — пропуск.
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = fileURLToPath(new URL("..", import.meta.url));
const src = join(root, "src");
const CYR = /[Ѐ-ӿ]/;
const PLURAL_FORMS = new Set(["zero", "one", "two", "few", "many", "other"]);
const LOCALES = { ru: "ru-RU", en: "en-US" };

const problems = [];

// ——— 1–3. Каталоги ———

function load(lang) {
  try {
    return JSON.parse(readFileSync(join(src, "i18n", `${lang}.json`), "utf-8"));
  } catch (e) {
    problems.push(`src/i18n/${lang}.json: не читается как JSON — ${e.message}`);
    return null;
  }
}

const isObj = (v) => typeof v === "object" && v !== null && !Array.isArray(v);
const isPluralNode = (v) => isObj(v) && Object.keys(v).length > 0 && Object.keys(v).every((k) => PLURAL_FORMS.has(k));
const placeholders = (s) =>
  [...new Set([...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]))].sort().join(",");

/** Листья каталога: путь → строка; узел множественного числа — путь → {форма: строка}. */
function flatten(node, prefix, out, lang) {
  for (const [k, v] of Object.entries(node)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (typeof v === "string" || isPluralNode(v)) out.set(path, v);
    else if (isObj(v)) flatten(v, path, out, lang);
    else problems.push(`src/i18n/${lang}.json: ${path} — не строка и не объект`);
  }
  return out;
}

const pluralCats = Object.fromEntries(
  Object.entries(LOCALES).map(([l, tag]) => [l, new Intl.PluralRules(tag).resolvedOptions().pluralCategories]),
);

const catalogs = Object.fromEntries(Object.keys(LOCALES).map((l) => [l, load(l)]));
if (catalogs.ru && catalogs.en) {
  const flat = Object.fromEntries(Object.entries(catalogs).map(([l, c]) => [l, flatten(c, "", new Map(), l)]));
  const base = flat.ru;
  let strings = 0;
  const before = problems.length;
  for (const [lang, map] of Object.entries(flat)) {
    for (const key of base.keys()) if (!map.has(key)) problems.push(`src/i18n/${lang}.json: нет ключа ${key}`);
    for (const key of map.keys()) if (!base.has(key)) problems.push(`src/i18n/${lang}.json: лишний ключ ${key} (его нет в ru.json)`);
    for (const [key, v] of map) {
      const ref = base.get(key);
      if (ref === undefined) continue;
      const plural = isObj(v);
      if (plural !== isObj(ref)) {
        problems.push(`src/i18n/${lang}.json: ${key} — ${plural ? "формы множественного числа" : "строка"}, а в ru.json — нет`);
        continue;
      }
      if (plural) {
        const need = pluralCats[lang];
        for (const c of need) if (!(c in v)) problems.push(`src/i18n/${lang}.json: ${key} — нет формы «${c}»`);
        for (const c of Object.keys(v)) {
          if (c !== "other" && !need.includes(c)) problems.push(`src/i18n/${lang}.json: ${key} — формы «${c}» в ${lang} не бывает`);
        }
      }
      const refPh = placeholders(plural ? (ref.other ?? Object.values(ref)[0]) : ref);
      for (const [form, s] of plural ? Object.entries(v) : [["", v]]) {
        strings++;
        const where = form ? `${key}.${form}` : key;
        if (typeof s !== "string" || !s.trim()) {
          problems.push(`src/i18n/${lang}.json: ${where} — пустая строка`);
          continue;
        }
        if (placeholders(s) !== refPh) {
          problems.push(`src/i18n/${lang}.json: ${where} — подстановки {${placeholders(s)}}, а в ru.json {${refPh}}`);
        }
        if (lang === "en" && CYR.test(s)) problems.push(`src/i18n/en.json: ${where} — кириллица: ${s.slice(0, 60)}`);
      }
    }
  }
  if (problems.length === before) {
    console.log(`OK: ru.json и en.json — ключей: ${base.size}, строк: ${strings}; паритет, формы и подстановки совпадают`);
  }
}

// ——— 4. Кириллица в коде ———

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (relative(src, p).replace(/\\/g, "/") === "i18n") continue;
      walk(p, out);
    } else if (/\.(ts|tsx)$/.test(name)) {
      out.push(p);
    }
  }
  return out;
}

const codeProblems = [];
for (const file of walk(src)) {
  const text = readFileSync(file, "utf-8");
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const visit = (node) => {
    const k = node.kind;
    if (
      k === ts.SyntaxKind.StringLiteral ||
      k === ts.SyntaxKind.NoSubstitutionTemplateLiteral ||
      k === ts.SyntaxKind.TemplateHead ||
      k === ts.SyntaxKind.TemplateMiddle ||
      k === ts.SyntaxKind.TemplateTail ||
      k === ts.SyntaxKind.JsxText ||
      k === ts.SyntaxKind.RegularExpressionLiteral ||
      k === ts.SyntaxKind.Identifier
    ) {
      const s = node.getText(sf);
      if (CYR.test(s)) {
        const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
        codeProblems.push(`${relative(root, file)}:${line + 1}: ${s.trim().slice(0, 80)}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
}

if (codeProblems.length) {
  problems.push("Кириллица вне src/i18n (строки интерфейса должны быть в ru.json и en.json):");
  problems.push(...codeProblems.map((p) => "  " + p));
} else {
  console.log("OK: кириллицы в строках и JSX вне src/i18n нет");
}

// ——— 5. Демо-данные ↔ тексты ядра ———
// node scripts/check-i18n.mjs --fix — переписать эти тексты в mock-data*.json из ядра.

/**
 * Ключ texts в mock-data*.json → ключ core/rupor/locales/*.json. У ядра формы множественного
 * числа — отдельные ключи «key.one», «key.few»…; в моке это объект {one, few, …}.
 */
const MOCK_CORE = {
  noticeAdmin: "eng.window_admin",
  noticeAi: "eng.ai_no_answer",
  aiTailInserted: "eng.ai_tail_inserted",
  noticeSelect: "ai.command_no_selection",
  insertFailed: "eng.insert_failed",
  insertError: "inj.focus_lost",
  noTargets: "eng.rdp_none",
  sentRdp: "eng.rdp_sent",
  learnAdded: "learn.added_variant",
  learnAddedTerm: "learn.added",
  learnSuggest: "learn.suggest_variant",
  learnSuggestTerm: "learn.suggest",
  balanceLow: "eng.low_balance",
  unknownStyle: "api.unknown_style",
  noKey: "api.openrouter_key_missing",
  envEmpty: "api.env_path_missing",
  noGpu: "eng.no_gpu",
  // папка модели и подбор модели (model_dir_set, model_recommend)
  modelDirLabel: "setting.model_dir",
  modelDirRelative: "cfg.path_not_absolute",
  modelBusy: "eng.model_dir_busy",
  progressMove: "fetch.move_progress_mb",
  modelMoved: "eng.model_moved",
  moveNoSpace: "fetch.move_no_space",
  recommendGpu: "rec.reason_gpu_fast",
  recommendCpu: "rec.reason_cpu_small",
  recThreads: "rec.threads",
  recRam: "rec.ram",
  noteTiny: "rec.note_tiny",
  noteBase: "rec.note_base",
  noteSmall: "rec.note_small",
  noteMedium: "rec.note_medium",
  noteTurbo: "rec.note_turbo",
  noteLarge: "rec.note_large",
  // перевод встречи (подсказка о встрече, ошибки meeting_start / meeting_copy)
  meetingSuggest: "meeting.suggest",
  meetingNoSource: "meeting.no_source",
  meetingUnknownSource: "meeting.source_not_found",
  meetingNothingToCopy: "meeting.nothing_to_copy",
  // Windows 10: встреча с программой — по всему звуку компьютера (notice ядра)
  meetingLoopbackFallback: "meeting.loopback_fallback",
  // встреча кончилась — автоостановка (notice ядра)
  meetingAutostoppedNotes: "meeting.autostopped_notes",
  meetingAutostopped: "meeting.autostopped_saved",
  // название записи без ИИ, когда ни окна встречи, ни строк (MeetingStatus.title)
  meetingTitleGeneric: "meeting.title_generic",
  // предложение встречи (meeting_suggest_answer)
  meetingSuggestExpired: "meeting.suggest_expired",
  suggestAnswer: "api.suggest_answer",
  // заметки к встрече (meeting_notes, meeting_copy {what}) и их ошибка без ключа
  meetingNotesEmpty: "meeting.notes_empty",
  meetingNotesNoMeeting: "meeting.notes_no_meeting",
  meetingNotesNothing: "meeting.notes_nothing_to_copy",
  meetingNotesFailed: "meeting.notes_failed",
  copyWhat: "api.copy_what",
  aiNoKey: "ai.no_key",
};
const MOCK_FILES = { ru: "mock-data.json", en: "mock-data.en.json" };
const coreDir = join(root, "..", "core", "rupor", "locales");
const fix = process.argv.includes("--fix");

/** Текст ядра по ключу: строка, или {форма: строка} для ключей множественного числа, или undefined. */
function coreText(core, key) {
  if (typeof core[key] === "string") return core[key];
  const forms = Object.fromEntries(
    Object.entries(core)
      .filter(([k, v]) => k.startsWith(`${key}.`) && PLURAL_FORMS.has(k.slice(key.length + 1)) && typeof v === "string")
      .map(([k, v]) => [k.slice(key.length + 1), v]),
  );
  return Object.keys(forms).length ? forms : undefined;
}

const canon = (v) => (isObj(v) ? JSON.stringify(Object.fromEntries(Object.entries(v).sort())) : JSON.stringify(v));

if (!existsSync(coreDir)) {
  console.log("Пропуск: нет core/rupor/locales — демо-данные с ядром не сверяются");
} else {
  const before = problems.length;
  let checked = 0;
  const mocks = {};
  for (const [lang, file] of Object.entries(MOCK_FILES)) {
    const where = `src/lib/${file}`;
    const path = join(src, "lib", file);
    let core;
    let text;
    try {
      text = readFileSync(path, "utf-8");
      mocks[lang] = JSON.parse(text);
      core = JSON.parse(readFileSync(join(coreDir, `${lang}.json`), "utf-8"));
    } catch (e) {
      problems.push(`${where} или core/rupor/locales/${lang}.json: не читается — ${e.message}`);
      continue;
    }
    const mock = mocks[lang];
    const pairs = [
      ...Object.entries(MOCK_CORE).map(([k, key]) => [`texts.${k}`, mock.texts ?? {}, k, key]),
      ...(mock.styles ?? []).flatMap((st) => ["name", "description"].map((f) => [`styles.${st.id}.${f}`, st, f, `ai.style.${st.id}.${f}`])),
    ];
    let fixed = 0;
    for (const [what, obj, field, key] of pairs) {
      checked++;
      const want = coreText(core, key);
      if (want === undefined) {
        problems.push(`core/rupor/locales/${lang}.json: нет ключа ${key} (нужен для ${where} ${what})`);
      } else if (canon(obj[field]) !== canon(want)) {
        if (fix) {
          obj[field] = want;
          fixed++;
        } else {
          problems.push(`${where}: ${what} не совпадает с ядром (${key}):\n    мок:  ${canon(obj[field])}\n    ядро: ${canon(want)}`);
        }
      }
    }
    if (fixed) {
      writeFileSync(path, JSON.stringify(mock, null, 2) + (text.endsWith("\n") ? "\n" : ""), "utf-8");
      console.log(`${where}: из ядра обновлено строк: ${fixed}`);
    }
    const coreStyles = Object.keys(core).filter((k) => /^ai\.style\.\w+\.name$/.test(k)).map((k) => k.split(".")[2]);
    const mockStyles = (mock.styles ?? []).map((st) => st.id);
    if (coreStyles.sort().join() !== [...mockStyles].sort().join()) {
      problems.push(`${where}: стили ${mockStyles.join(", ")}, а у ядра ${coreStyles.join(", ")}`);
    }
  }
  if (mocks.ru && mocks.en) {
    const ru = Object.keys(mocks.ru.texts ?? {});
    const en = Object.keys(mocks.en.texts ?? {});
    for (const k of ru) if (!en.includes(k)) problems.push(`src/lib/mock-data.en.json: нет texts.${k}`);
    for (const k of en) if (!ru.includes(k)) problems.push(`src/lib/mock-data.json: нет texts.${k}`);
    if (CYR.test(JSON.stringify(mocks.en.texts ?? {}))) problems.push("src/lib/mock-data.en.json: кириллица в texts");
  }
  if (problems.length === before) {
    console.log(`OK: демо-данные говорят текстами ядра — сверено строк: ${checked}`);
  } else if (!fix) {
    problems.push("  (тексты ядра поменялись? node scripts/check-i18n.mjs --fix перепишет их в демо-данные)");
  }
}

if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
