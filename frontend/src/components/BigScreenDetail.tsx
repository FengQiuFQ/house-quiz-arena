import type { BoardTimelineItem, ResultsData } from "../api/types";
import PaperMaterial from "../design/PaperMaterial";
import { HOUSE_ICON } from "../design/icons";

/**
 * 大屏的"细节层"：提交矩阵 + 逐题节奏 + 最近提交。
 *
 * 为什么抽成组件：投屏页与观赛页都要这几块，各写一遍必然走样
 * （本项目已经踩过：两页各写一份，改了一处另一处就不一致）。
 *
 * 为什么是**横排胶囊**而不是竖排列表：
 * 大屏必须**一屏放得下**（现场投影不能被滚动条破坏），竖排 20 行会直接把页面撑到
 * 1700px 以上 —— 实测观赛页因此溢出 1080p 达 661px。横排胶囊把整场 20 题压成
 * 两行就能读完，既保住了信息密度，又守住了高度预算。
 *
 * 视觉口径：题号用大号数字（远看靠它定位），进度用短条，状态用文字而不是只靠颜色。
 * 未关闭的回合不显示正确份数（服务端本来就不下发），这里如实写"待本题结束后公布"。
 */

function statusWord(it: BoardTimelineItem): string {
  if (it.closed) return "已结束";
  if (it.status === "open") return "作答中";
  if (it.status === "displaying") return "读题中";
  return "未开始";
}

/** 提交矩阵的数据形状（与后端 submission_matrix 一致，见后端 snapshot.py）。 */
export interface BoardMatrix {
  rounds: Array<{ index: number; status: string; total_submitted: number }>;
  houses: Array<{
    key: string; name: string; color_light: string; color_deep: string; total: number;
    cells: Array<{ round_index: number; submitted: number; total: number; ratio: number }>;
  }>;
  max_rounds: number;
  planned_questions: number;
}

const LEVEL_LABEL = ["无人提交", "少量提交", "约半数提交", "多数提交", "全员提交"];

function level(ratio: number): 0 | 1 | 2 | 3 | 4 {
  if (ratio <= 0) return 0;
  if (ratio <= 0.25) return 1;
  if (ratio <= 0.5) return 2;
  if (ratio <= 0.75) return 3;
  return 4;
}

/**
 * 提交矩阵：**回合 × 学院**的已交比例。
 *
 * 为什么大屏要它（这是"信息密度太低"最直接的一味药）：
 * 逐题节奏只说明"这题交了几份"，最近提交只说明"刚刚谁交了"，
 * 两者都看不出**哪一院在哪一题掉队了**。矩阵把整场压成一张格子图：
 * 横着读是一个学院的全程，竖着读是一道题的全场 —— 这是唯一能一眼看出趋势的表达。
 *
 * 为什么格子按**比例**而不是人数上色：各院人数不同，绝对人数会让"人少的院永远看着冷清"。
 * 空格不是缺数据，而是"当时没人交"，空格本身就是信息。
 *
 * 无障碍：颜色不是唯一手段 —— 每格带 title 与 aria-label，行首有学院名与色点，
 * 且亮暗分五档而不是连续渐变（色弱与读屏用户同样可用）。
 */
