import { useLayoutEffect, useMemo, useRef } from "react";
import { useNavigate, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { flushSync } from "react-dom";
import { api } from "../api/client";
import type { HomeData } from "../api/types";
import { withViewTransition } from "../design/motion";
import { useAuth, useVTNav } from "../App";
import Reveal from "../components/Reveal";
import InkIntro from "../components/InkIntro";
import { useHomeScroll } from "../design/useHomeScroll";
import PaperMaterial from "../design/PaperMaterial";
import CompBanner, { compStateOf, heroCopy } from "../components/CompBanner";
import Standings from "../components/Standings";
import { HOUSE_ICON, IconArrowRight } from "../design/icons";

const HOUSE_ORDER = ["tiger", "dragon", "tortoise", "phoenix"] as const;

function fmtDate(iso?: string | null) {
  if (!iso) return "—";
  return iso.slice(0, 16).replace("T", " ");
}

export default function Home() {
  const { data, isLoading } = useQuery({
    queryKey: ["home"],
    queryFn: () => api.get<HomeData>("/api/v1/home"),
    refetchInterval: (query) => query.state.data?.live_match ? 5000 : 20000,
    refetchIntervalInBackground: false,
  });
  const { vtNav } = useVTNav();
  const { mode } = useAuth();
  const nav = useNavigate();
  const busy = useRef(false);
  const pageRef = useRef<HTMLDivElement>(null);
  useHomeScroll(pageRef, !isLoading && !!data, mode);
  const cardRefs = useRef<Record<string, HTMLElement | null>>({});

  // 只共享水墨材质，正文不参与快照缩放。返回时在绘制前恢复卡片位置。
  useLayoutEffect(() => {
    const key = sessionStorage.getItem("expand-return");
    if (!key || !data) return;
    const saved = sessionStorage.getItem("home-scroll");
    if (saved) window.scrollTo({ top: Number(saved), behavior: "instant" as ScrollBehavior });
    const surface = cardRefs.current[key]?.querySelector<HTMLElement>(".ink-mat");
    surface?.style.setProperty("view-transition-name", "house-hero");
    const timer = window.setTimeout(() => {
      surface?.style.removeProperty("view-transition-name");
      sessionStorage.removeItem("expand-return");
      sessionStorage.removeItem("home-scroll");
    }, 700);
    return () => { window.clearTimeout(timer); surface?.style.removeProperty("view-transition-name"); };
  }, [!!data]);

  const expand = (key: string) => {
    if (busy.current) return;
    busy.current = true;
    const surface = cardRefs.current[key]?.querySelector<HTMLElement>(".ink-mat");
    sessionStorage.setItem("expand-return", key);
    sessionStorage.setItem("home-scroll", String(window.scrollY));
    surface?.style.setProperty("view-transition-name", "house-hero");
    void withViewTransition(() => {
      flushSync(() => nav(`/houses/${key}`));
      window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
    }, "expand").finally(() => {
      busy.current = false;
      surface?.style.removeProperty("view-transition-name");
    });
  };

  const houses = useMemo(
    () => HOUSE_ORDER.map((k) => data?.houses.find((h) => h.key === k)).filter(Boolean) as HomeData["houses"],
    [data],
  );

  /** 竞赛状态：日常 / 待开赛（倒计时）/ 进行中 / 暂停 —— 决定首屏文案与主行动 */
  /* 竞赛状态只用于**竞赛模式**的文案。
     这是模式与状态的正确关系：模式是用户的意图（我在操练 / 我在看比赛），
     状态是系统的事实（现在有没有比赛）。用户选了日常模式，就不该被竞赛文案抢占首屏
     —— 之前两者混用，切到日常却看到"竞赛进行中 / 进入答题"，是真实的设计矛盾。
     日常模式对比赛只做一行轻提示（见下方 dailyNotice），主行动始终是刷题与榜单。 */
  const cstate = compStateOf(data?.live_match ?? null);
  const copy = mode === "comp"
    ? heroCopy(cstate, data?.live_match?.title)
    : {
        kicker: "MGUS 国贸枫禾 · 知识操练",
        h1a: "课间十分钟，",
        h1b: "练几道题，刷题上榜",
        lead: "科学、生活与安全、地理文化、语言趣味、逻辑、科技与 AI。答满 20 题就能上排行榜。",
      };
  const m = data?.live_match;
  /* 日常模式下对比赛的提示：一行字，不抢主行动。
     比赛期间仍要让学生知道"现在有比赛"（否则会以为系统没开），
     但日常模式的主行动始终是"进入答题"（刷题），所以只给一条轻提示。 */
  const dailyNotice = cstate === "live" || cstate === "countdown"
    ? (cstate === "live" ? "比赛正在进行" : "比赛即将开始")
    : null;

  /** 最近比赛：按已公布赛果聚合（同场四院合并为一行，含时间/结果/查看入口） */
  const recent = useMemo(() => {
    const map = new Map<string, {
      match_id: string; title: string; published_at: string;
      is_demo: boolean; rows: Array<{ key: string; name_cn: string; color: string; score: number; rank: number; is_tie: boolean; participants: number }>;
    }>();
    houses.forEach((h) => {
      const l = h.latest;
      if (!l) return;
      let e = map.get(l.match_id);
      if (!e) {
        e = { match_id: l.match_id, title: l.match_title, published_at: l.published_at, is_demo: l.is_demo, rows: [] };
        map.set(l.match_id, e);
      }
      e.rows.push({ key: h.key, name_cn: h.name_cn, color: h.color_deep, score: l.score, rank: l.rank, is_tie: l.is_tie, participants: l.participants });
    });
    return Array.from(map.values())
      .sort((a, b) => (a.published_at < b.published_at ? 1 : -1))
      .slice(0, 4)
      .map((m) => ({ ...m, rows: m.rows.sort((a, b) => a.rank - b.rank || a.key.localeCompare(b.key)) }));
  }, [houses]);

  if (isLoading || !data) return <div className="container page loading-screen">加载中…</div>;

  return (
    <div ref={pageRef} className="container page home-page">
      <InkIntro kicker={copy.kicker} title={copy.h1a} subtitle={copy.h1b} lead={copy.lead}>
          <div className="cta-row">
            {mode === "comp" && m && cstate !== "daily" ? (
              <button
                className="btn btn-primary btn-xl"
                onClick={() => vtNav(`/matches/${m.match_id}/play`, "forward")}
              >
                {cstate === "countdown" ? "签到参赛" : cstate === "paused" ? "返回比赛" : "进入比赛"} <IconArrowRight size={16} />
              </button>
            ) : (
              <button
                className="btn btn-primary btn-xl"
                onClick={() => vtNav("/practice", "forward")}
              >
                进入答题 <IconArrowRight size={16} />
              </button>
            )}
            {/* 日常模式把"练习"放回中央按钮区（它不再出现在顶栏）：
                练习正是日常模式的核心功能，放在主行动旁边最容易被找到。 */}
            {mode === "daily" && (
              <>
                
                <button className="btn btn-lg" onClick={() => vtNav("/practice#board", "forward")}>
                  排行榜
                </button>
              </>
            )}
            {mode === "comp" && (
              <button className="btn btn-lg" onClick={() => vtNav("/practice", "forward")}>
                去练习（日常模式）
              </button>
            )}
            {recent[0] && (
              <button className="btn" onClick={() => vtNav(`/matches/${recent[0].match_id}/results`, "forward")}>
                上一场结果
              </button>
            )}
          </div>
      </InkIntro>
      <div className="home-content" id="home-content">
      <section className="home-overview" aria-label="今日概览">
        <div className="home-section-label"><span>今日 / TODAY</span><span>{mode === "daily" ? "日常 · 积少成多" : "竞赛 · 四院同场"}</span></div>
        {/* 竞赛状态条：**只在竞赛模式出现**。
            日常模式改用下方的一行轻提示 —— 日常模式的主行动是刷题，
            摆一条带大按钮的竞赛横幅会把学生从练习里拽走（这也是"双界面"的意义）。 */}
        {mode === "comp" && (
          <Reveal delay={180}>
            <CompBanner
              match={data.live_match}
              onEnter={() => data.live_match && vtNav(`/matches/${data.live_match.match_id}/play`, "forward")}
              onLobby={() => data.live_match && vtNav(`/matches/${data.live_match.match_id}/play`, "forward")}
              onPractice={() => vtNav("/practice", "forward")}
            />
          </Reveal>
        )}

        {/* 日常模式：比赛只做一行轻提示，可点但不会抢走注意力 */}
        {mode === "daily" && dailyNotice && m && (
          <Reveal delay={180}>
            <p className="daily-notice">
              <span className="dn-dot" aria-hidden="true" />
              {dailyNotice}：{m.title}
              <button className="dn-link" onClick={() => vtNav(`/matches/${m.match_id}/watch`, "forward")}>
                去看看
              </button>
            </p>
          </Reveal>
        )}

        {mode === "comp" && m && (
          <nav className="home-entry-links" aria-label="观看比赛">
            <button onClick={() => vtNav(`/matches/${m.match_id}/watch`, "forward")}>
              <span><b>观赛</b><small>观看当前比赛 · 题目按本场设置显示</small></span><IconArrowRight size={20} />
            </button>
            <button onClick={() => vtNav(`/matches/${m.match_id}/screen`, "forward")}>
              <span><b>教室投屏</b><small>四院进度与赛果 · 不显示题目</small></span><IconArrowRight size={20} />
            </button>
          </nav>
        )}
        {mode === "daily" && (
          <div className="home-practice-note"><span>自由练习，不限次数</span><span>累计答满 <b className="num">20</b> 题即可上榜</span></div>
        )}
        {data.demo && <p className="note mt-3">当前包含示例赛果，正式比赛结果以公布记录为准。</p>}
      </section>

      {/* ── 区二（日常模式）：排行榜主屏 ──
          日常模式的定位是"刷题 + 打榜"，所以中间这块直接给完整榜单：
          前三名领奖台 + 第 4 名以后列表，而不是再摆一遍 House 战绩卡。 */}
      {mode === "daily" && (
        <section className="home-chapter home-rank-chapter" data-scroll-chapter aria-label="日常排行榜">
          <div className="chapter-caption">
            <span className="chapter-index">练习 / RANKING</span>
            <h2>练习排行榜</h2>
            <p>累计答满 20 题参与排名。<br />按正确率、答题量或综合表现查看。</p>
            <a className="chapter-link" href="/leaderboard" onClick={(e) => { e.preventDefault(); vtNav("/leaderboard", "forward"); }}>查看完整榜单 <IconArrowRight size={17} /></a>
            <span className="chapter-track" aria-hidden="true"><i /></span>
          </div>
          <Reveal className="chapter-body"><Standings /></Reveal>
        </section>
      )}

      {mode === "comp" && (
        <section className="section home-rules home-process" aria-labelledby="rules-title">
          <div className="section-head"><h2 id="rules-title" className="section-title">如何参赛</h2><span className="section-aside">从签到到揭晓</span></div>
          <div className="entry-journey has-ink"><PaperMaterial house="dragon" variant="strip" sheen={false} />
          <ol className="entry-steps">
            <li><span className="num">01</span><h3>登录并签到</h3><p>使用自己的学院账号，在开赛前完成签到。</p></li>
            <li><span className="num">02</span><h3>留在等待室</h3><p>确认学院与参赛编号。主持人开赛后，自动进入答题。</p></li>
            <li><span className="num">03</span><h3>作答并确认</h3><p>按本场倒计时提交，以页面的提交成功提示为准。</p></li>
            <li><span className="num">04</span><h3>等待成绩揭晓</h3><p>成绩由主持人统一公布，可在结果页查看计分明细。</p></li>
          </ol></div>
          <details className="home-rules-detail"><summary>赛制有什么区别？</summary><p>抢答赛：四院同题作答。接力赛：各院按分配的题组作答。题量、读题时间与计分规则由本场比赛设定，以赛前说明和结果页为准。</p></details>
        </section>
      )}

      {/* ── 区三：四 House ── */}
      {/* ── 区三：四 House（仅竞赛模式）──
          日常模式不摆 House 战绩卡：它只服务"刷题 + 打榜"这一件事，
          在这里再放一遍学院成绩只会分散注意力（想看学院成绩请切竞赛模式）。 */}
      {mode === "comp" && (
      <section className="section home-houses" aria-labelledby="houses-title">
        <div className="chapter-caption">
          <span className="chapter-index">学院 / HOUSES</span>
          <h2 id="houses-title" className="section-title">四院档案</h2>
          <p>查看学院最近成绩与历次战绩。</p>
          <span className="chapter-track" aria-hidden="true"><i /></span>
        </div>

        <div className="house-grid">
          {houses.map((h, i) => {
            const l = h.latest;
            const Icon = HOUSE_ICON[h.key] ?? HOUSE_ICON.dragon;
            return (
              <Reveal key={h.key} delay={i * 70}>
                <button
                  type="button"
                  data-scroll-panel
                  ref={(el) => { cardRefs.current[h.key] = el; }}
                  className={`house-card has-ink house-${h.key} card-link`}
                  onClick={() => expand(h.key)}
                  aria-label={`${h.name_cn} ${h.name_en}${l ? `，最近一场 ${l.score.toFixed(2)} 分，第 ${l.rank} 名` : "，还没有比赛记录"}`}
                >
                  <PaperMaterial house={h.key} variant="card" />
                  <span className="hc-inner">
                    <span className="house-card-top">
                      <span className="house-badge" aria-hidden="true"><Icon size={20} /></span>
                      <span className="house-names">
                        <span className="house-name-cn">
                          {h.name_cn}
                        </span>
                        <span className="house-name-en">{h.name_en}</span>
                      </span>
                    </span>

                    <span className="hc-main">
                      <span className="house-score-row">
                        {l ? (
                          <>
                            <span className="house-score num">{l.score.toFixed(2)}</span>
                            <span className="house-score-unit">最近一场</span>
                          </>
                        ) : (
                          <span className="house-score void">还没打过</span>
                        )}
                      </span>
                      <span className="hc-sub">
                        {l ? (
                          <>
                            <span>第 {l.rank} 名{l.is_tie ? "（并列）" : ""} · {l.participants} 人上场</span>
                            <span style={{ color: "var(--ink-3)" }}>{l.match_title}</span>
                          </>
                        ) : (
                          <span>这个学院还没有比赛记录。</span>
                        )}
                      </span>
                    </span>

                    <span className="hc-foot">
                      <span>{h.championships} 次夺冠</span>
                      <span className="go">学院详情 <IconArrowRight size={15} /></span>
                    </span>
                  </span>
                </button>
              </Reveal>
            );
          })}
        </div>
      </section>
      )}

      {/* ── 区四：最近赛果 ── */}
      <section className="section home-recent" aria-labelledby="recent-title">
        <Reveal>
          <div className="section-head">
            <div><span className="chapter-index">记录 / RESULTS</span><h2 id="recent-title" className="section-title">赛程与榜单</h2></div>
            <span className="section-aside">已公布的比赛记录</span>
          </div>
        </Reveal>
        <div className="home-record-list">
          <Reveal>
            <div className="panel-card">
              
              <div className="match-list">
                {recent.length === 0 ? (
                  <p className="match-empty">第一场比赛结束后，结果会出现在这里。</p>
                ) : (
                  recent.slice(0, 3).map((m) => (
                    <div className="match-row" key={m.match_id}>
                      <div>
                        <div className="match-title wrap-anywhere">{m.title}</div>
                        <div className="match-when num">
                          {fmtDate(m.published_at)}
                          {m.is_demo && <> · <span className="tag-box">示例数据</span></>}
                        </div>
                      </div>
                      <div className="match-results">
                        {m.rows.filter((r) => r.rank === 1).map((r) => (
                          <span className="match-res" key={r.key}>
                            <span className="dot" style={{ background: r.color }} />
                            <span>{r.name_cn}</span>
                            <b className="num">{r.score.toFixed(2)}</b>
                            <span className="rk">{r.is_tie ? "并列第一" : "第一名"}</span>
                          </span>
                        ))}
                      </div>
                      <Link
                        className="btn btn-sm"
                        to={`/matches/${m.match_id}/results`}
                        onClick={(e) => { e.preventDefault(); vtNav(`/matches/${m.match_id}/results`, "forward"); }}
                      >
                        看结果 <IconArrowRight size={15} />
                      </Link>
                    </div>
                  ))
                )}
              </div>
            </div>
          </Reveal>

        </div>
      </section>
      
      </div>
    </div>
  );
}
