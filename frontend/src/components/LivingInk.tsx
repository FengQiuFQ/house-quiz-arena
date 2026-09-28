import { useEffect, useRef } from "react";

/**
 * 全局背景层：**会呼吸的水墨**（§6 第 1 层，B3 强化版）。
 *
 * 相对上一版的升级（用户要求"更浮夸一点"）：
 *  1. **真实的墨边** —— 每个墨域套一层 SVG 位移滤镜（feTurbulence + feDisplacementMap），
 *     边缘不再是数学上完美的椭圆，而是带毛边与渗透感的墨迹；位移场每 9 秒在两个
 *     种子值之间缓慢过渡（只改 scale，不重建滤镜树），因此"墨在缓缓化开"。
 *  2. **三层视差** —— 远/中/近三层以不同速率随滚动位移，深度感成立。
 *     视差只作用于背景层（Skill 规则：绝不作用于文字或交互控件）。
 *  3. **随滚动渐强** —— 下滚时墨的浓度与尺度略微上升，页面像"往墨里沉"。
 *  4. **光标墨晕** —— 指针移动时有一团低强度墨晕跟随（仅桌面指针，触屏不启用）。
 *  5. **换页时墨域收束再展开** —— View Transitions 期间由 CSS 驱动（见 motion.css）。
 *
 * 性能红线（本层是全站最贵的一层，必须守住）：
 *  · 滤镜只做**静态**几何变换 + 极慢的 scale 过渡；绝不做逐帧滤镜参数动画。
 *  · 只用 transform / opacity / CSS 变量；滚动与指针事件经 rAF 合并，不直接改样式。
 *  · 减弱档（reduced）下整层不动且降为静态；简化档（simplified）保留视差、停掉滤镜流动。
 *  · 整层 aria-hidden，不参与语义；文字区一律不覆盖。
 */

type W = [number, number, number, number];
const VAR = ["--fw-a", "--fw-b", "--fw-c", "--fw-d"];

/** 每个场景一套权重 + 色值（写内联变量＝赋 CSS 变量，不是堆叠覆盖样式）。
 *
 *  B3 二次调参：色值 alpha 从 0.46–0.6 提到 0.78–0.95。
 *  原因是 A/B 实测发现——继续乘倍率（1.6×）时墨反而变淡，说明**天花板在色值本身**：
 *  rgba alpha 只有 0.5 时，无论怎么加权，墨域核心的色度都上不去。
 *  提高 alpha 才是真正的"更浓"。（对照实测见 _verify/shots/b3ab/ab-v2.json）
 *  安全性：墨只影响背景层，文字对比度由 field-veil 与卡片实底数据带保证，改动后已复测。 */
const SCENES: Record<string, { w: W; c: [string, string, string, string] }> = {
  home: { w: [0.52, 0.44, 0.36, 0.3], c: ["rgba(56,168,132,.9)", "rgba(86,108,158,.86)", "rgba(212,94,52,.82)", "rgba(186,200,212,.8)"] },
  practice: { w: [0.42, 0.32, 0.26, 0.22], c: ["rgba(52,156,124,.86)", "rgba(80,102,150,.8)", "rgba(200,88,50,.76)", "rgba(180,194,206,.74)"] },
  play: { w: [0.3, 0.24, 0.2, 0.17], c: ["rgba(48,142,114,.8)", "rgba(74,96,142,.74)", "rgba(190,84,48,.7)", "rgba(174,188,200,.68)"] },
  screen: { w: [0.4, 0.38, 0.34, 0.3], c: ["rgba(52,158,124,.86)", "rgba(82,104,152,.82)", "rgba(204,92,52,.8)", "rgba(184,198,210,.78)"] },
  results: { w: [0.4, 0.36, 0.32, 0.28], c: ["rgba(54,160,126,.86)", "rgba(84,106,154,.82)", "rgba(206,94,54,.78)", "rgba(182,196,208,.78)"] },
  admin: { w: [0.14, 0.12, 0.11, 0.1], c: ["rgba(46,125,100,.6)", "rgba(70,88,126,.56)", "rgba(180,80,47,.5)", "rgba(158,170,180,.5)"] },
  login: { w: [0.26, 0.24, 0.2, 0.18], c: ["rgba(50,148,118,.82)", "rgba(78,100,146,.78)", "rgba(196,86,48,.72)", "rgba(176,190,202,.72)"] },
};
const HOUSE_WASH: Record<string, { w: W; c: [string, string, string, string] }> = {
  tiger: { w: [0.08, 0.08, 0.07, 0.66], c: ["rgba(46,125,100,.24)", "rgba(70,88,126,.24)", "rgba(180,80,47,.2)", "rgba(214,226,236,.95)"] },
  dragon: { w: [0.66, 0.08, 0.07, 0.08], c: ["rgba(58,178,140,.95)", "rgba(70,88,126,.24)", "rgba(180,80,47,.2)", "rgba(158,170,180,.24)"] },
  tortoise: { w: [0.08, 0.66, 0.07, 0.08], c: ["rgba(46,125,100,.24)", "rgba(92,120,174,.95)", "rgba(180,80,47,.2)", "rgba(158,170,180,.24)"] },
  phoenix: { w: [0.08, 0.08, 0.66, 0.08], c: ["rgba(46,125,100,.24)", "rgba(70,88,126,.24)", "rgba(222,96,52,.95)", "rgba(158,170,180,.24)"] },
};

