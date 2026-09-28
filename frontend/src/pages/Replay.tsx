import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import PaperMaterial from "../design/PaperMaterial";
import { IconArrowLeft, IconClock, IconPeople } from "../design/icons";

/* ── 契约（与 GET /api/v1/matches/{id}/replay 一一对应）──
   这几个类型只服务本页，因此留在页面内，不去污染公共的 api/types.ts。 */
interface ReplayRound {
  round_instance_id: string;
  status: string;
  house_scope: string[] | null;
  position: number | null;
  is_backup: boolean;
  open_at: string | null;
  deadline_at: string | null;
  offset_ms: number | null;
}

interface ReplayEvent {
  round_instance_id: string;
  house: string | null;
  alias: string;
  option_id: string;
  elapsed_ms: number | null;
  received_at: string;
  correct: boolean | null;
}

interface ReplayData {
  match: {
    id: string;
    title: string;
    status: string;
    competition_mode: "shared_buzz" | "relay";
    questions_per_house: number | null;
    revealed: boolean;
  };
  server_time: string;
  viewer: { house_key: string | null; is_admin: boolean };
  started_at: string | null;
  rounds: ReplayRound[];
  events: ReplayEvent[];
  note: string;
}

const HOUSE_KEYS = ["tiger", "dragon", "tortoise", "phoenix"];
/** 接口只给 house key，中文院名是固定的四院常量（不是接口字段）。 */
const HOUSE_CN: Record<string, string> = { tiger: "白虎", dragon: "青龙", tortoise: "玄武", phoenix: "朱雀" };
const MODE_CN: Record<string, string> = { shared_buzz: "抢答赛", relay: "接力赛" };
/** 回合状态：服务端英文枚举 → 现场用语；出现未收录的值时原样显示，绝不猜。 */
const ROUND_CN: Record<string, string> = {
  pending: "未开始", displaying: "读题中", open: "作答中", closed: "已结束", void: "已作废",
};
/** 状态胶囊的点色语义与观战页保持一致：进行中=红点、已结束=绿点、已作废=黄点。 */
const ROUND_TAG: Record<string, string> = {
  pending: "", displaying: "", open: "live", closed: "ok", void: "warn",
};
const MATCH_CN: Record<string, string> = {
  draft: "草稿", lobby: "等待开赛", running: "进行中", paused: "暂停", locked: "已锁定",
  calculated: "已核算", reveal_scheduled: "待揭晓", revealed: "已揭晓",
  archived: "已归档", cancelled: "已取消",
};

/** 播放步进：100ms 一跳 = 1× 实时。回放的价值就在于重看现场节奏，
 *  走得比实时快就退化成"进度条动画"；要跳着看用滑块。 */
const STEP_MS = 100;

/** 用时一律"秒 + 一位小数"：回放里毫秒没有信息量，也不该与别处口径不同。 */
function fmtSec(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}

function houseName(key: string | null): string {
  return key ? HOUSE_CN[key] ?? key : "未知学院";
}

/** 院色只挂在 .house-* 上；未知 key 不加类，否则会继承上一个元素的院色。 */
function houseClass(key: string | null): string {
  return key && HOUSE_KEYS.includes(key) ? `house-${key}` : "";
}

/** 同一题内按用时升序；用时缺失的排最后，用受理时间兜底以保证顺序稳定。 */
function compareEvents(a: ReplayEvent, b: ReplayEvent): number {
  if (a.elapsed_ms === null && b.elapsed_ms === null) return a.received_at.localeCompare(b.received_at);
  if (a.elapsed_ms === null) return 1;
  if (b.elapsed_ms === null) return -1;
  return a.elapsed_ms - b.elapsed_ms || a.received_at.localeCompare(b.received_at);
}

/** 作答窗口 = 截止 − 开题。两个时间戳都由服务端下发，缺任一个就不画窗口条。 */
function windowOf(r: ReplayRound): number | null {
  if (!r.open_at || !r.deadline_at) return null;
  const a = Date.parse(r.open_at);
  const b = Date.parse(r.deadline_at);
  if (Number.isNaN(a) || Number.isNaN(b) || b <= a) return null;
  return b - a;
}

interface Mark {
  key: string;
  house: string | null;
  alias: string;
  elapsed: number | null;
  /** 在全局时间轴上的位置（ms）：本题开题偏移 + 本人用时。 */
  at: number;
  correct: boolean | null;
}

interface TLineRound {
  id: string;
  title: string;
  hasPosition: boolean;
  isBackup: boolean;
  status: string;
  /** house_scope 里属于四院的 key；为空表示这一题没有限定学院（四院共同）。 */
  houses: string[];
  scopeLabel: string;
  offset: number;
  windowMs: number | null;
  marks: Mark[];
}

