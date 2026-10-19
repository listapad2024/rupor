import { AnimatePresence, motion } from "framer-motion";
import { TriangleAlert, X } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { t } from "../../i18n";
import { cn } from "../../lib/cn";
import { Button } from "./controls";

// ——— Модальное окно ———

export const ModalContext = createContext(false);

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.getClientRects().length > 0);
}

/**
 * Модальное окно. Фокус: при открытии — внутрь (autoFocus или первый элемент),
 * Tab/Shift+Tab не выходят за пределы окна, при закрытии фокус возвращается туда,
 * откуда окно открыли.
 */
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = 440,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  // onClose часто стрелочная функция: в ref, чтобы перерисовка не перезапускала эффект
  // (а с ним — возврат фокуса).
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const raf = requestAnimationFrame(() => {
      const panel = panelRef.current;
      if (!panel || panel.contains(document.activeElement)) return;
      // крестик — последний выбор: начинаем с содержимого окна
      const els = focusables(panel);
      (els.find((el) => el.dataset.modalClose === undefined) ?? els[0] ?? panel).focus({ preventScroll: true });
    });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const els = focusables(panel);
      const first = els[0];
      const last = els[els.length - 1];
      if (!first || !last) {
        e.preventDefault();
        panel.focus({ preventScroll: true });
        return;
      }
      const active = document.activeElement;
      const inside = active instanceof Node && panel.contains(active);
      if (e.shiftKey && (active === first || !inside)) {
        e.preventDefault();
        last.focus({ preventScroll: true });
      } else if (!e.shiftKey && (active === last || !inside)) {
        e.preventDefault();
        first.focus({ preventScroll: true });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("keydown", onKey);
      if (prev && prev.isConnected) prev.focus({ preventScroll: true });
    };
  }, [open]);

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[90] flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }}
        >
          <div className="absolute inset-0 bg-black/35 backdrop-blur-[3px]" onMouseDown={onClose} />
          <motion.div
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={title ? titleId : undefined}
            tabIndex={-1}
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 4 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            style={{ width }}
            className="relative max-w-full rounded-2xl border border-line-strong bg-surface p-6 shadow-pop outline-none"
          >
            <ModalContext.Provider value={true}>
              <button
                type="button"
                data-modal-close=""
                onClick={onClose}
                aria-label={t("common.close")}
                className="absolute right-3 top-3 flex size-8 items-center justify-center rounded-lg text-subtle hover:bg-surface-2 hover:text-fg"
              >
                <X className="size-4" />
              </button>
              {title && (
                <div id={titleId} className="mb-2 pr-8 text-[17px] font-semibold tracking-[-0.01em] text-fg">
                  {title}
                </div>
              )}
              <div className="text-[13.5px] leading-relaxed text-muted">{children}</div>
              {footer && <div className="mt-6 flex justify-end gap-2">{footer}</div>}
            </ModalContext.Provider>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

// ——— Подтверждение ———

export interface ConfirmOptions {
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

type ConfirmFn = (o: ConfirmOptions) => Promise<boolean>;
const ConfirmCtx = createContext<ConfirmFn>(() => Promise.resolve(false));

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const confirm = useCallback<ConfirmFn>((o) => {
    setOpts(o);
    return new Promise<boolean>((res) => {
      resolver.current = res;
    });
  }, []);

  const done = (v: boolean) => {
    resolver.current?.(v);
    resolver.current = null;
    setOpts(null);
  };

  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      <Modal
        open={opts !== null}
        onClose={() => done(false)}
        title={
          opts && (
            <span className="flex items-center gap-2.5">
              {opts.danger && (
                <span className="flex size-8 items-center justify-center rounded-full bg-danger/12 text-danger">
                  <TriangleAlert className="size-4" />
                </span>
              )}
              {opts.title}
            </span>
          )
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => done(false)}>
              {opts?.cancelLabel ?? t("common.cancel")}
            </Button>
            <Button variant={opts?.danger ? "danger-solid" : "primary"} onClick={() => done(true)} autoFocus>
              {opts?.confirmLabel ?? t("common.confirm")}
            </Button>
          </>
        }
      >
        {opts?.body}
      </Modal>
    </ConfirmCtx.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  return useContext(ConfirmCtx);
}

// ——— Подсказка при наведении ———

export function Tooltip({
  content,
  children,
  side = "top",
  className,
}: {
  content: ReactNode;
  children: ReactElement;
  side?: "top" | "bottom" | "right";
  className?: string;
}) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const show = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      const r = ref.current?.getBoundingClientRect();
      if (!r) return;
      if (side === "right") setPos({ x: r.right + 8, y: r.top + r.height / 2 });
      else setPos({ x: r.left + r.width / 2, y: side === "top" ? r.top - 8 : r.bottom + 8 });
    }, 350);
  };
  const hide = () => {
    if (timer.current) clearTimeout(timer.current);
    setPos(null);
  };
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  return (
    <span
      ref={ref}
      className={cn("inline-flex", className)}
      onMouseEnter={show}
      onMouseLeave={hide}
      onMouseDown={hide}
    >
      {children}
      {createPortal(
        <AnimatePresence>
          {pos && (
            <motion.div
              initial={{ opacity: 0, scale: 0.96 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.96 }}
              transition={{ duration: 0.12 }}
              style={{
                position: "fixed",
                left: pos.x,
                top: pos.y,
                translateX: side === "right" ? "0%" : "-50%",
                translateY: side === "top" ? "-100%" : side === "right" ? "-50%" : "0%",
              }}
              className="pointer-events-none z-[100] max-w-xs rounded-lg bg-[#18181d] px-2.5 py-1.5 text-[12px] font-medium text-white shadow-pop dark:bg-[#2a2a31]"
            >
              {content}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </span>
  );
}
