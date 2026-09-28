/**
 * 动效基础设施（时间与缓动值在 motion.css，这里只做行为）。
 *
 * 三档表现（功能完全一致）：
 *   full       完整：全局背景 + 卡片材质持续流动 + 全部转场
 *   simplified 简化：停掉装饰循环，保留短促过渡
 *   reduced    减弱：仅保留必要反馈（尊重系统 prefers-reduced-motion）
 *
 * 规则：高频动画只用 transform/opacity；同一属性只有一套机制；
 * 连续点击取消前一次而非排队；隐藏页与离屏元素暂停装饰运动。
 */

export type MotionLevel = "full" | "simplified" | "reduced";
const KEY = "mgus-motion";

export function detectLevel(): MotionLevel {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return "reduced";
  const saved = localStorage.getItem(KEY);
  if (saved === "full" || saved === "simplified" || saved === "reduced") return saved;
  const nav = navigator as Navigator & { deviceMemory?: number; connection?: { saveData?: boolean } };
  const weak = (nav.hardwareConcurrency ?? 8) <= 4 || (nav.deviceMemory !== undefined && nav.deviceMemory <= 4) || nav.connection?.saveData === true;
  return weak ? "simplified" : "full";
}

export function getLevel(): MotionLevel {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return "reduced";
  return (document.documentElement.dataset.motion as MotionLevel) || "full";
}

/** 是否需要持续装饰运动（渲染期判断用；不触发重渲染） */
export function isLite(): boolean {
  return getLevel() !== "full";
}

export function applyLevel(level: MotionLevel): void {
  document.documentElement.dataset.motion = level;
}

export function setLevel(level: MotionLevel): void {
  localStorage.setItem(KEY, level);
  applyLevel(level);
}

/* ═══ 显示档位：**已下线**（2026-09-26 产品决定）════════════════════════════
   原来有「常规 / 希沃大屏」两档：大屏档把整套字号、间距、触控目标一起 ×1.9，
   并且**会在"大尺寸 + 触摸"的设备上自动启用**（希沃一体机就是这一类）。

   产品负责人决定去掉这个选项，"一直就用现在这套尺寸"。因此这里：
     · 删掉档位类型与读写接口，界面不再提供切换；
     · 同时**清掉 `mgus-display` 这个 localStorage 键** —— 否则以前在希沃上被自动
       写成 "large" 的机器，升级后仍会从存储里读出大屏档，看起来像"取消了没生效"；
     · `data-display` 一律保持在 "standard"（不设置 large）。

   CSS 里 `html[data-display="large"]` 那些规则**保留但已成死代码**（属性不会再出现），
   并在 tokens.css 处标注了它们的来历；将来若要恢复"远距可读"这一档，
   正确的起点是重新评估观看距离，而不是把这段直接打开。 */
const DKEY = "mgus-display";

export function applyDisplay(): void {
  // 清掉历史遗留的档位记录，再固定为标准档
  try { localStorage.removeItem(DKEY); } catch { /* 隐私模式下不可写，忽略 */ }
  document.documentElement.dataset.display = "standard";
}

/* ═══ 双模式：日常操练 / 正式竞赛 ═══════════════════════════════════════════
   两者的差别不只是文案，而是**整页内容与可用功能**：
     日常  注册/登录 → 刷题 → 上排行榜（PK）        —— 没有观战、没有大屏
     竞赛  签到 → 答题 / 观战 / 大屏 → 统一揭晓      —— 一切围绕正在进行的那一场

   为什么用 URL 参数而不是全局状态：现场讲课时需要**直接给一条链接**就能打开竞赛大屏
   （`/?mode=comp`），而不是"先打开首页，再点右上角，再点大屏"。URL 可分享、可收藏、
   可投屏；纯状态做不到。同时把选择写进 localStorage，作为下次访问的默认。

   自动判定：后台已经开启比赛（lobby/running）时**默认进竞赛模式** ——
   比赛期间打开网站的人，绝大多数是想看比赛的。
   ═══════════════════════════════════════════════════════════════════ */
export type UiMode = "daily" | "comp";
export const MODE_PARAM = "mode";
const MKEY = "mgus-uimode";