export function BoardMatrixPanel({ matrix, highlight }: {
  matrix: BoardMatrix;
  highlight?: string | null;
}) {
  const planned = Math.max(matrix.planned_questions, matrix.max_rounds, 1);
  const cols = Array.from({ length: planned }, (_, i) => matrix.rounds[i] ?? null);
  return (
    <section className="bd-panel bd-matrix" aria-label="各学院提交进度矩阵">
      <h2 className="bd-title">
        提交进度
        <span className="bd-sub">一行一院、一格一题；越亮表示该院交这一题的人越多</span>
      </h2>
      <div className="mx-body" role="table" aria-label="回合 × 学院 的提交进度">
        {matrix.houses.map((h) => {
          const done = h.cells.filter((c) => c.ratio >= 0.999).length;
          const mine = highlight === h.key;
          return (
            <div className={`mx-row${mine ? " mine" : ""}`} key={h.key} role="row">
              <span className="mx-name" role="rowheader">
                <i className="house-dot" style={{ background: h.color_light }} aria-hidden="true" />
                {h.name}
                {mine && <em className="mx-you">本院</em>}
              </span>
              <span className="mx-cells">
                {cols.map((c, i) => {
                  const cell = h.cells[i];
                  if (!c || !cell) {
                    return <i key={i} className="mx-cell l0 pending"
                              title={`第 ${i + 1} 题：未开始`} />;
                  }
                  const lv = level(cell.ratio);
                  return (
                    <i key={i} className={`mx-cell l${lv}`}
                       style={lv > 0
                         ? { background: `color-mix(in oklab, ${h.color_deep} ${28 + lv * 18}%, #0d1512)` }
                         : undefined}
                       title={`${h.name} 第 ${i + 1} 题：${cell.submitted}/${cell.total} 人（${LEVEL_LABEL[lv]}）`}
                       aria-label={`${h.name} 第 ${i + 1} 题 ${LEVEL_LABEL[lv]}，${cell.submitted} / ${cell.total}`}
                       role="cell" />
                  );
                })}
              </span>
              <span className="mx-stat num" role="cell">{done}<small>/{planned}</small></span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/**
 * 揭晓后的名次条：大屏在结果公布后**必须自己说结果**。
 *
 * 为什么需要它：`/board` 与 `/spectate/live` 两条链路**都已经在返回 `result`**，
 * 但两个大屏页谁都没渲染它 —— 揭晓之后屏幕上仍然只有"逐题节奏 + 最近提交"，
 * 观众站在投影前看不到谁赢了，得自己掏手机去翻结果页。
 * 现场投影最大的价值恰恰是"揭晓那一刻全场一起看到名次"，这个洞必须补。
 *
 * 为什么排成**一行四列**而不是竖排名次表：大屏必须一屏放下，
 * 竖排 4 行会多占约 220px，实测把观赛页顶破 1080p（溢出 221px）。
 * 四院固定就是四个，横着排既省高度，又正好让四院并排可比。
 *
 * 只显示**聚合**名次（学院 / 得分 / 名次 / 正确题数 / 接力用时），不含个人成绩：
 * 大屏是公开场合，个人分数不该出现在墙上。这与"匿名大屏不给题面"是同一条边界。
 *
 * 用 ol 而不是 table：这是**有序**结果，读屏会念"第 1 项"，
 * 比表格的"行 1 列 2"更贴合语义。
 */
export function RevealedStandings({ result }: { result: ResultsData }) {
  const houses = [...result.houses].sort((a, b) => a.rank - b.rank);
  return (
    <section className="bd-panel bd-standing" aria-label="最终名次">
      <h2 className="bd-title">
        最终名次
        <span className="bd-sub num">
          第 {result.result_version} 版 · {result.published_at?.slice(11, 16)} 公布
        </span>
      </h2>
      <ol className="rk-strip">
        {houses.map((h) => (
          <li key={h.key} className={`rk-card has-ink house-${h.key} rank-${Math.min(h.rank, 4)}`}>
            <PaperMaterial house={h.key} variant="strip" sheen={false} />
            <span className="rk-card-rank"><b className="num">{String(h.rank).padStart(2, "0")}</b><span>{h.is_tie ? "并列名次" : h.rank === 1 ? "本场冠军" : h.rank === 2 ? "本场亚军" : h.rank === 3 ? "本场季军" : "本场名次"}</span></span>
            <span className="rk-name">
              <span className="rk-crest" aria-hidden="true">{HOUSE_ICON[h.key]?.({ size: 21 })}</span>
              <span>{h.name}</span>
              {h.is_tie && <em className="rk-tie">并列</em>}
            </span>
            <span className="rk-score num">{h.score.toFixed(1)}<small>House 分</small></span>
            <span className="rk-meta num">
              {h.correct_total} 题正确
              {h.timing ? ` · 用时 ${Math.round((h.timing.total_ms ?? 0) / 1000)}s` : ""}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function QuestionPace({ timeline, total }: {
  timeline: BoardTimelineItem[];
  total: number;
}) {  if (timeline.length === 0) {
    return (
      <section className="bd-panel" aria-label="逐题节奏">
        <h2 className="bd-title">逐题节奏</h2>
        <p className="bd-empty">比赛还没开始作答。开赛后这里按题显示每题的提交份数与用时。</p>
      </section>
    );
  }
  const done = timeline.filter((x) => x.closed).length;
  return (
    <section className="bd-panel" aria-label="逐题节奏">
      <h2 className="bd-title">
        逐题节奏
        <span className="bd-sub num">已结束 {done}{total ? ` / ${total}` : ""} 题</span>
      </h2>
      <ol className="pace">
        {timeline.map((it) => {
          const secs = it.duration_ms != null ? Math.round(it.duration_ms / 1000) : null;
          const label = `第 ${it.position ?? "?"} 题，${statusWord(it)}，`
            + `${it.submitted} 份提交`
            + (it.correct != null ? `，${it.correct} 份正确` : "，结果待本题结束后公布")
            + (secs != null ? `，用时 ${secs} 秒` : "");
          return (
            <li key={it.round_instance_id}
                className={`pace-chip ${it.closed ? "closed" : it.status}`}
                title={label}>
              <span className="pace-no num">{it.position ?? "·"}</span>
              <span className="pace-nums">
                <b className="num">{it.submitted}</b>
                {it.correct != null && <em className="num">对 {it.correct}</em>}
                {secs != null && <em className="num">{secs}s</em>}
              </span>
              <span className="sr-only">{label}</span>
            </li>
          );
        })}
        {/* 还没打到的题用占位格补齐：让"全场共几题、还剩几题"一眼可见 */}
        {total > timeline.length && Array.from({ length: Math.min(total - timeline.length, 24) }, (_, i) => (
          <li key={`todo-${i}`} className="pace-chip todo" aria-hidden="true">
            <span className="pace-no num">{timeline.length + i + 1}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function RecentSubmissions({ recent }: {
  recent: Array<{ at: string | null; house_key: string; house_name: string; position: number | null }>;
}) {
  return (
    <section className="bd-panel" aria-label="最近提交">
      <h2 className="bd-title">
        最近提交
        <span className="bd-sub">只显示学院、题号与时间，不含选项与对错</span>
      </h2>
      {recent.length === 0 ? (
        <p className="bd-empty">还没有人提交。第一笔提交出现后这里会实时滚动。</p>
      ) : (
        <ul className="rc-inline">
          {recent.map((x, i) => (
            <li key={`${x.at}-${i}`} className={`rc-chip house-${x.house_key}`}>
              <span className="rc-dot" aria-hidden="true" />
              <span className="rc-house">{x.house_name}</span>
              <span className="rc-what num">{x.position ? `第 ${x.position} 题` : "提交"}</span>
              <time className="rc-time num">{x.at?.slice(11, 19) ?? ""}</time>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}




