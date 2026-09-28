import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";

/**
 * 自定义下拉选择。
 *
 * 为什么不用原生 `<select>`：
 *   · 原生控件的**弹出层由操作系统绘制**，深色页面里的圆角、行高、悬停态一律不受控，
 *     和这套水墨界面拼在一起像两个软件（现场投屏时尤其明显）；
 *   · 原生 `<select>` 的宽度随最长选项撑开，同一行里几个下拉框宽度参差不齐；
 *   · 触屏（希沃）上原生弹出层的点按目标偏小。
 *
 * 自己实现要把原生控件本来白送的能力补齐，否则是退步。这里逐条做到：
 *   · 键盘：↑↓ 移动、Home/End 首尾、Enter/Space 选中、Esc 关闭、Tab 关闭并交还焦点、
 *     字符键按首字母跳转（"类型预选"）；
 *   · 无障碍：触发器是 `role="combobox"` + `aria-expanded/aria-controls/aria-activedescendant`，
 *     面板是 `role="listbox"`，选项是 `role="option"` + `aria-selected`；
 *   · 空间不足时自动**向上展开**（表格底部的下拉框不会跑出屏幕）；
 *   · 面板用 Portal 挂到 body 并**固定定位**：卡片、滚动容器都不会把它裁掉。
 *
 * 给自动化测试留的稳定钩子：触发器带 `data-slt="<aria-label>"`，选项带 `data-value`。
 * Playwright 无法对自定义 listbox 用 selectOption，必须点开再点选项，见 _verify/adminui.cjs。
 */

export interface SelectOption {
  value: string;
  label: string;
  /** 次要说明，显示在标签下方（例如"全场共 20 题"）。 */
  hint?: string;
  disabled?: boolean;
}

interface Props {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  /** 无障碍名称；同时作为自动化钩子 data-slt。必填 —— 无名称的下拉框没有意义。 */
  ariaLabel: string;
  disabled?: boolean;
  /** sm：表格/工具条里用；md：表单里用（默认）。 */
  size?: "sm" | "md";
  /** 触发器最小宽度（px）。同排多个下拉框给同一个值，视觉上才对齐。 */
  minWidth?: number;
  placeholder?: string;
  /** 给外部 `<label htmlFor>` 锚定用：挂在**触发器按钮**上。
   *  触发器是 button 而非 input，但 label 的 htmlFor 指向它同样能建立可访问名关联，
   *  也保留了"点标签即聚焦控件"的习惯。 */
  id?: string;
  style?: CSSProperties;
}

export default function Select({
  value, onChange, options, ariaLabel, disabled, size = "md", minWidth, placeholder, id, style,
}: Props) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [drop, setDrop] = useState<"down" | "up">("down");
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const listId = useId();

  const current = options.find((o) => o.value === value);
  const selectedIndex = Math.max(0, options.findIndex((o) => o.value === value));

  const place = useCallback(() => {
    const el = btnRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const below = window.innerHeight - r.bottom;
    // 展开方向：下方放不下（约 260px）且上方更宽裕时向上
    const up = below < 260 && r.top > below;
    const h = Math.min(280, Math.max(84, options.length * 40 + 12));
    setDrop(up ? "up" : "down");
    setPos({ left: r.left, top: up ? r.top - h - 6 : r.bottom + 6, width: r.width });
  }, [options.length]);

  // 打开时定位；打开期间跟随滚动/缩放重算（用 capture 才能收到内部滚动容器的滚动）
  useEffect(() => {
    if (!open) return;
    place();
    const onMove = () => place();
    window.addEventListener("scroll", onMove, true);
    window.addEventListener("resize", onMove);
    return () => {
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
    };
  }, [open, place]);

  // 打开时把高亮项滚进视野
  useLayoutEffect(() => {
    if (!open) return;
    const node = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    node?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  // 点击别处关闭
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || listRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const openAt = (idx: number) => {
    const start = idx >= 0 ? idx : selectedIndex;
    setActive(start);
    setOpen(true);
  };

  const commit = (idx: number) => {
    const opt = options[idx];
    if (!opt || opt.disabled) return;
    onChange(opt.value);
    setOpen(false);
    btnRef.current?.focus();
  };

  const move = (delta: number) => {
    if (!options.length) return;
    let i = active;
    for (let n = 0; n < options.length; n++) {
      i = (i + delta + options.length) % options.length;
      if (!options[i].disabled) break;
    }
    setActive(i);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openAt(e.key === "ArrowUp" ? options.length - 1 : selectedIndex);
      }
      return;
    }
    switch (e.key) {
      case "Escape": e.preventDefault(); setOpen(false); btnRef.current?.focus(); break;
      case "Tab": setOpen(false); break;
      case "ArrowDown": e.preventDefault(); move(1); break;
      case "ArrowUp": e.preventDefault(); move(-1); break;
      case "Home": e.preventDefault(); setActive(0); break;
      case "End": e.preventDefault(); setActive(options.length - 1); break;
      case "Enter": case " ": e.preventDefault(); commit(active); break;
      default: {
        // 类型预选：按首字符跳到下一个匹配项
        if (e.key.length === 1 && /\S/.test(e.key)) {
          const k = e.key.toLowerCase();
          const from = active + 1;
          for (let n = 0; n < options.length; n++) {
            const i = (from + n) % options.length;
            if (!options[i].disabled && options[i].label.toLowerCase().startsWith(k)) {
              setActive(i);
              break;
            }
          }
        }
      }
    }
  };

  const label = current?.label ?? placeholder ?? "请选择";

  return (
    <span className={`slt slt-${size}`} style={style}>
      <button
        ref={btnRef}
        id={id}
        type="button"
        className={`slt-btn${open ? " on" : ""}`}
        role="combobox"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open ? `${listId}-${active}` : undefined}
        data-slt={ariaLabel}
        disabled={disabled}
        style={minWidth ? { minWidth } : undefined}
        onClick={() => (open ? setOpen(false) : openAt(selectedIndex))}
        onKeyDown={onKeyDown}
      >
        <span className={`slt-val${current ? "" : " ph"}`}>{label}</span>
        <svg className="slt-caret" viewBox="0 0 12 8" aria-hidden="true" focusable="false">
          <path d="M1 1.5 6 6.5 11 1.5" fill="none" stroke="currentColor" strokeWidth="1.6"
                strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && pos && createPortal(
        <div
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label={ariaLabel}
          className="slt-panel"
          data-drop={drop}
          style={{ left: pos.left, top: pos.top, minWidth: pos.width }}
          onKeyDown={onKeyDown}
        >
          {options.map((o, i) => (
            <div
              key={o.value}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={o.value === value}
              aria-disabled={o.disabled || undefined}
              data-idx={i}
              data-value={o.value}
              className={`slt-opt${i === active ? " act" : ""}${o.value === value ? " sel" : ""}${o.disabled ? " dis" : ""}`}
              onMouseEnter={() => !o.disabled && setActive(i)}
              onMouseDown={(e) => { e.preventDefault(); commit(i); }}
            >
              <span className="slt-tick" aria-hidden="true">{o.value === value ? "◆" : ""}</span>
              <span className="slt-body">
                <span className="slt-lab">{o.label}</span>
                {o.hint && <span className="slt-hint">{o.hint}</span>}
              </span>
            </div>
          ))}
        </div>,
        document.body,
      )}
    </span>
  );
}
