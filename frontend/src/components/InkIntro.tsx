import { useEffect, useRef, type ReactNode } from "react";
import "../design/ink-intro.css";

type InkIntroProps = {
  kicker: string;
  title: string;
  subtitle: string;
  lead: string;
  children: ReactNode;
};

const studies = [
  { name: "科学", label: "THE NATURAL WORLD", material: "dragon-flow", motif: "orbit" },
  { name: "文化", label: "CULTURE & PLACE", material: "tiger-bleed", motif: "arch" },
  { name: "逻辑", label: "THE ART OF THINKING", material: "tortoise-flow", motif: "logic" },
  { name: "生活", label: "EVERYDAY DISCOVERIES", material: "phoenix-flow", motif: "leaf" },
  { name: "科技", label: "IDEAS OF TOMORROW", material: "dragon-bleed", motif: "grid" },
];

function StudyDrawing({ motif }: { motif: string }) {
  return (
    <svg className="ink-gallery-drawing" viewBox="0 0 200 200" fill="none" aria-hidden="true">
      {motif === "orbit" && <>
        <circle cx="100" cy="100" r="63" />
        <ellipse cx="100" cy="100" rx="84" ry="25" transform="rotate(-32 100 100)" />
        <ellipse cx="100" cy="100" rx="32" ry="79" transform="rotate(-32 100 100)" />
        <path d="M100 15v170M15 100h170" strokeDasharray="2 5" />
        <circle cx="151" cy="59" r="5" fill="currentColor" />
      </>}
      {motif === "arch" && <>
        <path d="M33 169V85a67 67 0 0 1 134 0v84M50 169V85a50 50 0 0 1 100 0v84M67 169V85a33 33 0 0 1 66 0v84M84 169V85a16 16 0 0 1 32 0v84" />
        <path d="M24 170h152M24 178h152M100 14v177" strokeDasharray="3 5" />
      </>}
      {motif === "logic" && <>
        <path d="m100 20 73 42v76l-73 42-73-42V62Zm0 0v82m73-40-73 40-73-40m73 118v-78m-73 36 73-40 73 40" />
        <path d="m63 41 74 118M137 41 63 159M27 100h146" strokeDasharray="3 5" />
        <circle cx="100" cy="100" r="8" fill="currentColor" />
      </>}
      {motif === "leaf" && <>
        <path d="M97 183C64 126 44 67 127 19c36 62 42 114-30 164ZM127 19 97 183M103 147l40-39M108 116l41-37M113 88l30-28M103 147l-32-26M110 108 81 82M116 74 96 56" />
        <circle cx="108" cy="100" r="82" strokeDasharray="2 6" />
      </>}
      {motif === "grid" && <>
        <path d="M25 37h150v126H25Zm0 25h150M25 87h150M25 112h150M25 137h150M50 37v126M75 37v126M100 37v126M125 37v126M150 37v126" opacity=".55" />
        <path d="m25 137 50-50 25 25 50-75M50 163l50-51 25 25 50-50" strokeWidth="2" />
        <circle cx="75" cy="87" r="5" fill="currentColor" /><circle cx="150" cy="37" r="5" fill="currentColor" />
      </>}
    </svg>
  );
}

