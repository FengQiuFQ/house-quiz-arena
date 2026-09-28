import { useEffect, type RefObject } from "react";
import { getLevel } from "./motion";

const MOTION_TARGETS = [
  ":scope > *",
  "section",
  "article",
  "h1",
  "h2",
  ".section-head",
  ".section-title",
  ".house-card",
  ".feature",
  ".panel-card",
  ".card",
  ".comp-banner",
  ".info-box",
  ".match-row",
  ".result-card",
  ".phase-card",
  ".stat-card",
  ".player",
  ".watch-grid",
  ".watch-stage",
  ".watch-side-col",
  ".match-meta",
  ".screen-page",
  ".rp-panel",
  ".admin-panel",
].join(",");

const POINTER_SURFACES = ".house-card, .feature, .panel-card";

/** 全站滚动叙事：按进入方向显影，桌面指针只点亮当前卡面。 */
export function useGlobalScrollMotion(
  rootRef: RefObject<HTMLElement>,
  traceRef: RefObject<HTMLDivElement>,
  routeKey: string,
) {
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
    const observed = new Set<HTMLElement>();
    const inView = new WeakMap<HTMLElement, boolean>();
    let direction: "down" | "up" = window.scrollY > 0 ? "up" : "down";
    let lastY = window.scrollY;
    let scrollFrame = 0;
    let pointerFrame = 0;
    let activeSurface: HTMLElement | null = null;
    let pointerX = 0;
    let pointerY = 0;

    const clearPointer = () => {
      if (pointerFrame) cancelAnimationFrame(pointerFrame);
      pointerFrame = 0;
      if (activeSurface) delete activeSurface.dataset.pointer;
      activeSurface = null;
    };

    const revealObserver = new IntersectionObserver((entries) => {
      if (getLevel() !== "full" || reduced.matches) {
        entries.forEach(({ target }) => {
          (target as HTMLElement).dataset.scrollMotion = "shown";
        });
        return;
      }

      entries.forEach((entry) => {
        const el = entry.target as HTMLElement;
        const wasInView = inView.get(el) ?? false;
        inView.set(el, entry.isIntersecting);
        if (entry.isIntersecting && !wasInView) {
          el.dataset.scrollDirection = direction;
          el.style.setProperty("--scroll-delay", `${Math.min((observed.size % 4) * 38, 114)}ms`);
          el.dataset.scrollMotion = "entering";
        } else if (!entry.isIntersecting && wasInView && el.dataset.scrollMotion !== "entering") {
          el.dataset.scrollMotion = "idle";
        }
      });
    }, { threshold: 0, rootMargin: "0px 0px -12% 0px" });

    const collectTargets = () => {
      if (getLevel() !== "full" || reduced.matches) {
        root.querySelectorAll<HTMLElement>(MOTION_TARGETS).forEach((el) => {
          if (!el.closest(".reveal")) el.dataset.scrollMotion = "shown";
        });
        clearPointer();
        return;
      }

      const current = new Set(
        [...root.querySelectorAll<HTMLElement>(MOTION_TARGETS)].filter((el) => !el.closest(".reveal")),
      );
      observed.forEach((el) => {
        if (!current.has(el)) {
          revealObserver.unobserve(el);
          observed.delete(el);
        }
      });
      current.forEach((el) => {
        if (observed.has(el)) return;
        el.dataset.scrollMotion = "idle";
        observed.add(el);
        revealObserver.observe(el);
      });
    };

    const updateScroll = () => {
      scrollFrame = 0;
      if (document.hidden) return;
      const y = window.scrollY;
      if (Math.abs(y - lastY) > 1) direction = y > lastY ? "down" : "up";
      lastY = y;
      document.documentElement.dataset.scrollDirection = direction;

      const maxScroll = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
      traceRef.current?.style.setProperty("--scroll-progress", String(Math.max(0, Math.min(1, y / maxScroll))));
      if (getLevel() !== "full" || reduced.matches || !finePointer.matches) clearPointer();
    };
    const scheduleScroll = () => {
      if (!scrollFrame && !document.hidden) scrollFrame = requestAnimationFrame(updateScroll);
    };
    const onScroll = () => {
      const y = window.scrollY;
      if (Math.abs(y - lastY) > 1) direction = y > lastY ? "down" : "up";
      lastY = y;
      document.documentElement.dataset.scrollDirection = direction;
      scheduleScroll();
    };
    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType !== "mouse" || !finePointer.matches || getLevel() !== "full" || reduced.matches) {
        clearPointer();
        return;
      }
      const target = (event.target as Element).closest<HTMLElement>(POINTER_SURFACES);
      if (!target || !root.contains(target)) {
        clearPointer();
        return;
      }
      if (target !== activeSurface) {
        clearPointer();
        activeSurface = target;
      }
      pointerX = event.clientX;
      pointerY = event.clientY;
      if (!pointerFrame) pointerFrame = requestAnimationFrame(() => {
        pointerFrame = 0;
        if (!activeSurface || document.hidden) return;
        const rect = activeSurface.getBoundingClientRect();
        activeSurface.style.setProperty("--surface-x", `${pointerX - rect.left}px`);
        activeSurface.style.setProperty("--surface-y", `${pointerY - rect.top}px`);
        activeSurface.dataset.pointer = "on";
      });
    };
    const onAnimationEnd = (event: AnimationEvent) => {
      const el = event.target as HTMLElement;
      if (event.animationName.startsWith("ink-scroll-") && el.dataset.scrollMotion === "entering") {
        el.dataset.scrollMotion = "shown";
      }
    };
    const onVisibility = () => {
      if (document.hidden) {
        if (scrollFrame) cancelAnimationFrame(scrollFrame);
        scrollFrame = 0;
        clearPointer();
      } else scheduleScroll();
    };
    const onMotionChange = () => {
      collectTargets();
      scheduleScroll();
    };

    const mutations = new MutationObserver(collectTargets);
    mutations.observe(root, { childList: true, subtree: true });
    const resize = new ResizeObserver(scheduleScroll);
    resize.observe(root);
    const motionSettings = new MutationObserver(onMotionChange);
    motionSettings.observe(document.documentElement, { attributes: true, attributeFilter: ["data-motion"] });
    root.addEventListener("pointermove", onPointerMove, { passive: true });
    root.addEventListener("pointerleave", clearPointer);
    root.addEventListener("animationend", onAnimationEnd);
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", scheduleScroll, { passive: true });
    document.addEventListener("visibilitychange", onVisibility);
    reduced.addEventListener("change", onMotionChange);
    finePointer.addEventListener("change", onMotionChange);

    collectTargets();
    scheduleScroll();

    return () => {
      if (scrollFrame) cancelAnimationFrame(scrollFrame);
      clearPointer();
      mutations.disconnect();
      resize.disconnect();
      motionSettings.disconnect();
      revealObserver.disconnect();
      root.removeEventListener("pointermove", onPointerMove);
      root.removeEventListener("pointerleave", clearPointer);
      root.removeEventListener("animationend", onAnimationEnd);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", scheduleScroll);
      document.removeEventListener("visibilitychange", onVisibility);
      reduced.removeEventListener("change", onMotionChange);
      finePointer.removeEventListener("change", onMotionChange);
      delete document.documentElement.dataset.scrollDirection;
      traceRef.current?.style.removeProperty("--scroll-progress");
    };
  }, [rootRef, traceRef, routeKey]);
}


