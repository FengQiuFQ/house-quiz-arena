import { useEffect } from "react";

/**
 * 常驻展示页的「安静材质」开关。
 *
 * 为什么需要它（这是用户反馈的问题，不是审美偏好）：
 * 全局水墨层的 `.wash` 是 **96vmax 的大圆**，直径接近屏幕宽度；而 `flow-drift-*`
 * 关键帧里的 translate 百分比是相对**元素自身尺寸**的 —— ±18% 就是 ±345px 的真实位移，
 * 四个墨域各自来回数百像素，周期 22–34s。
 *
 * 在桌面近距离看，这是"墨在缓缓化开"；但在 3–6 米外的墙上大屏，它就是**整块可见移动**
 * （实测每帧 1.2–1.8px，@3–6m 约等于每秒 25px 的视野位移），长时间盯着看即"一闪一闪"。
 * 实测已排除"动画被重置"：0 次 animationstart、0 次几何跳变 —— 所以问题在**幅度**，不在重启。
 *
 * 因此只对**可能连续开着几小时、观众在远处看**的页面降幅降速（周期 ×3、位移压到 1/3）；
 * 桌面交互页保持原有表现力 —— 首页的"浮夸"是用户明确要求的，不该被一起调安静。
 *
 * 用 html 级 class 而不是页面内样式：`.ink-field` 挂在全局 Layout 上，
 * 页面内部选不到它。
 *
 * ── 为什么同一个 hook 还要加 `big-screen` 类 ─────────────────────────────────
 * 大屏的字号倍率 `--fs` **必须写在 <html> 上**，不能写在 `.screen-page` 这类页面容器上。
 *
 * 原因是 CSS 自定义属性的继承语义：`--fs-xs: calc(var(--fs) * 12px)` 声明在 `:root`，
 * 它的**计算值**在 :root 那一步就已经把 `var(--fs)` 代进去了，然后作为"已算好的值"往下继承。
 * 在后代元素上重新声明 `--fs` **不会**让继承下来的 `--fs-xs` 重新计算 ——
 * 只有那些直接写 `var(--fs)` 的属性（例如 `.st-chip` 的 clamp）才会跟着变。
 *
 * 实测症状：在 `.screen-page` 上写 `--fs: 2.15` 后，口径条是 30.1px，
 * 而 `--fs-xs` 仍是 12px —— 同一屏里出现两套比例，且"小字"那一套在 3–6 米外读不出来。
 * 挂到 html 上，`:root` 与 `--fs` 在同一个元素上，整条刻度才会一起重算。
 */
export function useCalmInk(): void {
  useEffect(() => {
    const root = document.documentElement;
    root.classList.add("calm-ink", "big-screen");
    return () => root.classList.remove("calm-ink", "big-screen");
  }, []);
}