export default function InkIntro({ kicker, title, subtitle, lead, children }: InkIntroProps) {
  const rootRef = useRef<HTMLElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = rootRef.current;
    const stage = stageRef.current;
    if (!root || !stage) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const desktop = window.matchMedia("(min-width: 900px)");
    const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
    let frame = 0;
    let start = 0;
    let active = false;
    let pointerEnabled = false;
    let pointerFrame = 0;
    let pointerX = 0;
    let pointerY = 0;

    const resetPointer = () => {
      window.cancelAnimationFrame(pointerFrame);
      pointerFrame = 0;
      root.style.setProperty("--ink-pointer-x", "0");
      root.style.setProperty("--ink-pointer-y", "0");
      root.style.setProperty("--ink-pointer-active", "0");
    };
    const renderPointer = () => {
      pointerFrame = 0;
      if (!pointerEnabled || document.hidden) return;
      const bounds = stage.getBoundingClientRect();
      const x = Math.max(-1, Math.min(1, (pointerX - bounds.left) / bounds.width * 2 - 1));
      const y = Math.max(-1, Math.min(1, (pointerY - bounds.top) / bounds.height * 2 - 1));
      root.style.setProperty("--ink-pointer-x", String(x));
      root.style.setProperty("--ink-pointer-y", String(y));
      root.style.setProperty("--ink-pointer-active", "1");
    };
    const movePointer = (event: PointerEvent) => {
      if (!pointerEnabled || document.hidden) return;
      if (event.pointerType !== "mouse") {
        root.dataset.pointer = "off";
        resetPointer();
        return;
      }
      root.dataset.pointer = "on";
      pointerX = event.clientX;
      pointerY = event.clientY;
      if (!pointerFrame) pointerFrame = window.requestAnimationFrame(renderPointer);
    };

    const render = () => {
      frame = 0;
      if (document.hidden) return;
      const progress = active ? Math.max(0, Math.min(1, (window.scrollY - start) / 300)) : 0;
      root.style.setProperty("--ink-intro-progress", String(progress));
    };
    const schedule = () => {
      if (active && !frame && !document.hidden) frame = window.requestAnimationFrame(render);
    };
    const measure = () => {
      const level = document.documentElement.dataset.motion || "full";
      active = desktop.matches && !reduced.matches && level === "full";
      pointerEnabled = finePointer.matches && !reduced.matches && level === "full";
      root.dataset.pointer = pointerEnabled ? "on" : "off";
      resetPointer();
      root.dataset.scroll = active ? "on" : "off";
      const navHeight = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--nav-h")) || 0;
      start = root.getBoundingClientRect().top + window.scrollY - navHeight;
      if (active) schedule();
      else {
        window.cancelAnimationFrame(frame);
        frame = 0;
        root.style.setProperty("--ink-intro-progress", "0");
      }
    };
    const visibility = () => {
      if (document.hidden) {
        window.cancelAnimationFrame(frame);
        frame = 0;
        resetPointer();
      } else measure();
    };
    const observer = new MutationObserver(measure);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-motion"] });
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", measure, { passive: true });
    document.addEventListener("visibilitychange", visibility);
    reduced.addEventListener("change", measure);
    desktop.addEventListener("change", measure);
    finePointer.addEventListener("change", measure);
    stage.addEventListener("pointermove", movePointer, { passive: true });
    stage.addEventListener("pointerleave", resetPointer);
    stage.addEventListener("pointercancel", resetPointer);
    window.addEventListener("blur", resetPointer);
    measure();
    return () => {
      window.cancelAnimationFrame(frame);
      resetPointer();
      observer.disconnect();
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", measure);
      document.removeEventListener("visibilitychange", visibility);
      reduced.removeEventListener("change", measure);
      desktop.removeEventListener("change", measure);
      finePointer.removeEventListener("change", measure);
      stage.removeEventListener("pointermove", movePointer);
      stage.removeEventListener("pointerleave", resetPointer);
      stage.removeEventListener("pointercancel", resetPointer);
      window.removeEventListener("blur", resetPointer);
    };
  }, []);

  return (
    <section ref={rootRef} className="ink-intro" aria-labelledby="comp-title">
      <div ref={stageRef} className="ink-intro-stage">
        <div className="ink-intro-copy">
          <p className="ink-intro-kicker"><span />{kicker}<span /></p>
          <h1 id="comp-title" className="ink-intro-title"><span>{title}</span><span>{subtitle}</span></h1>
          <p className="ink-intro-lead">{lead}</p>
          <div className="ink-intro-actions">{children}</div>
        </div>
        <div className="ink-gallery" aria-hidden="true">
          <div className="ink-gallery-horizon" />
          {studies.map((study, index) => (
            <div className={`ink-gallery-card ink-gallery-card-${index + 1}`} key={study.name}>
              <div className="ink-gallery-sheet">
                <div className="ink-gallery-art">
                  <img className="ink-gallery-material" src={`/ink-mat/${study.material}.png`} alt="" decoding="async" />
                  <StudyDrawing motif={study.motif} />
                  <span className="ink-gallery-number">0{index + 1}</span>
                  <span className="ink-gallery-name">{study.name}</span>
                  <span className="ink-gallery-register">MGUS<br />知·识·札·记</span>
                </div>
                <div className="ink-gallery-caption"><span>{study.label}</span><span>↗</span></div>
              </div>
            </div>
          ))}
        </div>
        <div className="ink-intro-foot" aria-hidden="true"><span>一题一得 · 日有所进</span><span>向下探索 <span className="ink-intro-arrow">↓</span></span><span>MGUS / KNOWLEDGE</span></div>
      </div>
    </section>
  );
}
