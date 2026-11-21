// Заметки к встрече в окне перевода: пока их нет — что будет и кнопка «Сделать заметки
// сейчас»; пишутся — «Модель пишет заметки…» и текст по ходу; готовы — когда, какой моделью и
// почём, сами заметки (заголовки, списки, задачи с отметками, время строк — кнопкой к строке в
// расшифровке) и «Обновить»; не удались — причина и «Повторить» (прежние, если были, остаются).
import { CircleAlert, ListChecks, LoaderCircle, NotebookPen, RefreshCw, Sparkles } from "lucide-react";
import { Fragment, memo, useMemo, type ReactNode } from "react";
import { t, tp } from "../i18n";
import { cn } from "../lib/cn";
import { fmtSpend, fmtTime } from "../lib/format";
import { countTasks, parseNotes, type Block, type Inline } from "../lib/notesMd";
import type { MeetingNotes } from "../lib/types";

/** «anthropic/claude-sonnet-5.5» → «claude-sonnet-5.5»: провайдер и так виден в настройках. */
function shortModel(m: string): string {
  const i = m.lastIndexOf("/");
  return i >= 0 ? m.slice(i + 1) : m;
}

function generatedAt(iso: string | null): string | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? fmtTime(ms / 1000) : null;
}

export function NotesView({
  notes,
  hasLines,
  running,
  autoOn,
  offline,
  starting,
  onMake,
  onJump,
}: {
  notes: MeetingNotes | null;
  /** В расшифровке есть строки — заметки есть из чего сделать. */
  hasLines: boolean;
  /** Встреча идёт. */
  running: boolean;
  /** meeting_notes: заметки пишутся сами после «Стоп». */
  autoOn: boolean;
  offline: boolean;
  /** meeting_notes отправлен, ответа ещё нет. */
  starting: boolean;
  onMake: () => void;
  onJump: (ts: string) => void;
}) {
  const writing = notes?.status === "running";
  const md = notes?.markdown ?? "";
  const blocks = useMemo(() => (md ? parseNotes(md) : []), [md]);
  const busy = writing || starting;
  const canMake = hasLines && !offline && !busy;

  const make = (label: string, icon: ReactNode, primary = false) => (
    <button
      type="button"
      tabIndex={-1}
      disabled={!canMake}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onMake}
      className={cn("meeting-btn", primary ? "meeting-btn-start" : "meeting-btn-quiet")}
    >
      {busy ? <LoaderCircle className="animate-spin" /> : icon}
      {label}
    </button>
  );

  // Пишутся: строка состояния и текст по ходу (если модель уже начала)
  if (writing) {
    return (
      <div className="meeting-notes">
        <div className="meeting-notes-status" role="status">
          <LoaderCircle className="animate-spin" />
          <span>{t("meeting.notes.running")}</span>
        </div>
        {blocks.length > 0 && (
          <div className="meeting-md meeting-md-live">
            <NotesBlocks blocks={blocks} onJump={onJump} />
          </div>
        )}
      </div>
    );
  }

  const failed = notes?.status === "error";
  const errorBox = failed ? (
    <div className="meeting-hint meeting-hint-err meeting-notes-error" data-no-drag>
      <CircleAlert />
      <span className="min-w-0 flex-1">
        {t("meeting.notes.failed")}: {notes?.error ?? t("meeting.failed")}
      </span>
      <button
        type="button"
        tabIndex={-1}
        disabled={!canMake}
        onMouseDown={(e) => e.preventDefault()}
        onClick={onMake}
        className="meeting-hint-btn meeting-hint-btn-plain"
      >
        <RefreshCw />
        {t("meeting.notes.retry")}
      </button>
    </div>
  ) : null;

  // Заметок нет (или не удались, а прежних нет): что будет и кнопка
  if (!blocks.length) {
    const text = !hasLines
      ? t("meeting.notes.emptyIdle")
      : running
        ? autoOn
          ? t("meeting.notes.emptyLive")
          : t("meeting.notes.emptyLiveOff")
        : t("meeting.notes.emptyStopped");
    return (
      <div className="meeting-notes">
        {errorBox}
        <div className="meeting-empty">
          <span className="meeting-empty-icon">
            <NotebookPen />
          </span>
          <div className="text-[14px] font-semibold text-fg">{t("meeting.notes.emptyTitle")}</div>
          <p className="max-w-[300px] text-[12.5px] leading-relaxed text-muted">{text}</p>
          {hasLines && !failed && <div className="mt-2">{make(t("meeting.notes.make"), <Sparkles />, true)}</div>}
        </div>
      </div>
    );
  }

  // Готовы: когда, чем, почём; сами заметки; обновить
  const at = generatedAt(notes?.generated_at ?? null);
  const tasks = countTasks(blocks);
  const meta = [
    at ? t("meeting.notes.generated", { time: at }) : null,
    notes?.model ? shortModel(notes.model) : null,
    notes?.cost_usd !== null && notes?.cost_usd !== undefined ? fmtSpend(notes.cost_usd) : null,
  ].filter((x): x is string => Boolean(x));
  return (
    <div className="meeting-notes">
      {errorBox}
      <div className="meeting-notes-bar" data-no-drag>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[11.5px] text-subtle" title={notes?.model ?? undefined}>
            {meta.join(" · ")}
          </div>
          {tasks.total > 0 && (
            <div className="mt-0.5 flex items-center gap-1 text-[11.5px] text-muted">
              <ListChecks className="size-3.5" />
              {tp("meeting.notes.tasks", tasks.total)}
            </div>
          )}
        </div>
        {make(t("meeting.notes.refresh"), <RefreshCw />)}
      </div>
      {running && <div className="meeting-notes-stale">{t(autoOn ? "meeting.notes.staleAuto" : "meeting.notes.stale")}</div>}
      <div className="meeting-md">
        <NotesBlocks blocks={blocks} onJump={onJump} />
      </div>
    </div>
  );
}

