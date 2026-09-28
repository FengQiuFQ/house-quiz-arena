import { useEffect, useMemo, useRef } from "react";

/**
 * 练习页的「主题多选」控件。
 *
 * 为什么不把 Select 改成多选：`components/Select.tsx` 是自绘的**单选** listbox
 * （触发器 + 弹出面板 + aria-activedescendant），键盘模型是"↑↓ 移动高亮、回车选定"。
 * 多选要用同一套外观，就得把移动/选定/字符预选/点击其它处关闭全部重写，
 * 重写后与单选版只剩样式相同，反而更难维护 —— 所以另起一个控件。
 *
 * 为什么平铺成一行行勾选框，而不是"点开弹层再勾"：
 *   · 主题只有 11 个，两列网格一屏放得下；
 *   · 希沃触屏上没有 hover，弹层要先点开、还可能被容器裁掉或跑出屏幕，
 *     平铺没有这些失败模式，也天然支持 Tab 遍历与读屏朗读。
 *
 * 键盘可达性靠**真正的** `<input type="checkbox">`：Tab 逐个聚焦、Space 切换
 * 都是浏览器白送的能力。容器（label）**不加任何 transform 缩放动画**：
 * 仓库踩过这个坑 —— 按压动画 `transform: scale(0.97)` 会把 18px 的勾选框
 * 挪出指针命中范围，表现出来就是"勾选框点不动"（见 SubjectQuota.tsx 的注释与
 * motion.css 里 PRESSABLE 那份名单，本控件刻意不在名单内）。
 * 命中区域靠 min-height 撑到 44px，不靠变形。
 *
 * 给自动化测试留的稳定钩子：每个主题的勾选框带 `data-subject="<主题名>"`；
 * 「全部主题（混合）」那一行是另一个控件，带 `data-subject-all`，
 * 这样按 data-subject 取主题时不会把它一起选中。
 */

export interface SubjectOption {
  subject: string;
  count: number;
}

interface Props {
  items: SubjectOption[];
  /** 练习池可用题量（含待审核题） */
  total: number;
  /** 其中已审核的题量 */
  published: number;
  /** 已选主题。空数组表示"全部主题（混合）"，与后端"不传 subjects"等价。 */
  value: string[];
  onChange: (next: string[]) => void;
  loading?: boolean;
  /** 主题列表没读出来（网络/服务端错）。要说成"没读出来"，不能说成"题库是空的" —— 
   *  后者会让人以为题目被删了。 */
  error?: boolean;
}

/**
 * 已选主题的可用题量合计；一个主题都没选时＝全部主题的题量。
 *
 * 与后端抽题口径对齐：不传 subjects 就是全池随机，所以"没选"不等于"没题"。
 * 抽出来给练习页算"题够不够"用，避免同一笔账在页面里再算一遍（两处算必然走样）。
 */
export function availableCount(items: SubjectOption[], total: number, value: string[]): number {
  if (!value.length) return total;
  const byName = new Map(items.map((s) => [s.subject, s.count]));
  return value.reduce((sum, name) => sum + (byName.get(name) ?? 0), 0);
}

export default function SubjectPicker({
  items, total, published, value, onChange, loading, error,
}: Props) {
  const allRef = useRef<HTMLInputElement>(null);
  const names = useMemo(() => items.map((s) => s.subject), [items]);
  const picked = useMemo(() => new Set(value), [value]);

  const allPicked = names.length > 0 && names.every((n) => picked.has(n));
  const somePicked = names.some((n) => picked.has(n));
  // 半选（勾了一部分）：native checkbox 的 indeterminate 只能由 DOM 属性设置，
  // 它没有对应的 HTML attribute，React 也管不到 —— 必须自己写进去。
  useEffect(() => {
    if (allRef.current) allRef.current.indeterminate = somePicked && !allPicked;
  }, [somePicked, allPicked]);

  /* 勾选状态一律按选项列表的顺序重排。
     原因有两个：一是清空某项时用 filter 天然不会留下空位；二是"已选 n 个主题"
     的摘要与上面看到的行顺序一致，读起来对得上（按勾选先后排会出现摘要与视觉错位）。 */
  const toggle = (name: string, on: boolean) => {
    onChange(names.filter((n) => (on ? n === name || picked.has(n) : n !== name && picked.has(n))));
  };

  const pickedItems = items.filter((s) => picked.has(s.subject));
  const avail = availableCount(items, total, value);

  return (
    <section className="sp" aria-label="练习主题">
      <div className="sp-head">
        <span className="sp-title">主题</span>
        <span className="sp-stat">
          {error ? "题量载入失败"
            : loading ? "题量载入中…"
              : <>题库共 <b className="num">{total}</b> 题（其中 <b className="num">{published}</b> 题已审核）</>}
        </span>
      </div>

      {/* 一键全选/全不选。它自己也用真勾选框：半选态（勾了一部分）能一眼看出来，
          比一个纯按钮"点了变成什么"更可预期。 */}
      <label className="sp-all" data-subject-all>
        <input
          ref={allRef}
          type="checkbox"
          checked={allPicked}
          disabled={loading || error || names.length === 0}
          onChange={(e) => onChange(e.target.checked ? names : [])}
        />
        <span className="sp-name">全部主题（混合）</span>
        <span className="sp-note">
          {allPicked ? "已全选（点一下全部取消）" : somePicked ? "点一下勾上其余主题" : "点一下全选"}
        </span>
      </label>

      {/* 载入中/读失败时**不显示**这一行：它算的是"共 X 题"，而这时的 0 是"还没读到"，
          不是"没有题" —— 显示出来就是一句假话。 */}
      {!loading && !error && (
        <p className="sp-sum" role="status">
          {value.length
            ? <>已选 <b className="num">{pickedItems.length}</b> 个主题 · 共 <b className="num">{avail}</b> 题</>
            : <>未选主题 → 按全部主题（混合）出题 · 共 <b className="num">{avail}</b> 题</>}
        </p>
      )}

      <div className="sp-grid" role="group" aria-label="可选主题（可多选）">
        {error && <p className="sp-note">主题列表没读出来，刷新页面再试。</p>}
        {!loading && !error && items.length === 0 && <p className="sp-note">练习池暂无可练题目。</p>}
        {items.map((s) => {
          const on = picked.has(s.subject);
          return (
            <label key={s.subject} className={`sp-item${on ? " on" : ""}`}>
              <input
                type="checkbox"
                data-subject={s.subject}
                checked={on}
                disabled={loading}
                onChange={(e) => toggle(s.subject, e.target.checked)}
              />
              <span className="sp-name">{s.subject}</span>
              <span className="sp-avail num">{s.count} 题</span>
              {on && <span className="sp-on">已选</span>}
            </label>
          );
        })}
      </div>
    </section>
  );
}
