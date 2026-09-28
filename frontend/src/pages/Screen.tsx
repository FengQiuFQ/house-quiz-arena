import { useEffect, useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { useRt, serverOffset } from "../rt/store";
import Countdown from "../components/Countdown";
import PaperMaterial from "../design/PaperMaterial";
import { useCalmInk } from "../design/useCalmInk";
import { matchScene } from "../design/matchScene";
import { BoardMatrixPanel, QuestionPace, RecentSubmissions, RevealedStandings } from "../components/BigScreenDetail";
import { HOUSE_ICON } from "../design/icons";

/** 匿名投屏：只用 board 聚合数据，不显示题面或个人身份。 */
export default function Screen() {
  const { matchId = "" } = useParams();
  const { snapshot, conn, connectBoard, disconnect } = useRt();
  useEffect(() => { connectBoard(matchId); return () => disconnect(); }, [matchId, connectBoard, disconnect]);
  useCalmInk();
  const offset = useMemo(() => serverOffset(snapshot), [snapshot]);
  const m = snapshot?.match;
  const rd = m?.status === "running" ? snapshot?.round : null;
  const scene = matchScene(m?.status ?? "", rd);
  const houses = snapshot?.progress.houses ?? [];
  const planned = m?.planned_questions ?? 0;
  const total = snapshot?.progress.total_participants ?? 0;
  const submitted = snapshot?.progress.total_submitted ?? 0;
  const done = snapshot?.progress.rounds_done ?? 0;
  const isRelay = m?.competition_mode === "relay";
  const result = snapshot?.result;
  return (
    <div className="container page screen-page broadcast-page">
      <header className="broadcast-head">
        <div><p className="match-eyebrow">MGUS · 四院竞赛 / 现场投屏</p><h1>{m?.title ?? "正在连接比赛"}</h1></div>
        <span className={`conn ${conn === "open" ? "" : "off"}`} role="status"><i />{conn === "open" ? "实时连接" : "连接恢复中"}</span>
      </header>
      {!snapshot ? <div className="match-notice" role="status">正在读取比赛信息。连接后将自动显示。</div> : <>
        <div className="match-meta"><span>{isRelay ? "接力赛 · 各院独立题组" : "抢答赛 · 四院同题"}</span><span>{isRelay ? `每院 ${m?.questions_per_house ?? "—"} 题` : `全场 ${planned} 题`}</span><span>{m?.buzz ? "出题即可作答" : `读题 ${m?.display_seconds ?? 0} 秒后作答`}</span><span className="match-meta-end">不显示题目与答案</span></div>
        <div className={`broadcast-main${result ? " has-result" : ""}`}>
          <section className="match-phase has-ink" aria-label="比赛阶段">
            <PaperMaterial house="dragon" variant="stage" sheen={false} />
            <div className="phase-content"><p className="match-eyebrow">{scene.label}</p><h2>{scene.title}</h2><p className="phase-note">{scene.note}</p>
              {rd && <div className="phase-clock"><Countdown deadline={rd.phase === "open" ? rd.deadline_at : rd.open_at} serverOffset={offset} size="big" /><span>{rd.phase === "open" ? "作答剩余时间" : "距开放作答"}</span></div>}
              {snapshot.reveal && !result && <p className="phase-note">预计公布 {new Date(snapshot.reveal.reveal_at).toLocaleString("zh-CN", { hour12: false })}</p>}
              <dl className="phase-stats"><div><dt>{scene.pregame ? "已签到" : "参赛人数"}</dt><dd className="num">{total}<small>人</small></dd></div><div><dt>{scene.active ? "本题已提交" : "已完成"}</dt><dd className="num">{scene.active ? submitted : done}<small>{scene.active ? "人" : `/ ${planned} 题`}</small></dd></div></dl>
            </div>
          </section>
          {result ? <RevealedStandings result={result} /> : <section className="broadcast-houses" aria-label={scene.pregame ? "四院签到" : "四院提交进度"}>
            <div className="broadcast-section-head"><h2>{scene.pregame ? "四院集结" : "四院进度"}</h2><span>{scene.pregame ? "已签到人数" : scene.active ? "当前题提交人数 / 参赛人数" : "最近一题提交人数 / 参赛人数"}</span></div>
            <div className="broadcast-house-grid">{houses.map(h => {
              const Icon = HOUSE_ICON[h.key];
              const pct = h.total ? Math.round(h.submitted / h.total * 100) : 0;
              const aid = snapshot.progress.aid?.[h.key];
              return <article className={`broadcast-house has-ink house-${h.key}`} key={h.key}>
                <PaperMaterial house={h.key} variant="card" sheen={false} />
                <div className="broadcast-house-name">{Icon && <Icon size={24} />}<h3>{h.name}</h3></div>
                <div className="broadcast-house-count num">{scene.pregame ? h.total : h.submitted}<small>{scene.pregame ? "人已签到" : `/ ${h.total} 人`}</small></div>
                {scene.pregame ? <p className="broadcast-house-note">{h.total ? "已入场，等待开赛" : "等待队员签到"}</p> : <><div className="sb-bar" role="img" aria-label={`${h.name} 已提交 ${pct}%`}><i style={{ width: `${pct}%` }} /></div><p className="broadcast-house-note">{h.total ? `${pct}% 已提交` : "本院暂无参赛者"}{aid && aid.quota > 0 && ` · 援助余量 ${aid.remaining} 次`}</p></>}
              </article>;
            })}</div>
          </section>}
        </div>
        {!scene.pregame && !result && snapshot.matrix && (snapshot.matrix.rounds.length > 0) && <div className="broadcast-matrix"><BoardMatrixPanel matrix={snapshot.matrix} /></div>}
        <footer className="broadcast-foot"><span>{scene.pregame ? "请在个人设备上登录并签到" : "比赛状态与成绩以现场主持人及正式公布为准"}</span><Link to={`/matches/${matchId}/watch`}>切换观赛视图 →</Link></footer>
        <details className="match-details"><summary>本场规则与比赛记录</summary><div className="match-details-content">{m?.scoring_note && <p className="match-rule-copy">{m.scoring_note}</p>}<div className="bd-detail"><QuestionPace timeline={snapshot.timeline?.items ?? []} total={planned} /><RecentSubmissions recent={snapshot.recent ?? []} /></div></div></details>
      </>}
    </div>
  );
}