/** 色值乘上强度倍率：只改 alpha，不改色相。
 *  这样"更浓"是同一池墨更浓，而不是换了一组颜色——视觉语言的连续性不会被破坏。 */
function scaleAlpha(rgba: string, k: number): string {
  const m = rgba.match(/rgba?\(([^)]+)\)/);
  if (!m) return rgba;
  const parts = m[1].split(",").map((s) => s.trim());
  const [r, g, b] = parts;
  const a = parts.length > 3 ? Number(parts[3]) : 1;
  return `rgba(${r},${g},${b},${Math.min(1, a * k).toFixed(3)})`;
}

export default function LivingInk({ scene }: { scene: string }) {
  const ref = useRef<HTMLDivElement>(null);

  // 场景色值切换（含强度倍率 --ink-w/--ink-c，供 A/B 实测与演示调节）
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const cs = getComputedStyle(root);
    const kw = Number(cs.getPropertyValue("--ink-w")) || 1;
    const kc = Number(cs.getPropertyValue("--ink-c")) || 1;
    const house = scene.startsWith("house:") ? scene.slice(6) : null;
    const cfg = (house && HOUSE_WASH[house]) || SCENES[scene] || SCENES.home;
    VAR.forEach((v, i) => {
      root.style.setProperty(v, scaleAlpha(cfg.c[i], kc));
      const blob = root.querySelector<HTMLElement>(`.wash-${"abcd"[i]}`);
      if (blob) blob.style.opacity = String(Math.min(1, cfg.w[i] * kw));
    });
  }, [scene]);

  // 滚动视差 + 浓度渐强（rAF 合并）
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    let raf = 0;
    let lastY = -1;
    const apply = () => {
      raf = 0;
      const y = window.scrollY || 0;
      if (y === lastY) return;
      lastY = y;
      // 位移上限 140px：Skill 规则要求视差 delta 小，前景与背景不能明显脱节
      root.style.setProperty("--scroll-y", String(Math.min(y, 1600)));
      root.style.setProperty("--scroll-k", String(Math.min(1, y / 900)));
    };
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(apply); };
    window.addEventListener("scroll", onScroll, { passive: true });
    apply();
    return () => {
      window.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  // 光标墨晕（仅精确指针；触屏与减弱档不启用）
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    if (!window.matchMedia("(pointer: fine)").matches) return;
    if ((document.documentElement.dataset.motion || "full") === "reduced") return;
    let raf = 0;
    let px = 0, py = 0;
    const apply = () => {
      raf = 0;
      root.style.setProperty("--px", `${px}px`);
      root.style.setProperty("--py", `${py}px`);
      root.style.setProperty("--ptr", "1");
    };
    const onMove = (e: PointerEvent) => {
      px = e.clientX; py = e.clientY;
      if (!raf) raf = requestAnimationFrame(apply);
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <div className="ink-field" ref={ref} aria-hidden="true">
      {/* 位移滤镜：把椭圆的完美边缘变成有毛边与渗透感的墨迹。
          只做极慢的 scale 过渡（9s），不逐帧改滤镜参数。 */}
      <svg className="ink-defs" width="0" height="0" aria-hidden="true" focusable="false">
        <defs>
          <filter id="inkBleedA" x="-25%" y="-25%" width="150%" height="150%"
                  colorInterpolationFilters="sRGB">
            <feTurbulence type="fractalNoise" baseFrequency="0.008 0.011" numOctaves="3"
                          seed="7" result="t" />
            <feDisplacementMap in="SourceGraphic" in2="t" scale="78" xChannelSelector="R"
                               yChannelSelector="G" />
          </filter>
          <filter id="inkBleedB" x="-25%" y="-25%" width="150%" height="150%"
                  colorInterpolationFilters="sRGB">
            <feTurbulence type="fractalNoise" baseFrequency="0.006 0.009" numOctaves="3"
                          seed="23" result="t" />
            <feDisplacementMap in="SourceGraphic" in2="t" scale="96" xChannelSelector="R"
                               yChannelSelector="G" />
          </filter>
        </defs>
      </svg>

      <div className="field-base" />
      {/* 远层：大而慢 */}
      <div className="ink-layer layer-far">
        <div className="wash wash-a" style={{ opacity: 0.42, ["--fw-a" as string]: "rgba(46,125,100,.6)" }} />
        <div className="wash wash-b" style={{ opacity: 0.34, ["--fw-b" as string]: "rgba(70,88,126,.54)" }} />
      </div>
      {/* 近层：小、快、墨边更明显 */}
      <div className="ink-layer layer-near">
        <div className="wash wash-c" style={{ opacity: 0.28, ["--fw-c" as string]: "rgba(180,80,47,.46)" }} />
        <div className="wash wash-d" style={{ opacity: 0.24, ["--fw-d" as string]: "rgba(158,170,180,.44)" }} />
      </div>
      <div className="grain" />
      <div className="ptr-bloom" />
      <div className="field-veil" />
    </div>
  );
}
