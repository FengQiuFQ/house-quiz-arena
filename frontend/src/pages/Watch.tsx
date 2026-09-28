import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { useAuth, useVTNav } from "../App";
import Countdown from "../components/Countdown";
import { useCalmInk } from "../design/useCalmInk";
import PaperMaterial from "../design/PaperMaterial";
import SubmissionMatrix from "../components/SubmissionMatrix";
import { QuestionPace, RecentSubmissions, RevealedStandings } from "../components/BigScreenDetail";
import type { ResultsData } from "../api/types";
import { HOUSE_ICON } from "../design/icons";
import { matchScene } from "../design/matchScene";

interface TimelineItem {
  round_instance_id: string; position: number | null; status: string;
  house_scope: string[] | null; submitted: number; correct: number | null;
  closed: boolean; open_at: string | null; closed_at: string | null; duration_ms: number | null;
}

/** 比赛状态的中文名。**大屏上不能出现 `revealed` 这种内部枚举**：
 *  那是数据库里的值，观众看到只会以为是页面坏了。未收录的值给一个中性说法，
 *  也不显示英文原文 —— 大屏是给学生看的，不是给开发者看的。 */
const MATCH_STATUS_CN: Record<string, string> = {
  draft: "筹备中", lobby: "签到中", running: "进行中", paused: "已暂停",
  locked: "作答结束", calculated: "已结算", reveal_scheduled: "待揭晓",
  revealed: "已揭晓", archived: "已归档", cancelled: "已取消",
};

interface SpecLive {
  match: {
    id: string; title: string; status: string; mode: string;
    planned_questions: number; answer_seconds: number; display_seconds: number;
    live_visibility: string; spectator_mode: string;
    competition_mode?: string; questions_per_house?: number | null; buzz?: boolean;
  };
  server_time: string;
  viewer: { role: string; house_key: string | null; is_admin: boolean; revealed: boolean;
            anonymous?: boolean };
  matrix: {
    rounds: Array<{ index: number; status: string; total_submitted: number }>;
    houses: Array<{
      key: string; name: string; color_light: string; color_deep: string; total: number;
      cells: Array<{ round_index: number; submitted: number; total: number; ratio: number }>;
    }>;
    max_rounds: number; planned_questions: number;
  };
  timeline?: { items: TimelineItem[]; total: number };
  recent?: Array<{ at: string | null; house_key: string; house_name: string; position: number | null }>;
  room: null | {
    round_instance_id: string; position: number | null; total: number;
    phase: "reading" | "answering";
    display_at: string | null; open_at: string | null; deadline_at: string | null;
    prompt?: string;
    options?: Array<{ option_id: string; text: string }>;
  };
  my_house_progress?: { key: string; name: string; total: number; submitted: number };
  progress?: {
    houses: Array<{ key: string; name: string; total: number; submitted: number }>;
    total_participants: number; total_submitted: number; rounds_done: number;
    aid?: Record<string, { remaining: number; quota: number }>;
  };
  result?: ResultsData;
}

/**
 * 揭晓后的名次条见 `components/BigScreenDetail.tsx` 的 `RevealedStandings` ——
 * 投屏页与观赛页共用同一份实现（两页各写一份必然会走样，本项目已经踩过）。
 */

