import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown } from "lucide-react";
import {
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { cn } from "../../lib/cn";
import { ModalContext } from "./overlay";

export interface SelectOption<T> {
  value: T;
  label: string;
  hint?: string;
  icon?: ReactNode;
}

interface Pos {
  left: number;
  top: number;
  width: number;
  up: boolean;
  maxH: number;
}

/** Выпадающий список в стиле приложения (порталом — не обрезается прокруткой). */
export function Select<T extends string | number | null>({
  value,
  options,
  onChange,
  placeholder,
  className,
  size = "md",
  disabled,
  minWidth = 180,
  align = "left",
}: {
  value: T;
  options: SelectOption<T>[];
  onChange: (v: T) => void;
  placeholder?: string;
  className?: string;
  size?: "sm" | "md";
  disabled?: boolean;
  minWidth?: number;
  align?: "left" | "right";
}) {
  const inModal = useContext(ModalContext);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [pos, setPos] = useState<Pos | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const current = options.find((o) => o.value === value);

  const place = useCallback(() => {
    const b = btnRef.current?.getBoundingClientRect();
    if (!b) return;
    const width = Math.max(b.width, minWidth);
    const below = window.innerHeight - b.bottom - 12;
    const above = b.top - 12;
    const want = Math.min(320, options.length * 36 + 12);
    const up = below < Math.min(want, 200) && above > below;
    const left = align === "right" ? Math.max(8, b.right - width) : Math.min(b.left, window.innerWidth - width - 8);
    setPos({
      left,
      top: up ? b.top - 6 : b.bottom + 6,
      width,
      up,
      maxH: Math.max(120, Math.min(320, up ? above : below)),
    });
  }, [align, minWidth, options.length]);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const idx = options.findIndex((o) => o.value === value);
    setActive(idx >= 0 ? idx : 0);
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || listRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onScroll = (e: Event) => {
      if (listRef.current && e.target instanceof Node && listRef.current.contains(e.target)) return;
      setOpen(false);
    };
    const onResize = () => setOpen(false);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const choose = (o: SelectOption<T>) => {
    onChange(o.value);
    setOpen(false);
    btnRef.current?.focus();
  };

  const onKey = (e: ReactKeyboardEvent) => {
    if (disabled) return;
    if (!open && (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      setOpen(true);
      return;
    }
    if (!open) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(options.length - 1, a + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const o = options[active];
      if (o) choose(o);
    } else if (e.key === "Escape" || e.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={onKey}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={cn(
          "group inline-flex items-center justify-between gap-2 rounded-[10px] border border-line bg-surface-2 text-left text-fg",
          "transition-[border-color,background-color,box-shadow] duration-150 hover:border-line-strong",
          "focus-visible:border-accent/60 focus-visible:shadow-glow focus-visible:outline-none",
          "disabled:opacity-50",
          open && "border-accent/60 bg-surface shadow-glow",
          size === "sm" ? "h-8 pl-2.5 pr-2 text-[13px]" : "h-9 pl-3 pr-2.5 text-[13.5px]",
          className,
        )}
      >
        <span className="flex min-w-0 items-center gap-2">
          {current?.icon && <span className="flex shrink-0 text-muted [&>svg]:size-4">{current.icon}</span>}
          <span className={cn("truncate", !current && "text-subtle")}>{current?.label ?? placeholder}</span>
        </span>
        <ChevronDown
          className={cn("size-4 shrink-0 text-subtle transition-transform duration-200", open && "rotate-180")}
        />
      </button>
      {createPortal(
        <AnimatePresence>
          {open && pos && (
            <motion.div
              ref={listRef}
              role="listbox"
              initial={{ opacity: 0, y: pos.up ? 6 : -6, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: pos.up ? 4 : -4, scale: 0.98 }}
              transition={{ duration: 0.14, ease: [0.22, 1, 0.36, 1] }}
              style={{
                position: "fixed",
                left: pos.left,
                width: pos.width,
                maxHeight: pos.maxH,
                ...(pos.up ? { bottom: window.innerHeight - pos.top } : { top: pos.top }),
                transformOrigin: pos.up ? "bottom" : "top",
              }}
              // Only modal lists go above Modal (90); background lists stay below its backdrop.
              className={cn(
                inModal ? "z-[95]" : "z-[80]",
                "overflow-y-auto rounded-xl border border-line-strong bg-surface p-1 shadow-pop",
              )}
            >
              {options.map((o, i) => {
                const selected = o.value === value;
                return (
                  <button
                    key={String(o.value)}
                    type="button"
                    data-idx={i}
                    role="option"
                    aria-selected={selected}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => choose(o)}
                    className={cn(
                      "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13.5px] transition-colors duration-100",
                      i === active ? "bg-surface-2" : "",
                    )}
                  >
                    {o.icon && <span className="flex shrink-0 text-muted [&>svg]:size-4">{o.icon}</span>}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-fg">{o.label}</span>
                      {o.hint && <span className="block truncate text-[12px] text-subtle">{o.hint}</span>}
                    </span>
                    {selected && <Check className="size-4 shrink-0 text-accent-text" />}
                  </button>
                );
              })}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}
