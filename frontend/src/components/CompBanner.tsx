import { useEffect, useMemo, useRef, useState } from "react";
import type { HomeData } from "../api/types";

/**
 * 首页竞赛状态条（三态 + 到点过渡）。
 *
 * 三态由后台驱动，前端只做展示判定：
 *   daily     日常模式：没有进行中/待开赛的比赛 → 主行动是"开始答一题"
 *   countdown 待开赛：match.status === "lobby" 且设了 start_at → 倒计时
 *   live      竞赛进行中：match.status === "running" → "立即参与竞赛"
 *   paused    主持人暂停
 *
 * 两条正确性约束：
 *  1. **倒计时用服务端时间校准**（server_time 与本地时钟求偏移），不信任本机时钟；
 *     现场机器时间不准是常态，用本地时间会给出错误的倒计时。
 *  2. 倒计时归零**不等于开赛**：真正开赛由主持人执行 start_match。
 *     因此归零后显示"等待主持人开赛"，而不是假装已经能答题——否则会出现
 *     "倒计时结束却进不去"的困惑。一旦 status 变为 running，本组件会自动切到"立即参与"。
 */

export type CompState = "daily" | "countdown" | "live" | "paused";

export function compStateOf(m: HomeData["live_match"]): CompState {
  if (!m) return "daily";
  if (m.status === "running") return "live";
  if (m.status === "paused") return "paused";
  if (m.status === "lobby") return "countdown";
  return "daily";
}

/**
 * 服务端时钟偏移（毫秒）。server_time 为空时按 0（退化为本机时钟）。
 *
 * ⚠️ 与 `rt/store.ts` 的 `serverOffset` 同一个坑：**按 server_time 的值缓存**，
 * 不能每次渲染都重算 `server_time - Date.now()`。重算会把倒计时公式里的 `now`
 * 消掉（remain = deadline − server_time，成了常量），倒计时就永久停住不走。
 * 这里不用对象身份做键：轮询每次返回新对象，身份缓存会全部 miss。
 */
const skewByServerTime = new Map<string, number>();

export function serverOffsetMs(m: HomeData["live_match"]): number {
  if (!m?.server_time) return 0;
  const key = m.server_time;
  const hit = skewByServerTime.get(key);
  if (hit !== undefined) return hit;
  const server = new Date(key).getTime();
  const skew = Number.isFinite(server) ? server - Date.now() : 0;
  if (skewByServerTime.size >= 32) {
    for (const k of [...skewByServerTime.keys()].slice(0, 16)) skewByServerTime.delete(k);
  }
  skewByServerTime.set(key, skew);
  return skew;
}

function fmt(secs: number): { d: number; h: number; m: number; s: number } {
  const s = Math.max(0, Math.floor(secs));
  return {
    d: Math.floor(s / 86400),
    h: Math.floor((s % 86400) / 3600),
    m: Math.floor((s % 3600) / 60),
    s: s % 60,
  };
}

const pad = (n: number) => String(n).padStart(2, "0");

