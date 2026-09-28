import { useEffect, useRef, type ReactNode } from "react";
import { getLevel } from "../design/motion";

/** 滚动进入编排：分组错峰、整行进入、单次触发（§2.3；反向滚动不重播）。 */
export default function Reveal({ children, delay = 0, className = "" }: { children: ReactNode; delay?: number; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || el.dataset.reveal === "shown") return;
    // 入场中修改 delay 时直接完成，不能把已显示的内容重新藏起来。
    if (el.dataset.reveal === "entering") {
      el.dataset.reveal = "shown";
      return;
    }
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let stopped = false;
    let io: IntersectionObserver | undefined;
    let motionObserver: MutationObserver | undefined;
    const cleanup = () => {
      stopped = true;
      io?.disconnect();
      motionObserver?.disconnect();
      reduced.removeEventListener("change", onMotionChange);
      el.removeEventListener("animationend", onAnimationEnd);
      el.removeEventListener("animationcancel", onAnimationEnd);
    };
    const show = () => {
      el.dataset.reveal = "shown";
      el.classList.add("revealed");
      cleanup();
    };
    const onMotionChange = () => {
      if (!stopped && getLevel() !== "full") show();
    };
    const onAnimationEnd = (event: AnimationEvent) => {
      if (!stopped && event.target === el && event.animationName === "rise-in") show();
    };
    if (getLevel() !== "full" || typeof IntersectionObserver === "undefined") {
      show();
      return;
    }
    el.dataset.reveal = "waiting";
    io = new IntersectionObserver(
      (entries) => {
        if (stopped || el.dataset.reveal !== "waiting") return;
        if (entries.some((entry) => entry.isIntersecting)) {
          el.style.setProperty("--rv-delay", `${Math.max(0, Math.min(delay, 180))}ms`);
          el.dataset.reveal = "entering";
          el.classList.add("revealed");
          io?.disconnect();
        }
      },
      { threshold: 0, rootMargin: "0px 0px -32px 0px" },
    );
    motionObserver = new MutationObserver(onMotionChange);
    motionObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-motion"] });
    reduced.addEventListener("change", onMotionChange);
    el.addEventListener("animationend", onAnimationEnd);
    el.addEventListener("animationcancel", onAnimationEnd);
    io.observe(el);
    return cleanup;
  }, [delay]);
  return (
    <div ref={ref} className={`reveal ${className}`}>
      {children}
    </div>
  );
}
