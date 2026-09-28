import { useEffect, type RefObject } from "react";
import { getLevel } from "./motion";

/** 原生滚动驱动首页章节进度；全站指针追视由 useGlobalScrollMotion 统一处理。 */
export function useHomeScroll(ref: RefObject<HTMLDivElement>, ready: boolean, mode: string) {
  useEffect(() => {
    const page = ref.current;
    if (!page || !ready) return;
    const chapters = [...page.querySelectorAll<HTMLElement>("[data-scroll-chapter]")];
    const panels = [...page.querySelectorAll<HTMLElement>("[data-scroll-panel]")];
    const nav = document.querySelector<HTMLElement>(".topbar");
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let frame = 0;
    const update = () => {
      frame = 0;
      if (document.hidden) return;
      const full = getLevel() === "full" && !reduced.matches && window.innerWidth >= 900;
      const height = window.innerHeight;
      const progress = chapters.map((section) => {
        const rect = section.getBoundingClientRect();
        return Math.max(0, Math.min(1, (height * 0.68 - rect.top) / Math.max(rect.height, 1)));
      });
      const active = panels.map((panel) => {
        const rect = panel.getBoundingClientRect();
        return rect.top < height * 0.74 && rect.bottom > height * 0.26;
      });
      page.dataset.scrollMotion = full ? "full" : "static";
      if (nav) nav.dataset.homeScrolled = String(window.scrollY > 24);
      chapters.forEach((section, i) => section.style.setProperty("--chapter-progress", String(full ? progress[i] : 1)));
      panels.forEach((panel, i) => panel.dataset.inView = String(!full || active[i]));
    };
    const schedule = () => {
      if (!frame && !document.hidden) frame = window.requestAnimationFrame(update);
    };
    const onVisibility = () => {
      if (document.hidden) { cancelAnimationFrame(frame); frame = 0; }
      else schedule();
    };
    const motionObserver = new MutationObserver(schedule);
    motionObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-motion"] });
    const resizeObserver = new ResizeObserver(schedule);
    resizeObserver.observe(page);
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule, { passive: true });
    document.addEventListener("visibilitychange", onVisibility);
    reduced.addEventListener("change", schedule);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      motionObserver.disconnect();
      resizeObserver.disconnect();
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      document.removeEventListener("visibilitychange", onVisibility);
      reduced.removeEventListener("change", schedule);
      if (nav) delete nav.dataset.homeScrolled;
    };
  }, [ref, ready, mode]);
}