export default function CompBanner({
  match, onEnter, onLobby, onPractice,
}: {
  match: HomeData["live_match"];
  onEnter: () => void;
  onLobby: () => void;
  onPractice: () => void;
}) {
  const state = compStateOf(match);
  const offset = useMemo(() => serverOffsetMs(match), [match]);
  const targetRef = useRef<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [fired, setFired] = useState(false);

  targetRef.current = match?.start_at ? new Date(match.start_at).getTime() : null;

  // 本地每秒走一格（展示用）；服务端数据本身由 useQuery 轮询刷新
  useEffect(() => {
    if (state !== "countdown" || targetRef.current === null) return;
    const t = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(t);
  }, [state, match?.start_at]);

  const remainSec = targetRef.current === null ? null
    : Math.max(0, (targetRef.current - (now + offset)) / 1000);
  const reached = remainSec !== null && remainSec <= 0;

  // 到点：切一次 ready 态并播放一次过渡（不重复触发）
  useEffect(() => {
    if (state !== "countdown" || !reached || fired) return;
    setFired(true);
  }, [state, reached, fired]);
  useEffect(() => { setFired(false); }, [match?.match_id, match?.status]);

  // ── 竞赛进行中 ──
  if (state === "live") {
    return (
      <div className="comp-banner live state-enter" role="status">
        <div className="cb-left">
          <span className="cb-kicker">竞赛进行中</span>
          <strong className="cb-title">{match!.title}</strong>
          <span className="cb-meta num">
            {match!.checked_in ?? 0} 人已签到
            {match!.planned_questions ? ` · 共 ${match!.planned_questions} 题` : ""}
          </span>
        </div>
        {/* 主行动：比赛进行中时「进入答题」是最重要的按钮，给最大尺寸与实底强调。
            「等待室」降级为次级按钮 —— 已经开赛了，等待室不是主要去处。 */}
        <div className="cb-actions cb-actions-main">
          <button className="btn btn-primary btn-xl" onClick={onEnter}>
            进入答题
          </button>
          
        </div>
      </div>
    );
  }

  // ── 主持人暂停 ──
  if (state === "paused") {
    return (
      <div className="comp-banner paused state-enter" role="status">
        <div className="cb-left">
          <span className="cb-kicker">比赛暂停中</span>
          <strong className="cb-title">{match!.title}</strong>
          <span className="cb-meta">已提交的答案已保存。返回比赛页面，恢复后可继续作答。</span>
        </div>
        <button className="btn btn-primary" onClick={onEnter}>返回比赛</button>
        <button className="btn btn-primary" onClick={onEnter}>返回比赛</button>
      </div>
    );
  }

  // ── 待开赛：倒计时 ──
  if (state === "countdown") {
    const t = fmt(remainSec ?? 0);
    return (
      <div className={`comp-banner countdown${reached ? " ready" : ""}`} role="status"
           aria-live="polite">
        <div className="cb-left">
          <span className="cb-kicker">{reached ? "即将开始" : "即将开赛"}</span>
          <strong className="cb-title">{match!.title}</strong>
          <span className="cb-meta num">
            {match!.checked_in ?? 0} 人已签到
            {match!.planned_questions ? ` · 共 ${match!.planned_questions} 题` : ""}
          </span>
        </div>

        {remainSec === null ? (
          /* 没有预定开赛时间：不伪造倒计时，显示"即将开始" */
          <div className="cb-actions cb-actions-main">
            <button className="btn btn-primary btn-xl" onClick={onLobby}>进入等待室</button>
          </div>
        ) : reached ? (
          /* 归零 ≠ 已开赛：真正开赛由主持人执行。这里给等待态而非假按钮。 */
          <div className="cb-actions cb-actions-main ready-actions">
            <button className="btn btn-primary btn-xl" onClick={onLobby}>
              进入等待室
            </button>
          </div>
        ) : (
          <>
            <div className="cb-clock" aria-label="距开赛剩余时间">
              {t.d > 0 && (
                <span className="cb-unit"><b className="num">{t.d}</b><i>天</i></span>
              )}
              <span className="cb-unit"><b className="num">{pad(t.h)}</b><i>时</i></span>
              <span className="cb-unit"><b className="num">{pad(t.m)}</b><i>分</i></span>
              <span className="cb-unit"><b className="num">{pad(t.s)}</b><i>秒</i></span>
            </div>
            {/* 倒计时期间必须给得出去处：学生看到倒计时却无处可等是最糟的体验。
                进等待室后可签到、看已签到人数，开赛后自动进入答题。 */}
            <div className="cb-actions cb-actions-main">
              <button className="btn btn-primary btn-xl" onClick={onLobby}>进入等待室</button>
            </div>
          </>
        )}
      </div>
    );
  }

  // ── 日常模式 ──
  return (
    <div className="comp-banner daily state-enter">
      <div className="cb-left">
        <span className="cb-kicker">日常模式</span>
        <strong className="cb-title">现在没有正在进行的比赛</strong>
        <span className="cb-meta">随时练一题，答满 20 题就能上排行榜。</span>
      </div>
      <div className="cb-actions cb-actions-main">
        <button className="btn btn-primary btn-xl" onClick={onPractice}>开始答一题</button>
      </div>
    </div>
  );
}

/** 供首页主标题按状态切换文案：竞赛期用竞赛语气，日常用日常语气。 */
export function heroCopy(state: CompState, title?: string) {
  switch (state) {
    case "live":
      return { kicker: "竞赛进行中", h1a: "四个学院，", h1b: "现在就在答题", lead: title ? `${title} 正在进行。进入后按统一回合作答。` : "比赛正在进行，进入后按统一回合作答。" };
    case "countdown":
      return { kicker: "即将开赛", h1a: "四院集结，", h1b: "准备好再出发", lead: title ? `${title} 即将开始。提前进入等待室，开赛后自动进入答题。` : "提前进入等待室，开赛后自动进入答题。" };
    case "paused":
      return { kicker: "比赛暂停中", h1a: "稍等一下，", h1b: "马上继续", lead: "主持人暂停了比赛。返回比赛页面，恢复后可继续作答。" };
    default:
      return { kicker: "MGUS 国贸枫禾 · House 知识竞赛", h1a: "课间十分钟，", h1b: "为学院赢一分", lead: "科学、生活与安全、地理文化、语言趣味、逻辑、科技与 AI。随手练一题，或者和另外三个学院同场比一场。" };
  }
}