interface Timeline {
  rounds: TLineRound[];
  span: number;
  eventCount: number;
}

/**
 * 把回放响应整理成"一条绝对时间轴 + 每题标记"的结构。
 *
 * 为什么标记位置用 `开题偏移 + 本题用时`，而不是只用用时：
 * 两种赛制下"用时"都是相对**本题开题**的，直接用它会把所有题压到同一段 0–N 秒里，
 * 接力赛里第 1 题和第 5 题的作答会叠在一起，"像录像一样重看"就没了。
 * 加上开题偏移后，各行共用同一条横轴，节奏才与现场一致。
 * 偏移缺失时（服务端没记 open_at）偏移按 0 处理，此时等价于"只有用时"的简单时间轴。
 */
function buildTimeline(d: ReplayData | null): Timeline {
  if (!d) return { rounds: [], span: 0, eventCount: 0 };

  const byRound = new Map<string, ReplayEvent[]>();
  for (const ev of d.events) {
    const arr = byRound.get(ev.round_instance_id);
    if (arr) arr.push(ev);
    else byRound.set(ev.round_instance_id, [ev]);
  }

  const rounds: TLineRound[] = d.rounds.map((r) => {
    const offset = r.offset_ms !== null ? Math.max(0, r.offset_ms) : 0;
    const scope = (r.house_scope ?? []).filter((k) => HOUSE_KEYS.includes(k));
    const marks: Mark[] = (byRound.get(r.round_instance_id) ?? [])
      .slice()
      .sort(compareEvents)
      .map((ev, i) => {
        const elapsed = ev.elapsed_ms !== null ? Math.max(0, ev.elapsed_ms) : null;
        return {
          key: `${ev.round_instance_id}-${ev.alias}-${i}`,
          house: ev.house,
          alias: ev.alias,
          elapsed,
          at: offset + (elapsed ?? 0),
          correct: ev.correct,
        };
      });
    return {
      id: r.round_instance_id,
      title: r.position !== null ? `第 ${r.position} 题` : "备用题",
      hasPosition: r.position !== null,
      isBackup: r.is_backup,
      status: r.status,
      houses: scope,
      // 抢答赛天然四院同题；接力赛看服务端下发的 scope，没限定则视为四院共同
      scopeLabel: d.match.competition_mode === "relay"
        ? (scope.length > 0 ? scope.map((k) => houseName(k)).join("、") : "四院共同")
        : "四院同题",
      offset,
      windowMs: windowOf(r),
      marks,
    };
  });

  const maxOffset = rounds.reduce((m, r) => Math.max(m, r.offset), 0);
  const maxElapsed = rounds.reduce(
    (m, r) => r.marks.reduce((n, k) => Math.max(n, k.elapsed ?? 0), m), 0);
  const maxAt = rounds.reduce((m, r) => r.marks.reduce((n, k) => Math.max(n, k.at), m), 0);
  // 总长取「最大开题偏移 + 最大单题用时」，并兜住"偏移最大的那题恰恰用时也最长"的情况，
  // 否则最后一条作答会被切在时间轴之外。
  return {
    rounds,
    span: Math.max(maxOffset + maxElapsed, maxAt),
    eventCount: d.events.length,
  };
}

/** 窗口条右端可能超出时间轴（截止时刻晚于最后一条作答）——按剩余长度截断，避免越界。 */
function clampWidth(start: number, len: number): number {
  return Math.max(0, Math.min(100 - start, len));
}

/** 对错：只在服务端给出时才显示。未揭晓时 correct 为 null，
 *  这里刻意不做任何推断——回放不能变成提前对答案的通道。 */
function Verdict({ correct }: { correct: boolean | null }) {
  if (correct === true) return <span className="rp-ok">对</span>;
  if (correct === false) return <span className="rp-no">错</span>;
  return <span className="rp-hidden">成绩公布后揭晓</span>;
}

/**
 * 镜像回放（/matches/:matchId/replay）。
 *
 * 与观战页的分工：观战页回答"此刻场上是什么样"，回放页回答"刚才每一步是谁、用了多久"。
 * 数据全部来自服务端记录的作答时间线（answers.received_at / elapsed_ms），
 * 因此不做任何推断：没有的字段就不显示，未揭晓的对错就写成"待揭晓"。
 */
