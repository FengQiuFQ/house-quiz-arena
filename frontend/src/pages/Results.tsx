import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { ResultsData } from "../api/types";
import { getLevel } from "../design/motion";
import PaperMaterial from "../design/PaperMaterial";
import { IconArrowLeft } from "../design/icons";

type Stage = "waiting" | "gather" | "build" | "champion" | "board" | "stable";

/* ── 逐题明细的数据形状（与匿名可读的 GET /api/v1/matches/{id}/board 一致）──
   为什么用 /board 而不是 /replay：
   `/replay` 把"没有会话"的请求一律当作**公开回放**处理，而公开回放按设计
   **绝不下发 `correct`**（走廊大屏不能变成"提前对答案"的通道）。
   结果页是匿名可访问的，于是第一次接 /replay 时整张表的对错列全是"待定" ——
   一行真实数据都没显示出来，等于白做。
   `/board` 是**匿名可读且已经过隐私边界验证**的那一个（投屏大屏就用它）：
   回合关闭后每题的对错份数照给，题目正文/选项/标准答案一律不给。
   逐题的"每院各交了多少"在 matrix 里，这里一并用来做每院的提交数列。 */
interface BoardTimelineLite {
  round_instance_id: string;
  position: number | null;
  status: string;
  submitted: number;
  correct: number | null;
  closed: boolean;
  duration_ms: number | null;
}
interface BoardLite {
  timeline?: { items: BoardTimelineLite[]; total: number };
  matrix?: {
    rounds: Array<{ index: number }>;
    houses: Array<{ key: string; name: string; cells: Array<{ round_index: number; submitted: number; total: number; ratio: number }> }>;
  };
}

const HOUSE_CN: Record<string, string> = {
  tiger: "白虎", dragon: "青龙", tortoise: "玄武", phoenix: "朱雀",
};
const HOUSE_ORDER = ["tiger", "dragon", "tortoise", "phoenix"];

