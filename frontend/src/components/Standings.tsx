import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useAuth, useVTNav } from "../App";
import { IconArrowRight } from "../design/icons";

interface LbItem {
  rank: number; display_name: string; answered_count: number; correct_count: number;
  accuracy: number; best_streak: number; avg_correct_ms: number | null; speed_sample: number;
}
interface LbData {
  board: string; min_questions: number; items: LbItem[]; note: string;
}

const BOARDS = [
  { k: "accuracy", label: "正确率榜", unit: "%" },
  { k: "speed", label: "速度榜", unit: "s" },
  { k: "streak", label: "连对榜", unit: "连对" },
] as const;
type BoardKey = typeof BOARDS[number]["k"];

function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}` : `${ms}`;
}

/**
 * 日常模式的排行榜主屏。
 *
 * 这是日常模式的**唯一主体**：学生来这里只做两件事——刷题、看自己排到哪了。
 * 因此页面不做"英雄区 + 特色卡"那套叙事，直接把榜单铺满，
 * 前三名用领奖台形式给出**完整数据**（正确率 / 用时 / 连对 / 题量），
 * 而不是只给一个名次 —— 学生想知道的是"我差在哪一项"。
 *
 * 名次顺序：视觉上 2-1-3（领奖台惯例），DOM 仍为 1-2-3，
 * 读屏听到的顺序才是正确名次；视觉顺序由 CSS order 实现，不影响语义。
 */
export default function Standings() {
  const { me } = useAuth();
  const { vtNav } = useVTNav();
  const [board, setBoard] = useState<BoardKey>("accuracy");

  const q = useQuery({
    queryKey: ["standings", board],
    queryFn: () => api.get<LbData>(`/api/v1/leaderboard?board=${board}&limit=50`),
    refetchInterval: 30000,
  });

  const items = q.data?.items ?? [];
  const top3 = items.slice(0, 3);
  const rest = items.slice(3);
  const cur = BOARDS.find((b) => b.k === board)!;
  const mineIdx = me ? items.findIndex((it) => me.display_name && it.display_name === me.display_name) : -1;

  const mainValue = (it: LbItem) => {
    if (board === "accuracy") return `${it.accuracy.toFixed(2)}`;
    if (board === "speed") return fmtMs(it.avg_correct_ms);
    return String(it.best_streak);
  };

  return (
    <section className="standings" aria-labelledby="sd-title">
      <div className="sd-head">
        <h2 id="sd-title" className="sd-title">排行榜</h2>
        <div className="sd-tabs" role="tablist" aria-label="榜单类型">
          {BOARDS.map((b) => (
            <button
              key={b.k}
              role="tab"
              aria-selected={board === b.k}
              className={`btn btn-sm${board === b.k ? " btn-primary" : ""}`}
              onClick={() => setBoard(b.k)}
            >
              {b.label}
            </button>
          ))}
        </div>
      </div>

      {q.isLoading && <p className="note mt-4">载入中…</p>}

      {!q.isLoading && items.length === 0 && (
        <div className="info-box mt-4">
          还没有人上榜。<b>答满 {q.data?.min_questions ?? 20} 题</b>就能进入榜单——
          现在就去练一组。
          <div className="row mt-4">
            <button className="btn btn-primary" onClick={() => vtNav("/practice", "forward")}>
              开始答一题 <IconArrowRight size={15} />
            </button>
          </div>
        </div>
      )}

      {top3.length > 0 && (
        <div className="podium">
          {top3.map((it) => (
            <article key={it.rank} className={`pd-card rank-${it.rank}`}>
              <span className="pd-rank">
                {it.rank === 1 ? "第一名" : it.rank === 2 ? "第二名" : "第三名"}
              </span>
              <h3 className="pd-name">{it.display_name}</h3>
              <p className="pd-score">
                {mainValue(it)}
                <span className="pd-unit">{cur.unit}</span>
              </p>
              <dl className="pd-facts">
                <div><dt>正确率</dt><dd>{it.accuracy.toFixed(1)}%</dd></div>
                <div><dt>答对用时</dt><dd>{fmtMs(it.avg_correct_ms)}{it.avg_correct_ms !== null ? "s" : ""}</dd></div>
                <div><dt>最长连对</dt><dd>{it.best_streak}</dd></div>
                <div><dt>题量</dt><dd>{it.answered_count}</dd></div>
                <div><dt>答对</dt><dd>{it.correct_count}</dd></div>
                <div><dt>速度样本</dt><dd>{it.speed_sample}</dd></div>
              </dl>
            </article>
          ))}
        </div>
      )}

      {rest.length > 0 && (
        <table className="table mt-5">
          <caption className="sr-only">第 4 名及以后</caption>
          <thead>
            <tr>
              <th scope="col">名次</th><th scope="col">昵称</th>
              <th scope="col">正确率</th><th scope="col">答对用时</th>
              <th scope="col">最长连对</th><th scope="col">题量</th><th scope="col">答对</th>
            </tr>
          </thead>
          <tbody>
            {rest.map((it) => (
              <tr key={it.rank}>
                <td className="num">{it.rank}</td>
                <td>{it.display_name}{mineIdx >= 0 && it.rank === mineIdx + 1 && <span className="mx-you">你</span>}</td>
                <td className="num">{it.accuracy.toFixed(2)}%</td>
                <td className="num">{fmtMs(it.avg_correct_ms)}{it.avg_correct_ms !== null ? "s" : ""}</td>
                <td className="num">{it.best_streak}</td>
                <td className="num">{it.answered_count}</td>
                <td className="num">{it.correct_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {q.data?.note && <p className="note mt-4">{q.data.note}</p>}
      <p className="note">
        榜单只显示昵称，不显示真实姓名或账号；未登录与未设置昵称的学习者统一显示为「枫禾学子」。
        {!me && <> <a href="/register">注册账号</a>后可保留自己的练习记录。</>}
      </p>
    </section>
  );
}