const NotesBlocks = memo(function NotesBlocks({ blocks, onJump }: { blocks: Block[]; onJump: (ts: string) => void }) {
  return (
    <>
      {blocks.map((b, i) => {
        if (b.k === "h") {
          const Tag = b.level === 1 ? "h2" : b.level === 2 ? "h3" : "h4";
          return (
            <Tag key={i} className={`meeting-md-h${b.level}`}>
              <Inlines c={b.c} onJump={onJump} />
            </Tag>
          );
        }
        if (b.k === "hr") return <hr key={i} />;
        if (b.k === "p") {
          return (
            <p key={i}>
              <Inlines c={b.c} onJump={onJump} />
            </p>
          );
        }
        return (
          <ul key={i}>
            {b.items.map((it, j) => (
              <li
                key={j}
                className={cn(it.check !== null && "meeting-md-task", it.check && "meeting-md-done")}
                style={it.depth ? { marginLeft: `${it.depth * 16}px` } : undefined}
              >
                {it.check !== null && (
                  <span className={cn("meeting-md-box", it.check && "meeting-md-box-on")} aria-hidden>
                    {it.check && (
                      <svg viewBox="0 0 12 12">
                        <path d="M2.5 6.2 5 8.6l4.6-5" />
                      </svg>
                    )}
                  </span>
                )}
                <span className="min-w-0">
                  <Inlines c={it.c} onJump={onJump} />
                </span>
              </li>
            ))}
          </ul>
        );
      })}
    </>
  );
});

function Inlines({ c, onJump }: { c: Inline[]; onJump: (ts: string) => void }) {
  return (
    <>
      {c.map((x, i) => {
        switch (x.k) {
          case "text":
            return <Fragment key={i}>{x.v}</Fragment>;
          case "b":
            return (
              <strong key={i}>
                <Inlines c={x.c} onJump={onJump} />
              </strong>
            );
          case "i":
            return (
              <em key={i}>
                <Inlines c={x.c} onJump={onJump} />
              </em>
            );
          case "code":
            return <code key={i}>{x.v}</code>;
          case "time":
            return (
              <button
                key={i}
                type="button"
                tabIndex={-1}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => onJump(x.v)}
                className="meeting-md-time tabular"
                title={t("meeting.notes.jump", { time: x.v })}
              >
                {x.v}
              </button>
            );
        }
      })}
    </>
  );
}