/** 数字只在真实结果到达后递增一次；不循环播放、不随装饰动画重启。 */
function useCountUp(target: number | null, active: boolean, dur = 820): number | null {
  const [v, setV] = useState<number | null>(target);
  useEffect(() => {
    if (target === null) { setV(null); return; }
    if (!active || getLevel() === "reduced") { setV(target); return; }
    let raf = 0;
    const t0 = performance.now();
    const tick = (now: number) => {
      const k = Math.min(1, (now - t0) / dur);
      setV(target * (1 - Math.pow(1 - k, 3)));
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, active, dur]);
  return v;
}

/** 揭晓舞台：集结 → 蓄势 →（服务器放行后）→ 胜者 → 完整榜单 → 稳定。
 *  公开前接口根本不返回成绩（无泄露面）；跳过只在数据可得后生效；重播不改任何比赛状态。 */
export default function Results() {
  const { matchId = "" } = useParams();
  const q = useQuery({
    queryKey: ["results", matchId],
    queryFn: async () => {
      try {
        return { ok: true as const, data: await api.get<ResultsData>(`/api/v1/matches/${matchId}/results`) };
      } catch (e) {
        if (e instanceof ApiError && e.code === "RESULT_NOT_PUBLIC") return { ok: false as const };
        throw e;
      }
    },
    refetchInterval: 3000,
  });
  const data = q.data?.ok ? q.data.data : null;
  const [stage, setStage] = useState<Stage>("waiting");  const timers = useRef<number[]>([]);
  const clearTimers = () => { timers.current.forEach((t) => window.clearTimeout(t)); timers.current = []; };
  const later = (ms: number, fn: () => void) => { timers.current.push(window.setTimeout(fn, ms)); };
  const runStage = useCallback(() => {
    clearTimers();
    if (getLevel() === "reduced") { setStage("stable"); return; }
    setStage("gather");
    later(640, () => setStage("build"));
    later(1380, () => setStage("champion"));
    later(2680, () => setStage("board"));
    later(3400, () => setStage("stable"));
  }, []);

  useEffect(() => () => clearTimers(), []);
  useEffect(() => {
    if (!data) { setStage("waiting"); return; }
    runStage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.result_version]);

  const skip = useCallback(() => { if (data) { clearTimers(); setStage("stable"); } }, [data]);
  const champions = useMemo(() => data?.houses.filter((h) => h.rank === 1) ?? [], [data]);
  const counting = stage === "champion";
  const champScore = useCountUp(champions[0]?.score ?? null, counting);
  const maxScore = data ? Math.max(...data.houses.map((h) => h.score), 1) : 1;
  const inCeremony = stage === "gather" || stage === "build";

  /* 逐题明细。只在成绩已公开后才请求（`enabled`），
     未公开时连请求都不发 —— 与"公开前本页不持有任何成绩数据"同一条边界。
     一次拿够：题目节奏 + 每院每题的提交数。 */
  const detail = useQuery({
    queryKey: ["results-detail", matchId],
    enabled: !!data,
    staleTime: 60_000,
    queryFn: () => api.get<BoardLite>(`/api/v1/matches/${matchId}/board`),
  });

  /** 把 matrix 摊成 "题序号 → 院 → 提交人数"，好和 timeline 的每一行对上。
   *  两者都是按回合先后排列的，用下标对齐；长度不一致时**不猜**，直接不给每院列。 */
  const perHouse = useMemo(() => {
    const m = detail.data?.matrix;
    const tl = detail.data?.timeline?.items ?? [];
    if (!m || m.houses.length === 0 || m.rounds.length !== tl.length) return null;
    return tl.map((_, i) => {
      const cell: Record<string, { submitted: number; total: number }> = {};
      for (const h of m.houses) {
        const c = h.cells[i];
        if (c) cell[h.key] = { submitted: c.submitted, total: c.total };
      }
      return cell;
    });
  }, [detail.data]);

  if (q.isLoading) return <div className="container page loading-screen">加载中…</div>;

  // ── 公开前：四院同等待遇的中性等待 ──
  if (!data) {
    return (
      <div className="container page">
        <div className="rv-stage">
          <div className="rv-orbs">
            {["tiger", "dragon", "tortoise", "phoenix"].map((k, i) => (
              <div key={k} className={`rv-orb has-ink house-${k}`}>
                <PaperMaterial house={k} variant="orb" sheen={false} />
                {["白虎", "青龙", "玄武", "朱雀"][i]}
              </div>
            ))}
          </div>
          <p className="ch-kicker">成绩尚未公布</p>
          <p className="lead">四院同时等待。公布时间以现场主持公告为准；服务器放行前本页不持有任何成绩数据。</p>
          <div className="ch-actions">
            <a className="btn" href={`/matches/${matchId}/screen`}>返回等待画面</a>
            <a className="btn btn-quiet" href="/"><IconArrowLeft size={15} /> 返回首页</a>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="container page">
      <div className="spread">
        <h1 className="section-title wrap-anywhere">{data.match.title}</h1>
        <span className="push tag num">
          {stage === "stable" ? `${data.published_at?.slice(0, 16).replace("T", " ")} 公布` : "揭晓进行中"}
          {data.is_demo && <> · <span className="tag-box">演示数据</span></>}
        </span>
      </div>

      <div className={`rv-stage ${stage}`}>
        {inCeremony && (
          <>
            <div className={`rv-orbs ${stage === "build" ? "rv-build" : ""}`}>
              {data.houses.map((h) => (
                <div key={h.key} className={`rv-orb has-ink house-${h.key}`}>
                  <PaperMaterial house={h.key} variant="orb" sheen={false} />
                  {h.name}
                </div>
              ))}
            </div>
            <p className="ch-kicker">{stage === "gather" ? "四院集结" : "蓄势中"}</p>
          </>
        )}

        {(stage === "champion" || stage === "board" || stage === "stable") && (
          <div className="rv-champion" style={{ width: "100%" }}>
            <p className="ch-kicker">{champions.length > 1 ? "并列冠军" : "本期冠军"}</p>
            <div style={{ display: "flex", gap: "clamp(18px, 5vw, 64px)", justifyContent: "center", flexWrap: "wrap", marginTop: 14 }}>
              {champions.map((h) => (
                <div key={h.key} className={`champion-card has-ink house-${h.key}`}>
                  <PaperMaterial house={h.key} variant="card" />
                  <div className="ck">{h.key}</div>
                  <div className="cn">{h.name}</div>
                  <div className="cs num">
                    {counting && champScore !== null ? champScore.toFixed(2) : h.score.toFixed(2)}
                    <span className="unit">分</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {(stage === "board" || stage === "stable") && (
        <>
          <p className="r-sub center mt-3">
            {/* 原来这里是 `满足正式排名条件` / `不满足正式排名条件：${notes}` —— 那是**数据自检口吻**
                （"本场是否够格计为正式排名"是给复核者看的判据），学生与老师读到只会困惑。
                改为对读者有意义的两句：正式场次直接给结论，未达标的说明原因并给出该怎么办。 */}
            {data.is_official ? "本场已计入正式成绩" : `本场未计入正式成绩（${data.notes ?? "人数或题量未达要求"}），仅供参照`}
            <span className="num"> · 有效题数 {data.valid_question_count} · 成绩版本 v{data.result_version}</span>
          </p>

          <div className={`r-bars mt-5 ${stage === "board" ? "rv-board" : ""}`}>
            {data.houses.map((h) => (
              <div key={h.key} className={`rb-row house-${h.key}`}>
                <span className="rb-name">
                  {h.name}
                  <small>{h.key}{!h.meets_minimum && " · 人数未达标"}</small>
                </span>
                <div className="rb-track"><div className="rb-fill" style={{ width: `${Math.round((h.score / maxScore) * 100)}%` }} /></div>
                <span className="rb-score num">{h.score.toFixed(2)}{h.is_tie && <span className="tie-tag">并列</span>}</span>
              </div>
            ))}
          </div>

          {/* 接力赛：总用时与每题用时是评分依据，必须与分数并列展示。
              不显示 0.0s：没有用时数据的院显示「—」，否则会被读成"瞬间解出"。 */}
          {data.match.competition_mode === "relay" && (
            <div className="table-scroll mt-5">
              <table className="table">
                <caption className="label" style={{ textAlign: "left", paddingBottom: 8 }}>
                  接力赛用时（做完全部题目的净用时；每院每道题以最先解出的那一刻计）
                </caption>
                <thead>
                  <tr>
                    <th scope="col">House</th>
                    <th scope="col">总用时</th>
                    <th scope="col">每题用时</th>
                    <th scope="col">已完成 / 应完成</th>
                    <th scope="col">本场每院题量</th>
                  </tr>
                </thead>
                <tbody>
                  {data.houses.map((h) => (
                    <tr key={h.key}>
                      <td>{h.name}</td>
                      <td className="num">{h.timing?.total_s != null ? `${h.timing.total_s}s` : "—"}</td>
                      <td className="num">{h.timing?.per_question_s != null ? `${h.timing.per_question_s}s` : "—"}</td>
                      <td className="num">{h.timing ? `${h.timing.answered_rounds} / ${h.timing.expected_rounds}` : "—"}</td>
                      <td className="num">{data.match.questions_per_house ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* 计分口径必须原样给出：学生看到的每一分都要能追溯到算法 */}
          <p className="note mt-4">{data.match.scoring_note}</p>

          <div className="stat-grid">
            <div className="stat"><span className="k">完成全场</span><div className="v num">{data.houses.reduce((a, h) => a + h.participants, 0)}<span className="unit">人</span></div><div className="s">四院合计（冻结名单）</div></div>
            <div className="stat"><span className="k">最高个人分</span><div className="v num">{Math.max(...data.houses.flatMap((h) => h.distribution), 0).toFixed(2)}</div><div className="s">不公开姓名</div></div>
            <div className="stat"><span className="k">正确作答用时中位数</span><div className="v num">{data.speed ? `${data.speed.median_s}` : "—"}<span className="unit">{data.speed ? "秒" : ""}</span></div><div className="s">{data.speed?.note ?? "无正确作答样本"}</div></div>
            <div className="stat"><span className="k">援助使用</span><div className="v num">{data.houses.reduce((a, h) => a + h.aid_used, 0)}<span className="unit">次</span></div><div className="s">正确率仅作描述</div></div>
          </div>

          <div className="r-trend">
            <div className="spread">
              <h2 className="subsection-title">个人得分分布（匿名 · 按 House）</h2>
              <span className="push label">样本量 = 各院冻结参赛人数</span>
            </div>
            {data.houses.map((h) => (
              <div className="dist-row" key={h.key}>
                <span style={{ fontSize: "var(--fs-sm)", color: "var(--ink-2)" }}>{h.name}</span>
                <span className="dist-cells">
                  {h.distribution.length === 0
                    ? <span className="num" style={{ color: "var(--ink-3)", fontSize: "var(--fs-xs)" }}>无样本</span>
                    : h.distribution.map((s, i) => <span key={i} className="num">{s.toFixed(2)}</span>)}
                </span>
              </div>
            ))}
          </div>

          <p className="note mt-5">
            {/* 只去掉自检口吻的「平分按精确分数判定并列」（那是判据实现细节）；
                「口径」「冻结名单」等术语**按产品负责人的要求保留**（本次只清自检文案）。 */}
            口径：House 分 = 冻结名单成员个人得分的算术平均（个人分 = 100 × 正确题数 ÷ 有效题数）；
            分数相同即并列；速度仅统计、非计分项。
            {data.notes && <> 备注：{data.notes}</>}
          </p>

          {/* ── 逐题明细：回答"第几题全场答得怎么样、我们院交了多少" ──
              这一块存在的理由就是结果页原先**没有任何下钻入口**：
              看完名次想知道细节只能自己去翻，而这一页本来就有全部数据。 */}
          <section className="r-detail" aria-label="逐题明细">
            <div className="spread">
              <h2 className="subsection-title">逐题明细</h2>
              <span className="push label">
                不含题目正文与标准答案
              </span>
            </div>

            {detail.isLoading && <p className="note">正在载入逐题明细…</p>}
            {detail.isError && (
              <p className="note">逐题明细暂时取不到；总分与名次不受影响，可稍后重试或打开镜像回放。</p>
            )}
            {detail.data && (detail.data.timeline?.items?.length ?? 0) === 0 && (
              <p className="note">这场比赛没有可展示的作答记录。</p>
            )}
            {(detail.data?.timeline?.items?.length ?? 0) > 0 && (
              <div className="table-scroll">
                <table className="table qd-table">
                  <caption className="sr-only">每一题上的提交人数、正确人数与用时</caption>
                  <thead>
                    <tr>
                      <th scope="col">题</th>
                      <th scope="col">提交</th>
                      <th scope="col">正确</th>
                      <th scope="col">正确率</th>
                      <th scope="col">用时</th>
                      {perHouse && HOUSE_ORDER.map((k) => <th key={k} scope="col">{HOUSE_CN[k]}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data!.timeline!.items.map((it, i) => {
                      const pct = it.submitted > 0 && it.correct != null
                        ? `${Math.round((it.correct / it.submitted) * 100)}%` : null;
                      const secs = it.duration_ms != null ? Math.round(it.duration_ms / 1000) : null;
                      return (
                        <tr key={it.round_instance_id}>
                          <th scope="row" className="num">{it.position ?? "备用"}</th>
                          <td className="num">{it.submitted}</td>
                          <td className="num">
                            {it.correct != null ? it.correct : <span className="qd-none">待本题结束</span>}
                          </td>
                          <td className="num">{pct ?? "—"}</td>
                          <td className="num">{secs != null ? `${secs}s` : "—"}</td>
                          {perHouse && HOUSE_ORDER.map((k) => {
                            const c = perHouse[i]?.[k];
                            if (!c) return <td key={k} className="qd-none">—</td>;
                            return (
                              <td key={k} className="num">
                                {c.submitted}<small className="qd-of">/{c.total}</small>
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            <div className="r-detail-actions">
              <a className="btn btn-sm" href={`/matches/${matchId}/replay`}>打开镜像回放</a>
              <a className="btn btn-sm btn-quiet" href={`/matches/${matchId}/screen`}>投屏大屏</a>
              <span className="label">
                回放按服务端记录的作答时间线重放；登录后还能看到每院的逐题用时与对错。
              </span>
            </div>
          </section>
        </>
      )}

      <div className="ch-actions">
        {stage !== "stable" && <button className="btn" onClick={skip}>跳过演出</button>}
        {stage === "stable" && <button className="btn" onClick={runStage}>重播揭晓演出</button>}
        <a className="btn btn-quiet" href="/"><IconArrowLeft size={15} /> 返回首页</a>
      </div>
    </div>
  );
}