export default function Replay() {
  const { matchId = "" } = useParams();

  const q = useQuery({
    queryKey: ["replay", matchId],
    queryFn: () => api.get<ReplayData>(`/api/v1/matches/${matchId}/replay`),
    enabled: !!matchId,
    // 回放本身是历史数据，不需要实时刷新；唯一的活值是"对错"，
    // 它在结果公布后才会下发，所以只在未揭晓时低频轮询，揭晓后彻底停下。
    refetchInterval: (query) =>
      query.state.data && !query.state.data.match.revealed ? 20000 : false,
  });

  const d = q.data ?? null;
  const tl = useMemo(() => buildTimeline(d), [d]);
  const span = tl.span;

  // 播放时钟（ms）。与接口数据分离，refetch 不会打断正在进行的播放。
  const [clock, setClock] = useState(0);
  const [playing, setPlaying] = useState(false);

  // 换一场比赛（同组件实例、参数变化）时必须清空时钟，否则新回放会从旧进度开始。
  useEffect(() => {
    setClock(0);
    setPlaying(false);
  }, [matchId]);

  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => {
      setClock((c) => Math.min(span, c + STEP_MS));
    }, STEP_MS);
    // 卸载或暂停时一定要停表：否则离开页面后计时器还在推 state（React 会警告、也浪费电）。
    return () => window.clearInterval(id);
  }, [playing, span]);

  // 走到终点自动停表。单独一个 effect 而不是写在 setClock 的更新函数里——
  // 更新函数必须是纯的（StrictMode 下会被调用两次，副作用会翻倍）。
  useEffect(() => {
    if (playing && clock >= span) setPlaying(false);
  }, [playing, clock, span]);

  const relayStats = useMemo(() => {
    if (!d || d.match.competition_mode !== "relay") return null;
    const acc = HOUSE_KEYS.map((key) => ({ key, assigned: 0, answered: 0, netMs: 0, noTime: 0, timed: 0 }));
    for (const r of tl.rounds) {
      // 备用题不计入分配：它只在正题作废时才会启用，算进去会让"每院 N 题"比后台设置的多，
      // 观者会以为有题目被漏答。
      if (!r.hasPosition) continue;
      for (const a of acc) {
        // 没限定学院的题（scope 为空）按四院都答来算——服务端不下发限定就等于四院共同作答
        if (r.houses.length > 0 && !r.houses.includes(a.key)) continue;
        a.assigned += 1;
        let seen = 0;
        let best: number | null = null;
        for (const m of r.marks) {
          if (m.house !== a.key) continue;
          seen += 1;
          // 一院一题可能有多条受理记录（多人抢按），服务端的"该题用时"取最早一次，
          // 所以这里也取最小值：求平均会把净用时算大，等于改了评分口径。
          if (m.elapsed !== null && (best === null || m.elapsed < best)) best = m.elapsed;
        }
        if (seen > 0) {
          a.answered += 1;
          // 用时缺失（服务端没记下 elapsed_ms）要单独计数：把它当 0 秒会让这一院
          // 看起来是"瞬间解出"，那是凭空造出来的最好成绩。
          if (best === null) a.noTime += 1;
        }
        if (best !== null) {
          a.netMs += best;
          a.timed += 1;
        }
      }
    }
    const slowest = acc.reduce((m, a) => Math.max(m, a.netMs), 0);
    const mine = d.viewer.house_key;
    return acc.map((a) => ({
      ...a,
      mine: a.key === mine,
      barPct: slowest > 0 ? (a.netMs / slowest) * 100 : 0,
    }));
  }, [d, tl]);

  const buzzStats = useMemo(() => {
    if (!d || d.match.competition_mode !== "shared_buzz") return null;
    const mine = d.viewer.house_key;
    return HOUSE_KEYS.map((key) => {
      let answers = 0;
      let fastest: number | null = null;
      for (const r of tl.rounds) {
        for (const m of r.marks) {
          if (m.house !== key) continue;
          answers += 1;
          if (m.elapsed !== null && (fastest === null || m.elapsed < fastest)) fastest = m.elapsed;
        }
      }
      return { key, answers, fastest, mine: key === mine };
    });
  }, [d, tl]);

  if (q.isLoading) return <div className="container page loading-screen">载入中…</div>;

  if (q.error) {
    const needLogin = q.error instanceof ApiError && q.error.status === 401;
    const msg = q.error instanceof ApiError ? q.error.message : "回放载入失败，请稍后重试。";
    return (
      <div className="container page narrow">
        <div className="error-box" role="alert">{msg}</div>
        <div className="row mt-4">
          {needLogin && (
            <a className="btn btn-primary" href={`/login?next=/matches/${matchId}/replay`}>去登录后查看</a>
          )}
          <Link className="btn" to="/"><IconArrowLeft size={15} /> 返回首页</Link>
        </div>
      </div>
    );
  }

  if (!d) return <div className="container page loading-screen">载入中…</div>;

  const mode = d.match.competition_mode;
  const now = Math.min(clock, span);
  const hasEvents = tl.eventCount > 0 && span > 0;
  const mainCount = tl.rounds.filter((r) => r.hasPosition).length;
  const backupCount = tl.rounds.length - mainCount;

  const pctNum = (ms: number): number => (span > 0 ? Math.min(100, Math.max(0, (ms / span) * 100)) : 0);

  const playLabel = playing ? "暂停" : now >= span && hasEvents ? "重新播放" : now > 0 ? "继续回放" : "播放回放";
  const togglePlay = () => {
    if (!playing && hasEvents && now >= span) setClock(0); // 播完后再点 = 从头重播
    setPlaying((p) => !p);
  };
  const reset = () => {
    setPlaying(false);
    setClock(0);
  };

  // 抬头的水墨用观者所在学院的墨色；没有学院（或无 key）时用青龙兜底，
  // 这样匿名的管理员看到的也是一块完整的材质，而不是一片空底。
  const viewerKey = d.viewer.house_key;
  const heroHouse = viewerKey && HOUSE_KEYS.includes(viewerKey) ? viewerKey : "dragon";

  return (
    <div className="rp-page">
      <header className="rp-hero has-ink">
        <PaperMaterial house={heroHouse} variant="stage" sheen={false} />
        <div className="rp-hero-inner">
          <p className="rp-kicker">镜像回放 · {MODE_CN[mode] ?? mode}</p>
          <h1 className="rp-title">{d.match.title}</h1>
          <div className="row rp-hero-tags">
            <span className={`tag ${d.match.revealed ? "ok" : ""}`}>
              {MATCH_CN[d.match.status] ?? d.match.status}
            </span>
            <span className="tag">{MODE_CN[mode] ?? mode}</span>
            {mode === "relay" && d.match.questions_per_house !== null && (
              <span className="tag">每院 {d.match.questions_per_house} 题</span>
            )}
            <span className="tag">时间轴 {fmtSec(span)}</span>
            {viewerKey && (
              <span className={`tag rp-tag-house ${houseClass(viewerKey)}`}>
                本院：{houseName(viewerKey)}
              </span>
            )}
          </div>
          <p className="rp-note">{d.note}</p>
          <div className="row rp-hero-actions">
            <Link className="btn" to={`/matches/${matchId}/watch`}>去观战页看当前进度</Link>
            <Link className="btn btn-quiet" to="/"><IconArrowLeft size={15} /> 返回首页</Link>
          </div>
        </div>
      </header>

      <div className="rp-grid">
        {/* ── 左：播放控制 + 题目时间线 ── */}
        <section className="rp-main" aria-label="回放时间线">
          <div className="rp-controls">
            <button className="btn btn-primary" onClick={togglePlay} disabled={!hasEvents}>
              {playLabel}
            </button>
            <button className="btn" onClick={reset} disabled={now === 0 && !playing}>重置</button>
            <span className="rp-clock">
              <IconClock size={15} />
              <b className="num">{fmtSec(now)}</b>
              <small className="num">/ {fmtSec(span)}</small>
            </span>
            <input
              className="rp-slider"
              type="range"
              min={0}
              max={Math.max(span, STEP_MS)}
              step={STEP_MS}
              value={now}
              disabled={!hasEvents}
              onChange={(e) => setClock(Number(e.target.value))}
              aria-label="回放进度"
            />
          </div>
          <p className="note mt-3">
            时间线按服务端的开题时刻与受理用时铺开，横轴为全场时间；播放以 1× 推进，
            也可拖动滑块跳到任意时刻。时钟未走到的事件淡显，走到后按现场顺序亮起。
          </p>

          {tl.rounds.length === 0 ? (
            <div className="info-box">这场比赛还没有开始，暂时没有可回放的回合。</div>
          ) : (
            <ol className="rp-timeline">
              {tl.rounds.map((r) => (
                <li key={r.id} className={`rp-row ${r.isBackup ? "rp-backup" : ""}`}>
                  <div className="rp-q">
                    <p className="rp-q-title">{r.title}</p>
                    <div className="rp-tags">
                      <span className={`tag ${ROUND_TAG[r.status] ?? ""}`}>
                        {ROUND_CN[r.status] ?? r.status}
                      </span>
                      <span className="tag">{r.scopeLabel}</span>
                    </div>
                  </div>

                  <div className="rp-track" aria-hidden="true">
                    {r.windowMs !== null && (
                      <i
                        className="rp-window"
                        style={{
                          left: `${pctNum(r.offset)}%`,
                          width: `${clampWidth(pctNum(r.offset), pctNum(r.windowMs))}%`,
                        }}
                      />
                    )}
                    {r.marks.map((m) => (
                      <i
                        key={m.key}
                        className={`rp-mark ${houseClass(m.house)} ${m.at <= now ? "on" : ""}`}
                        style={{ left: `${pctNum(m.at)}%` }}
                      />
                    ))}
                    <i className="rp-play" style={{ left: `${pctNum(now)}%` }} />
                  </div>

                  <div className="rp-acts">
                    {r.marks.length === 0 ? (
                      <span className="rp-empty">没有收到作答</span>
                    ) : (
                      r.marks.map((m) => (
                        <span
                          key={m.key}
                          className={`rp-ev ${houseClass(m.house)} ${m.at <= now ? "" : "future"}`}
                        >
                          <b className="rp-ev-alias num">{m.alias}</b>
                          <span className="rp-ev-house">{houseName(m.house)}</span>
                          <span className="rp-ev-time num">
                            {m.elapsed !== null ? fmtSec(m.elapsed) : "用时未记录"}
                          </span>
                          {m.at <= now && <Verdict correct={m.correct} />}
                        </span>
                      ))
                    )}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </section>

        {/* ── 右：学院口径 ── */}
        <aside className="rp-side">
          {relayStats ? (
            <section className="rp-panel" aria-label="各院净用时">
              <div className="rp-panel-head">
                <IconPeople size={16} />
                <h2 className="rp-panel-title">各院净用时</h2>
              </div>
              <p className="rp-panel-sub">净用时＝该院每道题最早一次作答的用时之和。</p>
              <div className="rp-houses">
                {relayStats.map((s) => (
                  <div key={s.key} className={`rp-house ${houseClass(s.key)} ${s.mine ? "mine" : ""}`}>
                    <div className="rp-house-head">
                      <span className="rp-house-name">{houseName(s.key)}</span>
                      {s.mine && <em className="rp-house-mine">本院</em>}
                    </div>
                    <p className="rp-net num">{s.timed > 0 ? fmtSec(s.netMs) : "—"}</p>
                    <div className="rp-bar"><i style={{ width: `${s.barPct}%` }} /></div>
                    <p className="rp-house-nums">
                      <span>分配 <b className="num">{s.assigned}</b> 题</span>
                      <span>已答 <b className="num">{s.answered}</b> 题</span>
                      {s.assigned > s.answered && (
                        <span>缺答 <b className="num">{s.assigned - s.answered}</b> 题</span>
                      )}
                      {s.noTime > 0 && (
                        <span>用时未记录 <b className="num">{s.noTime}</b> 题</span>
                      )}
                    </p>
                  </div>
                ))}
              </div>
              <p className="rp-panel-note">
                一道题在该院解出后即结束计时；未作答或未记录用时的题不计入净用时（无可用用时显示「—」），
                备用题不计入分配。条形按占最慢一院的比例绘制，越长表示用时越久。
              </p>
            </section>
          ) : (
            <section className="rp-panel" aria-label="本场概览">
              <div className="rp-panel-head">
                <IconClock size={16} />
                <h2 className="rp-panel-title">本场概览</h2>
              </div>
              <div className="rp-facts">
                <span>正题 <b className="num">{mainCount}</b> 题</span>
                <span>备用 <b className="num">{backupCount}</b> 题</span>
                <span>作答记录 <b className="num">{tl.eventCount}</b> 次</span>
                <span>时间轴总长 <b className="num">{fmtSec(span)}</b></span>
              </div>
              <div className="rp-houses">
                {(buzzStats ?? []).map((s) => (
                  <div key={s.key} className={`rp-house ${houseClass(s.key)} ${s.mine ? "mine" : ""}`}>
                    <div className="rp-house-head">
                      <span className="rp-house-name">{houseName(s.key)}</span>
                      {s.mine && <em className="rp-house-mine">本院</em>}
                    </div>
                    <p className="rp-house-nums">
                      <span>作答 <b className="num">{s.answers}</b> 次</span>
                      <span>最快 <b className="num">{s.fastest !== null ? fmtSec(s.fastest) : "—"}</b></span>
                    </p>
                  </div>
                ))}
              </div>
              <p className="rp-panel-note">
                {/* 原文后半句是「「净用时」是接力赛的口径，本场不适用」——那是写给"两种赛制都清楚的人"
                    的自检备注；对看回放的观众，只需要说清这里的用时意味着什么。 */}
                抢答赛四院同题，这里的用时只反映按键快慢，不参与计分。
              </p>
            </section>
          )}
        </aside>
      </div>
    </div>
  );
}
