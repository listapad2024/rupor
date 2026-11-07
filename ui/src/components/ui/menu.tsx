import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cn } from "../../lib/cn";
import { Button, type ButtonVariant } from "./controls";

export interface MenuItem {
  label: string;
  icon?: ReactNode;
  hint?: string;
  danger?: boolean;
  onSelect: () => void;
}

function menuItems(list: HTMLElement | null): HTMLButtonElement[] {
  return list ? [...list.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')] : [];
}

/**
 * Кнопка с выпадающим меню действий. С клавиатуры: при открытии фокус на первом пункте,
 * стрелки / Home / End — по пунктам, Esc и Tab закрывают и возвращают фокус на кнопку.
 */
export function Menu({
  label,
  icon,
  items,
  variant = "secondary",
  size = "md",
  width = 220,
  iconOnly = false,
}: {
  label: string;
  /** Только значок; label — подсказка и aria-label. */
  iconOnly?: boolean;
  icon?: ReactNode;
  items: MenuItem[];
  variant?: ButtonVariant;
  size?: "sm" | "md";
  width?: number;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (!open) return;
    const r = btnRef.current?.getBoundingClientRect();
    if (r) setPos({ left: Math.max(8, r.right - width), top: r.bottom + 6 });
  }, [open, width]);

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) btnRef.current?.focus({ preventScroll: true });
  };

  // Меню открылось (позиция посчитана, список в DOM) — фокус на первый пункт
  useEffect(() => {
    if (!open || !pos) return;
    const raf = requestAnimationFrame(() => menuItems(listRef.current)[0]?.focus({ preventScroll: true }));
    return () => cancelAnimationFrame(raf);
  }, [open, pos]);

  const onListKey = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const items = menuItems(listRef.current);
    if (!items.length) return;
    const i = items.findIndex((el) => el === document.activeElement);
    let next: number | null = null;
    if (e.key === "ArrowDown") next = i < 0 ? 0 : (i + 1) % items.length;
    else if (e.key === "ArrowUp") next = i < 0 ? items.length - 1 : (i - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    else if (e.key === "Tab") {
      e.preventDefault();
      close(true);
      return;
    }
    if (next === null) return;
    e.preventDefault();
    items[next]?.focus({ preventScroll: true });
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || listRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      btnRef.current?.focus({ preventScroll: true });
    };
    const close = () => setOpen(false);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
    };
  }, [open]);

  return (
    <>
      <Button
        ref={btnRef}
        variant={variant}
        size={size}
        icon={icon}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={iconOnly ? label : undefined}
        title={iconOnly ? label : undefined}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        {iconOnly ? null : label}
      </Button>
      {createPortal(
        <AnimatePresence>
          {open && pos && (
            <motion.div
              ref={listRef}
              role="menu"
              aria-label={label}
              onKeyDown={onListKey}
              initial={{ opacity: 0, y: -6, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.98 }}
              transition={{ duration: 0.14, ease: [0.22, 1, 0.36, 1] }}
              style={{ position: "fixed", left: pos.left, top: pos.top, width, transformOrigin: "top right" }}
              className="z-[80] rounded-xl border border-line-strong bg-surface p-1 shadow-pop"
            >
              {items.map((it) => (
                <button
                  key={it.label}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    // фокус — на кнопку меню: пункт сейчас исчезнет, а действие может открыть
                    // диалог, который потом вернёт фокус туда, откуда его открыли
                    close(true);
                    it.onSelect();
                  }}
                  className={cn(
                    "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13.5px] outline-none transition-colors duration-100",
                    it.danger
                      ? "text-danger hover:bg-danger/10 focus-visible:bg-danger/10"
                      : "text-fg hover:bg-surface-2 focus-visible:bg-surface-2",
                  )}
                >
                  {it.icon && <span className="flex shrink-0 opacity-80 [&>svg]:size-4">{it.icon}</span>}
                  <span className="flex-1">{it.label}</span>
                  {it.hint && <span className="text-[12px] text-subtle">{it.hint}</span>}
                </button>
              ))}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}
