/**
 * 统一 SVG 图标体系（项目自带，不引图标库、不使用 Emoji）。
 * 统一规格：24 视觉盒、`currentColor` 描边、strokeWidth 2、round 端点与连接
 * → 线宽与视觉重量一致；尺寸由 size 属性控制。
 */
type P = { size?: number; className?: string };

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none" as const,
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  focusable: "false" as const,
});

export const IconSettings = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 3v2M12 19v2M4.2 7.5l1.7 1M18.1 15.5l1.7 1M4.2 16.5l1.7-1M18.1 8.5l1.7-1" />
  </svg>
);

export const IconMenu = ({ size = 20, className }: P) => (
  <svg {...base(size)} className={className}><path d="M4 7h16M4 12h16M4 17h16" /></svg>
);

export const IconClose = ({ size = 20, className }: P) => (
  <svg {...base(size)} className={className}><path d="M6 6l12 12M18 6L6 18" /></svg>
);

export const IconArrowRight = ({ size = 16, className }: P) => (
  <svg {...base(size)} className={className}><path d="M5 12h13M13 6l6 6-6 6" /></svg>
);

export const IconArrowLeft = ({ size = 16, className }: P) => (
  <svg {...base(size)} className={className}><path d="M19 12H6M11 18l-6-6 6-6" /></svg>
);

export const IconChevronDown = ({ size = 16, className }: P) => (
  <svg {...base(size)} className={className}><path d="M6 9l6 6 6-6" /></svg>
);

/** House 徽记：四个院各一枚，几何抽象（不做动物插画，也不冒充校徽）。 */
export const IconTiger = ({ size = 20, className }: P) => (
  <svg {...base(size)} className={className}><path d="M4 6l3.5 4M20 6l-3.5 4M6 14h12M8 18h8" /></svg>
);
export const IconDragon = ({ size = 20, className }: P) => (
  <svg {...base(size)} className={className}><path d="M3 16c3.5 0 4-8 8-8s4.5 5 10 5" /><path d="M17 8.5l2.5 1.5-2 2" /></svg>
);
export const IconTortoise = ({ size = 20, className }: P) => (
  <svg {...base(size)} className={className}><path d="M12 5l6 3.5v7L12 19l-6-3.5v-7z" /><path d="M12 9.5L15 11v3l-3 1.5L9 14v-3z" /></svg>
);
/** 第四院「朱雀 / Vermilion Bird」的图标。
 *  名字跟着**院名**走，不跟 house key 走：key 仍是 `phoenix`（技术标识，历史数据在用），
 *  但院名已定为朱雀 —— 组件继续叫 IconPhoenix 会让后来人以为这一院还叫凤凰。
 *
 *  ⚠️ 2026-09-26 改过一次图形。原来的是一条**闭合的新月形翅膀**
 *  （`M12 20c0-4 2-6 5-7-2.4-.6-4-2-4.6-4.4C11.6 6.4 12 4 14 3c-4 .6-6 3-6 6.6C8 13 9.6 15 12 20z`），
 *  当时的注释写的是"本来就是一只抽象飞鸟，朱雀可以直接沿用"——
 *  **渲染出来看并不是**：20px 下它是一个实心块，用户在大屏与结果页上直接反馈
 *  「朱雀应该是bird」。四院里 tiger（虎脸）与 tortoise（龟甲）都是一眼可辨的，
 *  朱雀作为唯一的鸟不该只是个色块。
 *  现图形：展翅 + 圆头 + 喙 + 分叉长尾羽 —— 在 96px 与 20px 下都读得出是鸟。
 *  候选对比图（6 版并排，含被否掉的方案）见 `_verify/out/bird-icons.png`，
 *  生成脚本 `_verify/probe_bird_icons.cjs`；规格仍与全站一致（24 盒、描边 2、round）。 */
export const IconVermilionBird = ({ size = 20, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M11.4 12.6C9.2 10.2 6.2 8.9 2.8 9.2c1.5 2.3 3.2 3.8 5.1 4.6" />
    <path d="M12.6 12.6c2.2-2.4 5.2-3.7 8.6-3.4-1.5 2.3-3.2 3.8-5.1 4.6" />
    <path d="M12 10.2c1.2 0 2.2 1 2.2 2.2 0 1.8-.8 3.6-1.5 5.2" />
    <path d="M12.7 17.6c-.6 1.4-1.5 2.4-2.7 3.1M12.7 17.6c.5 1.5.5 2.9 0 4.1" />
    <path d="M12 6.1a1.4 1.4 0 1 1 0 2.8 1.4 1.4 0 0 1 0-2.8z" />
    <path d="M10.7 6.8L8.8 5.6" />
  </svg>
);

export const HOUSE_ICON: Record<string, (p: P) => JSX.Element> = {
  tiger: IconTiger,
  dragon: IconDragon,
  tortoise: IconTortoise,
  phoenix: IconVermilionBird,
};

/** 赛制特色用图标（首页「比赛怎么打」四张卡）。
 *  与全站同一规格：24 盒、currentColor、strokeWidth 2、round 端点。 */
export const IconShield = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z" />
    <path d="M9 12l2 2 4-4" />
  </svg>
);

export const IconClock = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <circle cx="12" cy="12" r="8" />
    <path d="M12 7.5V12l3 2" />
  </svg>
);

export const IconPeople = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <circle cx="9" cy="9" r="3" />
    <path d="M4 19c0-2.8 2.2-5 5-5s5 2.2 5 5" />
    <path d="M16 8.2A3 3 0 0 1 16 14M17 19c0-2 .6-3.6-1-5" />
  </svg>
);

export const IconStar = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M12 4l2.3 4.9 5.2.7-3.8 3.6.9 5.2L12 15.9 7.4 18.4l.9-5.2L4.5 9.6l5.2-.7z" />
  </svg>
);