/** 按服务端观赛档位展示；签到、作答、揭晓采用不同信息层级，不显示标准答案。 */
export default function Watch() {
  const { matchId = "" } = useParams();
  const { me } = useAuth();
  const { vtNav } = useVTNav();
  const [err, setErr] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  /* 观赛大屏也是常驻展示页（现场会一直开着），同样用安静材质：
     详见 design/useCalmInk.ts —— 大屏上墨域位移幅度过大会被读成"整块在动"。 */
  useCalmInk();

  const q = useQuery({
    queryKey: ["spectate-live", matchId],
    queryFn: () => api.get<SpecLive>(`/api/v1/matches/${matchId}/spectate/live`),
    refetchInterval: 2000,
    enabled: !!matchId,
  });

  useEffect(() => {
    if (q.error instanceof ApiError) setErr(q.error.message);
    else if (q.data) setErr(null);
  }, [q.error, q.data]);

  const offset = useMemo(() => {
    if (!q.data?.server_time) return 0;
    return new Date(q.data.server_time).getTime() - Date.now();
  }, [q.data?.server_time]);

  // 秒级心跳：让"最后更新 x 秒前"这类只跟时间有关的信息自己走，不必等下一次轮询
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  if (err) {
    return (
      <div className="player narrow">
        <div className="error-box" role="alert">{err}</div>
        <p className="note mt-3">
          {/* 未登录不再是错误（2026-09-26 起观赛页对匿名开放，见后端 /spectate/live 的说明），
              这里只兜真实的失败原因：比赛不存在、限定范围赛未登录、网络中断。 */}
          如果这是限定范围赛，需要登录本院账号；比赛结束后结果对所有人公开。
        </p>
        <div className="row mt-4">
          <button className="btn" onClick={() => vtNav("/", "back")}>返回首页</button>
          {!me && <a className="btn btn-primary" href="/login">去登录</a>}
        </div>
      </div>
    );
  }

  const d = q.data;
  if (!d) return <div className="player narrow"><p className="note">正在连接比赛…</p></div>;
  // ⚠️ 这里原先还有一道 `house_key === null ⇒ 报错` 的"双保险"。
  // 它正是"管理员看不了任意 House"的直接原因：admin 账号没有绑定学院（house_id 为空），
  // 于是 `is_admin=true` 也没用 —— 页面自己把管理员拦在门外了。
  // 现在改为：匿名与无学院账号都正常渲染公开总览，只是没有"本院"那一格。
  const isAnon = d.viewer.anonymous === true || d.viewer.role === "anonymous";

  const mode = d.match.spectator_mode;
  const house = d.viewer.house_key;
  // 匿名与管理员都没有"本院"，材质取一个中性院色（纯装饰，不表达立场）
  const inkHouse = house ?? "dragon";
  const room = d.match.status === "running" ? d.room : null;
  const scene = matchScene(d.match.status, room);
  const houses = d.progress?.houses ?? [];
  const timeline = d.timeline?.items ?? [];
  const recent = d.recent ?? [];
  const isRelay = d.match.competition_mode === "relay";
  const lastUpdate = q.dataUpdatedAt ? Math.max(0, Math.round((Date.now() - q.dataUpdatedAt) / 1000)) : null;
  void tick; // 仅用于触发重渲染，让"最后更新"自己走

  return (
    <div className={`watch-page match-watch${scene.pregame ? " is-pregame" : ""}`}>
      <header className="broadcast-head">
        <div><p className="match-eyebrow">MGUS · 四院竞赛 / 观赛</p><h1>{d.match.title}</h1></div>
        <div className="watch-head-actions">
          <Link className="btn btn-sm" to={`/matches/${matchId}/screen`}>教室投屏</Link>
          {d.match.status === "lobby" && <Link className="btn btn-primary btn-sm" to={`/matches/${matchId}/play`}>我要参赛</Link>}
          <span className={`conn ${lastUpdate !== null && lastUpdate <= 6 ? "" : "off"}`}><i />{lastUpdate !== null && lastUpdate <= 6 ? "实时更新" : "更新延迟"}</span>
        </div>
      </header>
      <div className="match-meta"><span>{MATCH_STATUS_CN[d.match.status] ?? "状态同步中"}</span><span>{isRelay ? "接力赛 · 各院独立题组" : "抢答赛 · 四院同题"}</span><span>{isRelay ? `每院 ${d.match.questions_per_house ?? "—"} 题` : `全场 ${d.match.planned_questions} 题`}</span><span className="match-meta-end">{mode === "mirror" ? "同步题面 · 不含答案" : mode === "blur" ? "题目内容已隐藏" : "仅展示进度"}</span></div>
      <div className={`watch-grid${d.result ? " has-result" : ""}`}>
        <section className="watch-stage match-phase has-ink" aria-label={scene.active && mode === "mirror" ? "当前题目" : "比赛阶段"}>
          <PaperMaterial house={inkHouse} variant="stage" sheen={false} />
          <div className="watch-stage-inner">
            {scene.active && room && mode !== "summary" ? <>
              <div className="watch-question-head"><span>{scene.title} / 共 {room.total} 题</span><div className="ws-timer"><Countdown deadline={room.phase === "answering" ? room.deadline_at : room.open_at} serverOffset={offset} size="big" /><small>{scene.label}</small></div></div>
              {mode === "blur" ? <div className="watch-private"><h2>题目内容已隐藏</h2><p>本场观赛不公开题干与选项，你仍可查看倒计时和四院进度。</p></div> : <div className="ws-q"><p className="ws-prompt">{room.prompt ?? "题目正在同步"}</p><ul className="ws-options">{(room.options ?? []).map((o, i) => <li key={o.option_id}><span className="letter">{String.fromCharCode(65 + i)}</span><span>{o.text}</span></li>)}</ul></div>}
            </> : <div className="phase-content">
              <p className="match-eyebrow">{scene.label}</p><h2>{scene.title}</h2>
              <p className="phase-note">{scene.active && mode === "summary" ? "本场仅展示作答进度，题目不在观赛页公开。" : scene.note}</p>
              {scene.active && room && <div className="phase-clock"><Countdown deadline={room.phase === "answering" ? room.deadline_at : room.open_at} serverOffset={offset} size="big" /><span>{scene.label}</span></div>}
              <dl className="phase-stats"><div><dt>{scene.pregame ? "已签到" : "参赛人数"}</dt><dd className="num">{d.progress?.total_participants ?? 0}<small>人</small></dd></div><div><dt>{scene.active ? "本题已提交" : "已完成"}</dt><dd className="num">{scene.active ? d.progress?.total_submitted ?? 0 : d.progress?.rounds_done ?? 0}<small>{scene.active ? "人" : `/ ${d.match.planned_questions} 题`}</small></dd></div></dl>
              {d.result && <Link className="phase-link" to={`/matches/${matchId}/results`}>查看完整赛果与计分明细 →</Link>}
              {d.match.status === "cancelled" && <Link className="phase-link" to="/">返回首页 →</Link>}
            </div>}
          </div>
        </section>
        {d.result ? <RevealedStandings result={d.result} /> : <aside className="watch-side-col">
          <div className="broadcast-section-head"><h2>{scene.pregame ? "四院签到" : "四院进度"}</h2><span>{scene.pregame ? "已签到人数" : scene.active ? "本题提交 / 参赛" : "最近一题提交 / 参赛"}</span></div>
          <div className="wk-houses" aria-label={scene.pregame ? "四院签到人数" : "四院提交进度"}>
            {houses.map(h => {
              const pct = h.total ? Math.round(h.submitted / h.total * 100) : 0;
              const Icon = HOUSE_ICON[h.key];
              const aid = d.progress?.aid?.[h.key];
              return <div key={h.key} className={`wk-house has-ink house-${h.key}${h.key === house ? " mine" : ""}`}>
                <PaperMaterial house={h.key} variant="strip" sheen={false} />
                <span className="wk-house-name"><span className="wk-house-ico" aria-hidden="true">{Icon && <Icon size={20} />}</span>{h.name}{h.key === house && <i className="wk-you">本院</i>}</span>
                <span className="wk-house-num num">{scene.pregame ? h.total : h.submitted}<small>{scene.pregame ? " 人" : ` / ${h.total}`}</small></span>
                {scene.pregame ? <span className="wk-attendance">{h.total ? "已入场" : "等待签到"}</span> : <>
                  <span className="wk-bar" role="img" aria-label={`${h.name} 已提交 ${pct}%`}><i style={{ width: `${pct}%` }} /></span>
                  {aid && aid.quota > 0 && <span className="wk-aid">援助余量 {aid.remaining} 次</span>}
                </>}
              </div>;
            })}
          </div>
          {scene.pregame ? <div className="watch-attendance-note"><h3>观赛无需签到</h3><p>选手请进入答题页完成签到，并留在等待室；本页用于观看比赛。</p><Link className="phase-link" to={`/matches/${matchId}/play`}>前往参赛入口 →</Link></div> : d.matrix && <SubmissionMatrix matrix={d.matrix} highlight={house} compact />}
        </aside>}
      </div>
      <div className="watch-status-note">{isAnon ? "当前以访客身份观看" : house ? "已标记你的学院" : "全场观赛视图"}<span>{scene.active ? `已完成 ${d.progress?.rounds_done ?? 0} / ${d.match.planned_questions} 题 · 在场 ${d.progress?.total_participants ?? 0} 人` : "页面自动更新，无需刷新"}</span></div>
      {!scene.pregame && <details className="match-details"><summary>查看逐题记录与最近提交</summary><div className="bd-detail"><QuestionPace timeline={timeline} total={Math.max(d.timeline?.total ?? 0, d.match.planned_questions)} /><RecentSubmissions recent={recent} /></div></details>}
    </div>
  );
}
