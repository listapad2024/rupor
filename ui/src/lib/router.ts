// Простой hash-роутер: #/history, #/settings/ai …
import { useCallback, useEffect, useState, type RefObject } from "react";

export const ROUTES = [
  "home",
  "history",
  "speech",
  "dictionary",
  "snippets",
  "styles",
  "apps",
  "settings",
  "help",
  "wizard",
] as const;

export type Route = (typeof ROUTES)[number];

export interface Location {
  route: Route;
  sub: string | null;
}

export function parseHash(hash: string): Location {
  const clean = hash.replace(/^#\/?/, "");
  const [head, sub] = clean.split("/");
  const route = (ROUTES as readonly string[]).includes(head ?? "") ? (head as Route) : "home";
  return { route, sub: sub || null };
}

export function navigate(to: string): void {
  const target = `#/${to.replace(/^[#/]+/, "")}`;
  if (window.location.hash !== target) window.location.hash = target;
}

export function useLocation(): Location & { navigate: (to: string) => void } {
  const [loc, setLoc] = useState<Location>(() => parseHash(window.location.hash));
  useEffect(() => {
    const on = () => setLoc(parseHash(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = useCallback((to: string) => navigate(to), []);
  return { ...loc, navigate: go };
}

/** Третья часть адреса: #/settings/insert/sessions → "sessions". */
export function hashAnchor(hash = window.location.hash): string | null {
  const parts = hash.replace(/^#\/?/, "").split("/");
  return parts[2] || null;
}

/**
 * Прокрутить к блоку, если адрес ведёт на него (#/settings/insert/sessions — уведомление
 * «Не выбраны RDP-сессии»). Возвращает true на ~1,6 с — блок можно подсветить.
 */
export function useHashAnchor(anchor: string, ref: RefObject<HTMLElement | null>): boolean {
  const [flash, setFlash] = useState(false);
  useEffect(() => {
    let t1: ReturnType<typeof setTimeout> | null = null;
    let t2: ReturnType<typeof setTimeout> | null = null;
    const check = () => {
      if (hashAnchor() !== anchor) return;
      if (t1) clearTimeout(t1);
      if (t2) clearTimeout(t2);
      // после анимации смены страницы/вкладки
      t1 = setTimeout(() => {
        ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
        setFlash(true);
        t2 = setTimeout(() => setFlash(false), 1600);
      }, 260);
    };
    check();
    window.addEventListener("hashchange", check);
    return () => {
      window.removeEventListener("hashchange", check);
      if (t1) clearTimeout(t1);
      if (t2) clearTimeout(t2);
    };
  }, [anchor, ref]);
  return flash;
}