export function modeToHref(path: string, mode: UiMode): string {
  const [base, hash = ""] = path.split("#");
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}${MODE_PARAM}=${mode}${hash ? "#" + hash : ""}`;
}

export function readModeFromLocation(search: string, live: boolean): UiMode {
  const p = new URLSearchParams(search).get(MODE_PARAM);
  if (p === "daily" || p === "comp") return p;
  const saved = localStorage.getItem(MKEY);
  if (saved === "daily" || saved === "comp") return saved;
  return live ? "comp" : "daily";
}

export function rememberMode(mode: UiMode): void {
  localStorage.setItem(MKEY, mode);
}

/** View Transitions 封装：方向性页面过渡 / 共享元素展开。不支持或减弱档 → 直接更新（功能不受影响）。 */
export type VtKind = "forward" | "back" | "sibling" | "expand" | "collapse";
let activeTransition: Pick<ViewTransition, "skipTransition"> | null = null;
let transitionId = 0;

export function withViewTransition(update: () => void, kind: VtKind = "forward"): Promise<void> {
  const root = document.documentElement;
  const id = ++transitionId;
  activeTransition?.skipTransition();
  activeTransition = null;
  let updated = false;
  let updateResult = Promise.resolve();
  const updateOnce = () => {
    if (!updated) {
      updated = true;
      try { updateResult = Promise.resolve(update()); }
      catch (error) { updateResult = Promise.reject(error); }
    }
    return updateResult;
  };
  let done: Promise<void>;
  if (getLevel() === "reduced" || typeof document.startViewTransition !== "function") {
    delete root.dataset.vt;
    done = updateOnce();
  } else {
    root.dataset.vt = kind;
    try {
      const vt = document.startViewTransition(updateOnce);
      activeTransition = vt;
      // 跳过/无法建立快照只影响动画；业务更新失败仍须通过返回值报告。
      void vt.ready.catch(() => undefined);
      done = Promise.all([
        vt.updateCallbackDone,
        vt.finished.catch(() => undefined),
      ]).then(() => undefined);
    } catch {
      // 即使浏览器在调用更新后才抛错，也只复用结果，不重复执行业务更新。
      done = updateOnce();
    }
  }
  return done.finally(() => {
    if (id === transitionId) {
      activeTransition = null;
      delete root.dataset.vt;
    }
  });
}

/** 液态按压反馈：全局委托（pointer 事件同时覆盖触屏与鼠标；在按压点产生局部材质反馈）。 */
let pressed: HTMLElement | null = null;
const PRESSABLE = ".btn, .opt, .q-row, .tab-item, .nav-link, .card-link, [data-press]";

function pressOff(): void {
  if (!pressed) return;
  const el = pressed;
  pressed = null;
  el.classList.remove("pressing");
  if (getLevel() === "reduced") return;
  // 连续操作：先移除再重加，取消上一次的释放动画（不排队）
  el.classList.remove("releasing");
  void el.offsetWidth;
  el.classList.add("releasing");
  window.setTimeout(() => el.classList.remove("releasing"), 280);
}

export function initLiquidPress(): () => void {
  const down = (e: PointerEvent) => {
    const target = e.target as HTMLElement | null;
    const el = target?.closest?.(PRESSABLE);
    if (!(el instanceof HTMLElement)) return;
    if (pressed && pressed !== el) pressOff();
    pressed = el;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--px", `${e.clientX - r.left}px`);
    el.style.setProperty("--py", `${e.clientY - r.top}px`);
    el.classList.add("pressing");
    if (getLevel() === "full") {
      const old = el.querySelector(":scope > .press-ripple");
      if (old) old.remove();
      const rip = document.createElement("span");
      rip.className = "press-ripple";
      rip.setAttribute("aria-hidden", "true");
      rip.style.left = `${e.clientX - r.left}px`;
      rip.style.top = `${e.clientY - r.top}px`;
      el.appendChild(rip);
      rip.addEventListener("animationend", () => rip.remove(), { once: true });
    }
  };
  const up = () => pressOff();
  document.addEventListener("pointerdown", down, { passive: true });
  window.addEventListener("pointerup", up, { passive: true });
  window.addEventListener("pointercancel", up, { passive: true });
  return () => {
    document.removeEventListener("pointerdown", down);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
  };
}

/** 页面隐藏 → 暂停全部装饰运动（恢复可见后自动继续）。 */
export function initVisibilityPause(): () => void {
  const onVis = () => document.documentElement.classList.toggle("motion-paused", document.hidden);
  document.addEventListener("visibilitychange", onVis);
  onVis();
  return () => document.removeEventListener("visibilitychange", onVis);
}
