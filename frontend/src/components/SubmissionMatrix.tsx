import { useMemo } from "react";

/**
 * 提交矩阵 —— 参考 GitHub 贡献活跃度图的表达方式，但**语义不同**，这里说清楚：
 *
 * GitHub 的格子回答"某一天我做了多少"；本矩阵回答"**某一回合、某个学院交了多少人**"。
 *   · 列 = 回合（第 1 题、第 2 题……），从左到右就是比赛的推进方向；
 *   · 行 = 学院；
 *   · 格子亮度 = **该院本题已交人数 ÷ 该院签到人数**（满编归一化）。
 *
 * 为什么不直接用"已交人数"：各院人数可能不同，绝对人数会让"人少的院永远看着冷清"。
 * 比例才是可比的；而且**空格本身就是信息**——它精确地显示"哪一院在第几题掉队了"，
 * 这正是活跃度图最有价值的地方。
 *
 * 颜色不是唯一识别手段：每行左侧有学院名与色块，格子按比例分五档
 * （0 / 1–25% / 26–50% / 51–75% / 76–100%），并附带 aria-label，色弱与读屏用户同样可用。
 */

export interface MatrixData {
  rounds: Array<{ index: number; status: string; total_submitted: number }>;
  houses: Array<{
    key: string; name: string; color_light: string; color_deep: string; total: number;
    cells: Array<{ round_index: number; submitted: number; total: number; ratio: number }>;
  }>;
  max_rounds: number;
  planned_questions: number;
}

function level(ratio: number): 0 | 1 | 2 | 3 | 4 {
  if (ratio <= 0) return 0;
  if (ratio <= 0.25) return 1;
  if (ratio <= 0.5) return 2;
  if (ratio <= 0.75) return 3;
  return 4;
}

const LEVEL_LABEL = ["无人提交", "少量提交", "约半数提交", "多数提交", "全员提交"];

export default function SubmissionMatrix({
  matrix, highlight, compact = false,
}: {
  matrix: MatrixData;
  /** 高亮某个学院（观战/答题时是"我们院"） */
  highlight?: string | null;
  compact?: boolean;
}) {
  const planned = Math.max(matrix.planned_questions, matrix.max_rounds, 1);

  // 未开始的回合也要占位（灰格），否则"还没打到"与"没人交"看起来一样
  const cols = useMemo(
    () => Array.from({ length: planned }, (_, i) => matrix.rounds[i] ?? null),
    [matrix.rounds, planned],
  );

  return (
    <section className={`mx${compact ? " mx-compact" : ""}`} aria-label="各学院提交进度矩阵">
      <header className="mx-head">
        <h3 className="mx-title">提交进度</h3>
        <span className="mx-legend" aria-hidden="true">
          少
          {[0, 1, 2, 3, 4].map((l) => <i key={l} className={`mx-sw l${l}`} />)}
          多
        </span>
      </header>

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
                    return (
                      <i key={i} className="mx-cell l0 pending" title={`第 ${i + 1} 题：未开始`} />
                    );
                  }
                  const lv = level(cell.ratio);
                  return (
                    <i
                      key={i}
                      className={`mx-cell l${lv}`}
                      style={lv > 0
                        ? { background: `color-mix(in oklab, ${h.color_deep} ${28 + lv * 18}%, #0d1512)` }
                        : undefined}
                      title={`${h.name} 第 ${i + 1} 题：${cell.submitted}/${cell.total} 人（${LEVEL_LABEL[lv]}）`}
                      aria-label={`${h.name} 第 ${i + 1} 题 ${LEVEL_LABEL[lv]}，${cell.submitted} / ${cell.total}`}
                      role="cell"
                    />
                  );
                })}
              </span>
              <span className="mx-stat num" role="cell">
                {done}<small>/{planned}</small>
              </span>
            </div>
          );
        })}
      </div>

      <p className="mx-note">
        每一格是一道题、一行是一个学院；亮度表示<strong>该院有多少人交了这一题</strong>。
        空格不是"缺数据"，而是"当时没人交"——它精确显示哪一院在第几题掉队。
      </p>
    </section>
  );
}
