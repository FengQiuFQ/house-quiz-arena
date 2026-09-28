import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { flushSync } from "react-dom";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../App";
import { api } from "../api/client";
import { setLevel, modeToHref, type MotionLevel, withViewTransition } from "../design/motion";
import { useGlobalScrollMotion } from "../design/useGlobalScrollMotion";

/** 被记住的"上次所在页面"，按模式分开记。
 *  为什么按模式分开：两个模式的内容完全不同，混着记会出现
 *  "刚切到竞赛却被送到练习页"这种荒谬结果。 */
const LAST_KEY = (m: "daily" | "comp") => `mgus-last-${m}`;
import LivingInk from "./LivingInk";
import Wordmark, { Monogram } from "../design/Logo";
import { IconSettings, IconMenu, IconClose } from "../design/icons";

const NAV: Array<{ to: string; label: string; match: (p: string) => boolean }> = [
  { to: "/", label: "首页", match: (p) => p === "/" },
  { to: "/admin", label: "后台", match: (p) => p.startsWith("/admin") },
];

/** 顶栏：品牌清楚、导航稳定。宽度不足时先收起品牌副行，再折叠为菜单 —— 导航永不逐字换行。 */
export default function Layout({ children, scene, authed, live = false, liveMatchId = null,
  liveMatchRunning = false }: {
  children: ReactNode; scene: string; authed: boolean; live?: boolean; liveMatchId?: string | null;
  /** 比赛是否**正在打**（running / paused）。只有它才配让"切竞赛"偏离竞赛首页。
   *  不能拿 live 代替：live 包含 lobby（签到中）—— 大厅阶段也当成"进行中"，
   *  切「竞赛」就会落到观战页而不是竞赛首页（已实测复现）。 */
  liveMatchRunning?: boolean;
}) {
  const { me, motion, setMotion, mode, setMode } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const [hover, setHover] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [setOpen, setSetOpen] = useState(false);
  const [narrow, setNarrow] = useState(false);
  const lastPath = useRef(loc.pathname);
  const linkRefs = useRef<Record<string, HTMLAnchorElement | null>>({});
  const indRef = useRef<HTMLSpanElement>(null);
  const setRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const traceRef = useRef<HTMLDivElement>(null);

  useGlobalScrollMotion(mainRef, traceRef, `${loc.pathname}${loc.key}`);

  /**
   * 把顶栏的**实测高度**写成 `--nav-h`。
   *
   * 为什么不能让 CSS 里那个写死的值继续用：投屏/观赛页用
   * `height: calc(100dvh - var(--nav-h))` 做"一屏放下"的高度预算，
   * 而顶栏高度是**随字号倍率变的** —— 大屏档下它是 88px，普通页是 65px，
   * 写死的 81px 在两边都不对。实测症状：投屏页总高 1087px，在 1080p 上溢出 7px，
   * 而溢出的那 7px 正是"顶栏实际比预算高"。
   * 用 ResizeObserver 实测而不是再写一个魔数：字号倍率、窄屏折叠、
   * 品牌副行收起都会改变顶栏高度，只有实测能一直跟上。
   */
  useEffect(() => {
    const el = barRef.current;
    if (!el) return;
    const apply = () => {
      const h = Math.round(el.getBoundingClientRect().height);
      if (h > 0) document.documentElement.style.setProperty("--nav-h", `${h}px`);
    };
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => { ro.disconnect(); document.documentElement.style.removeProperty("--nav-h"); };
  }, [narrow]);

  const items = NAV.filter((n) => n.to !== "/admin" || me?.role === "admin");

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 720px)");
    const on = () => setNarrow(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  // 选中/悬停胶囊：连续滑动、移动中伸缩、到位恢复
  const measure = useCallback(() => {
    const ind = indRef.current;
    if (!ind) return;
    const active = items.find((n) => n.match(loc.pathname))?.to ?? "";
    const el = linkRefs.current[hover ?? active];
    if (!el) { ind.style.opacity = "0"; return; }
    const pr = ind.parentElement!.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    ind.style.opacity = "1";
    ind.style.width = `${r.width}px`;
    ind.style.transform = `translateX(${r.left - pr.left}px)`;
    ind.classList.remove("moving");
    void ind.offsetWidth;
    ind.classList.add("moving");
    const t = window.setTimeout(() => ind.classList.remove("moving"), 340);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc.pathname, hover, me?.role, narrow]);

  useLayoutEffect(() => {
    const t = measure();
    return () => { if (typeof t === "function") t(); };
  }, [measure]);

  useEffect(() => {
    const onResize = () => measure();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [measure]);

  // 设置菜单：点击外部 / Esc 关闭，并把焦点还给触发按钮
  useEffect(() => {
    if (!setOpen) return;
    const onDoc = (e: PointerEvent) => {
      if (!setRef.current?.contains(e.target as Node)) setSetOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setSetOpen(false); };
    document.addEventListener("pointerdown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("pointerdown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [setOpen]);

  /** 导航方向规律（同级切换 / 进入详情 / 返回上级）：
   *  · 首页 → 子页            → forward（深入一级）
   *  · 子页 → 首页 / 上级     → back（返回）
   *  · 子页 ↔ 子页            → sibling（同级接替）
   *  自动判断之外可用显式参数覆盖。 */
  const go = (to: string, explicit?: "forward" | "back" | "sibling") => {
    setMenuOpen(false);
    const here = lastPath.current;
    lastPath.current = to;
    let dir: "forward" | "back" | "sibling" = "sibling";
    if (here === "/" && to !== "/") dir = "forward";
    else if (to === "/") dir = "back";
    else if (to.split("/")[1] === here.split("/")[1]) dir = "sibling";
    else dir = "forward";
    void withViewTransition(() => { nav(to); }, explicit ?? dir);
  };

  const logout = async () => {
    await api.post("/api/v1/auth/logout").catch(() => undefined);
    setMenuOpen(false);
    nav("/");
    window.location.reload();
  };

  const MOTION_OPTS: Array<{ v: MotionLevel; label: string }> = [
    { v: "full", label: "完整" },
    { v: "simplified", label: "简化" },
    { v: "reduced", label: "减弱" },
  ];

  /* 顶栏的"上下文入口"：同一个位置，按模式给出该模式最常用的去处。
       日常 → 排行榜（独立页面，进去直接看到榜，不经过答题流程）
       竞赛 → 观赛大屏（比赛期间最常打开的是观战/大屏）
     为什么不是固定放"练习"：竞赛模式根本没有练习（练习只属于日常），
     在竞赛里显示一个点进去就跳走的"练习"是误导。
     为什么排行榜要有独立页：过去这个入口指向练习页的榜单标签，
     观感是"点排行榜怎么跳去答题了"——入口的落点必须与它的名字一致。 */
  const ctxEntry = mode === "daily"
    ? { to: "/leaderboard", label: "排行榜", hint: "正确率 / 速度 / 连对 / House 荣誉" }
    : {
        to: liveMatchId ? `/matches/${liveMatchId}/watch` : "/",
        label: "观赛",
        hint: liveMatchId ? "实时观赛与四院进度" : "当前没有进行中的比赛",
        disabled: !liveMatchId,
      };

  /* 模式切换：日常操练 ↔ 正式竞赛。
     行为定稿（修过一次，原因是真实观感问题）：

     ① **总是回到该模式的主页**（日常 → 首页 / 竞赛 → 竞赛首页），
        不续上次浏览的深页。
        为什么改：原先做的是"回到上次位置"，于是上次停留在练习页时，
        点「日常」会直接落到 /practice —— 而练习页一进去就是答题流程，
        观感变成"点了一下日常，怎么直接跳去答题了"，非常突兀。
        模式按钮的语义是**换一个空间**，不是"回到这个空间的上次位置"；
        空间内部的前进/后退交给浏览器与页内导航。

     ② 竞赛模式有一个例外：若上次那场比赛**仍在进行**、且上次在看观战，
        则直接回到观战页 —— 比赛正在发生时，这确实是最可能的意图。
        比赛已结束一律回主页，避免把人送进已锁定的答题页。

     过渡用 View Transition 的 sibling 语义：同级整页替换、背景延续、
     内容横向接替，而不是白屏重排。 */
  const [modeSwitching, setModeSwitching] = useState<"daily" | "comp" | null>(null);

  const switchMode = (m: "daily" | "comp") => {
    /* 即使点的就是当前模式，也要回到该模式的主页。
     *
     * 这条是真实体验问题：原来 `if (m === mode) return;` —— 人在练习页时点「日常」
     * 什么都不发生，看起来像按钮坏了；而模式按钮的语义本来就是"回到这个空间"。
     * 已经在主页时才是真正的空操作（避免每次点击都白跑一次过渡动画）。 */
    const alreadyHome = loc.pathname === "/" && /(^|[?&])mode=/.test(loc.search)
      && new URLSearchParams(loc.search).get("mode") === m;
    if (m === mode && alreadyHome) return;
    setModeSwitching(m);
    let to = "/";
    /* 例外分支：上次在**正在进行的比赛**的观战页时，切「竞赛」直接回到那个观战页。
       现场讲课时这确实是最可能的意图（比赛正在发生，人想看的就是它）。

       三道门槛缺一不可（原来只查了第一道，实测出过缺陷）：
         ① 上次停留的确实是观战页，且属于**当前这场**比赛；
         ② 这场比赛**正在打**（running / paused）—— 只查 liveMatchId 不够，
            因为 lobby（签到中）也会给 liveMatchId，于是"切竞赛"落到观战页而不是竞赛首页；
         ③ 该页面的 URL 只能保留 mode 参数，不能带上次留下的 mode=xxx，
            否则落地的页面会自己把模式改回旧值，出现"切了却没生效"的观感。 */
    if (m === "comp" && liveMatchRunning) {
      try {
        const remembered = localStorage.getItem(LAST_KEY("comp"));
        const mid = remembered?.match(/\/matches\/([^/]+)/)?.[1];
        if (mid && liveMatchId && mid === liveMatchId && remembered!.includes("/watch")) {
          to = `/matches/${mid}/watch`;
        }
      } catch { /* 隐私模式下 localStorage 不可用：回主页即可 */ }
    }
    const href = modeToHref(to, m);
    window.setTimeout(() => setModeSwitching(null), 620);
    void withViewTransition(() => {
      flushSync(() => { setMode(m); nav(href); });
    }, "sibling");
  };

  /* 进入某个模式内的页面时记下来（供下次切换回到这里） */
  useEffect(() => {
    if (loc.pathname === "/") return;
    const owner = loc.pathname.startsWith("/practice") ? "daily"
      : loc.pathname.startsWith("/matches/") ? "comp" : null;
    if (owner) {
      try { localStorage.setItem(LAST_KEY(owner), loc.pathname + loc.search); } catch { /* 隐私模式 */ }
    }
  }, [loc.pathname, loc.search]);

  return (
    <>
      <div ref={traceRef} className="scroll-trace" aria-hidden="true" />
      <LivingInk scene={scene} />
      {/* 模式切换的流程提示：一次性的覆盖条，明确告诉用户"正在切换到竞赛模式"。
          为什么值得做：两个模式的界面差异极大，直接硬切会让人以为"页面跳错了"；
          一个 0.6 秒的过渡把"切换"这件事变成可理解的动作。
          它不是加载遮罩（不阻塞操作），只是视觉引导；减弱档下不播动画。 */}
      {modeSwitching && (
        <div className={`mode-sweep ${modeSwitching}`} role="status" aria-live="polite">
          <span className="ms-label">
            正在切换到{modeSwitching === "comp" ? "竞赛模式" : "日常操练"}
          </span>
        </div>
      )}
      <div className="app-shell">
        <header className="topbar" ref={barRef}>
          <div className="container topbar-inner">
            <a
              className="brand"
              href="/"
              onClick={(e) => { e.preventDefault(); go("/"); }}
              aria-label="MGUS 国贸枫禾 House 知识竞赛 · 返回首页"
            >
              <Wordmark />
            </a>

            {!narrow && (
              <nav className="nav" aria-label="站点导航">
                <span className="nav-ind" ref={indRef} aria-hidden="true" />
                {items.map((n) => (
                  <a
                    key={n.to}
                    ref={(el) => { linkRefs.current[n.to] = el; }}
                    href={n.to}
                    className="nav-link"
                    onClick={(e) => { e.preventDefault(); go(n.to); }}
                    onMouseEnter={() => setHover(n.to)}
                    onMouseLeave={() => setHover(null)}
                    onFocus={() => setHover(n.to)}
                    onBlur={() => setHover(null)}
                    aria-current={n.match(loc.pathname) ? "page" : undefined}
                  >
                    {n.label}
                  </a>
                ))}
                {/* 上下文入口：位置固定，标签与去处随模式变化。
                    它的出现/消失带一次高度与透明度过渡，避免顶栏"跳一下"。 */}
                <a
                  key={ctxEntry.to + mode}
                  href={ctxEntry.to}
                  className={`nav-link ctx-entry${ctxEntry.disabled ? " off" : ""}`}
                  title={ctxEntry.hint}
                  ref={(el) => { linkRefs.current["ctx"] = el; }}
                  onMouseEnter={() => setHover("ctx")}
                  onMouseLeave={() => setHover(null)}
                  onFocus={() => setHover("ctx")}
                  onBlur={() => setHover(null)}
                  onClick={(e) => {
                    e.preventDefault();
                    if (ctxEntry.disabled) return;
                    go(ctxEntry.to.split("#")[0]);
                  }}
                  aria-disabled={ctxEntry.disabled || undefined}
                >
                  {ctxEntry.label}
                </a>
              </nav>
            )}

            <div className="nav" style={{ marginLeft: narrow ? "auto" : 0, gap: 6 }}>
              {/* 模式切换：右上角。比赛未开启时也允许切到竞赛模式（先看规则/等开赛），
                  因此不禁用，只在有进行中比赛时给"进行中"提示点。 */}
              {!narrow && (
                <div className="mode-switch" role="group" aria-label="显示模式">
                  <button
                    className={mode === "daily" ? "on" : ""}
                    aria-pressed={mode === "daily"}
                    onClick={() => switchMode("daily")}
                    title="日常操练：自由刷题、上排行榜"
                  >
                    日常
                  </button>
                  <button
                    className={mode === "comp" ? "on" : ""}
                    aria-pressed={mode === "comp"}
                    onClick={() => switchMode("comp")}
                    title="正式竞赛：参赛、观战、展示大屏"
                  >
                    竞赛{live && <i className="ms-dot" aria-label={liveMatchRunning ? "比赛进行中" : "有比赛待开始"} />}
                  </button>
                </div>
              )}

              {!narrow && (me ? (
                <button
                  className="btn btn-sm"
                  onClick={() => go("/account")}
                  title="账号与安全：修改密码"
                  aria-current={loc.pathname === "/account" ? "page" : undefined}
                >
                  {me.display_name || me.username}
                  {me.must_change_password && <span className="dot-warn" aria-label="需要修改密码" />}
                </button>
              ) : (
                <a
                  href="/login"
                  className="btn btn-sm btn-primary"
                  onClick={(e) => { e.preventDefault(); go("/login"); }}
                >
                  登录
                </a>
              ))}

              <div className="settings-wrap" ref={setRef}>
                <button
                  className="icon-btn"
                  aria-label="设置"
                  aria-expanded={setOpen}
                  aria-haspopup="dialog"
                  onClick={() => setSetOpen((v) => !v)}
                >
                  <IconSettings />
                </button>
                {setOpen && (
                  <div className="settings-menu" role="dialog" aria-label="设置">
                    <h4>动效档位</h4>
                    <div className="seg" role="group" aria-label="动效档位">
                      {MOTION_OPTS.map((o) => (
                        <button
                          key={o.v}
                          aria-pressed={motion === o.v}
                          onClick={() => { setLevel(o.v); setMotion(o.v); }}
                        >
                          {o.label}
                        </button>
                      ))}
                    </div>
                    <p className="settings-note">
                      完整档：背景与卡片材质持续流动；简化档：停掉装饰循环、保留短促过渡；减弱档：仅保留必要反馈。
                      系统「减少动态效果」开启时自动使用减弱档。
                    </p>
                    {/* 「显示尺寸（常规 / 希沃大屏）」这一档已按产品决定下线：
                        页面对所有设备固定使用同一套尺寸与触控目标。
                        这里连说明一起删掉，而不是只藏按钮 —— 留着一段解释"大屏档会怎样"
                        的文字却找不到开关，比没有更让人困惑。 */}
                    {me && (
                      <>
                        <h4 style={{ marginTop: "var(--sp-4)" }}>账号</h4>
                        <div className="seg" role="group" aria-label="账号操作">
                          <button onClick={() => { setSetOpen(false); go("/account"); }}>修改密码</button>
                          <button onClick={logout}>退出登录</button>
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>

              {narrow && (
                <button
                  className="icon-btn"
                  aria-label={menuOpen ? "关闭菜单" : "打开菜单"}
                  aria-expanded={menuOpen}
                  onClick={() => setMenuOpen((v) => !v)}
                >
                  {menuOpen ? <IconClose /> : <IconMenu />}
                </button>
              )}
            </div>
          </div>

          {narrow && menuOpen && (
            <div className="container" style={{ paddingBottom: "var(--sp-4)" }}>
              <nav className="settings-menu" style={{ position: "static", width: "100%", display: "grid", gap: 4 }} aria-label="站点导航">
                {items.map((n) => (
                  <a
                    key={n.to}
                    href={n.to}
                    className="nav-link"
                    style={{ minHeight: 44 }}
                    onClick={(e) => { e.preventDefault(); go(n.to); }}
                    aria-current={n.match(loc.pathname) ? "page" : undefined}
                  >
                    {n.label}
                  </a>
                ))}
                {me ? (
                  <>
                    <a href="/account" className="nav-link" style={{ minHeight: 44 }}
                       onClick={(e) => { e.preventDefault(); go("/account"); }}>
                      账号与安全{me.must_change_password ? "（需改密）" : ""}
                    </a>
                    <button className="nav-link" style={{ minHeight: 44, textAlign: "left" }} onClick={logout}>退出登录</button>
                  </>
                ) : (
                  <>
                    <a href="/login" className="nav-link" style={{ minHeight: 44 }} onClick={(e) => { e.preventDefault(); go("/login"); }}>登录</a>
                    <a href="/register" className="nav-link" style={{ minHeight: 44 }} onClick={(e) => { e.preventDefault(); go("/register"); }}>注册</a>
                  </>
                )}
                {/* 窄屏也要能到达上下文入口（日常=排行榜 / 竞赛=观赛大屏） */}
                <a href={ctxEntry.to} className={`nav-link ctx-entry${ctxEntry.disabled ? " off" : ""}`}
                   style={{ minHeight: 44 }}
                   onClick={(e) => {
                     e.preventDefault();
                     if (ctxEntry.disabled) return;
                     setMenuOpen(false); go(ctxEntry.to.split("#")[0]);
                   }}>
                  {ctxEntry.label}
                </a>
                {/* 窄屏也要能切模式 */}
                <div className="mode-switch" role="group" aria-label="显示模式" style={{ marginTop: 6 }}>
                  <button className={mode === "daily" ? "on" : ""} aria-pressed={mode === "daily"}
                          onClick={() => { setMenuOpen(false); switchMode("daily"); }}>日常操练</button>
                  <button className={mode === "comp" ? "on" : ""} aria-pressed={mode === "comp"}
                          onClick={() => { setMenuOpen(false); switchMode("comp"); }}>
                    正式竞赛{live && <i className="ms-dot" aria-label={liveMatchRunning ? "比赛进行中" : "有比赛待开始"} />}
                  </button>
                </div>
              </nav>
            </div>
          )}
        </header>

        <main ref={mainRef}>{children}</main>

        <footer className="site">
          <div className="footer-inner">
            <span className="brand" style={{ gap: 8 }}>
              <Monogram size={22} />
              <span style={{ fontSize: "var(--fs-sm)", fontWeight: 700 }}>MGUS 国贸枫禾</span>
            </span>
            <span className="meta">House 知识竞赛</span>
          </div>
          {/* 站点署名（产品负责人指定文案，全局显示）。
              ⚠️ 这一行**有意打破"全站无 Emoji"这条视觉红线**：文案里的 🍁 是作者署名的一部分。
              处理方式是"显式白名单"而不是放宽整条规则 ——
              `_verify/emojifinal.cjs` 里把 🍁 与这行署名记为唯一例外，
              其余任何地方出现 Emoji 仍然判失败。判据与出处见该脚本顶部的说明。 */}
          <p className="footer-sign">By.MapleAutumn🍁『时光流转，愿你能与珍爱之人再度重逢』</p>
        </footer>
      </div>
    </>
  );
}


