import { useEffect, useRef } from "react";
import { GRAIN_MASKS, COMPOSE } from "./svgInk";
import { CARD_GRAIN_URL, SOFT_NOISE_URL } from "./grain";
import { getLevel } from "./motion";

const KEYS = ["tiger", "dragon", "tortoise", "phoenix"] as const;
export type HouseKey = (typeof KEYS)[number];
/** 五个墨层（与 _verify/bake_material_textures.cjs 的输出文件名一一对应） */
const LAYERS = ["deep", "bleed", "glyph", "flow", "fine"] as const;

/**
 * 水墨材质层（House 卡 / 详情主视觉 / 揭晓单元 / 投屏卡共用）。
 *
 * ⚠️ 运行时用的是**烘焙贴图**，不是实时滤镜（2026-09-26 第二轮改动，成因见下）。
 *
 * 原来这里是：`inkLayers.ts` 现场生成 SVG 字符串 → 注入 DOM → 由浏览器**每帧**
 * 跑 feTurbulence（最多 4 八度）+ feDisplacementMap(scale 88–118) + feGaussianBlur。
 * 有头实测（/houses/:key，1440×900）：
 *     静置帧间隔 p50 **23.9ms**（≈42fps 上限）、p95 29.1ms、6 秒掉帧 **192**；GPU 跑满。
 *   逐项归因（out/housedetail-matrix.txt，2×2 因子）：
 *     只去滤镜 23.9→**6.1ms**（−74%）｜只冻动画 23.9→22.6ms（几乎无变化）
 *     ⇒ 成本**全在实时滤镜**上，与动画无关。
 *   为什么贵得隐蔽：滤镜挂在带 scale 动画、`inset:-22%`（hero 再叠 `scale(1.34)`）的层上，
 *   实测最大 3378×2323 = **780 万像素**；缩放改变光栅化比例 ⇒ 浏览器无法复用滤镜结果。
 *   而且实时 feTurbulence 的输出会**随每次重光栅化重新随机** —— 这就是"字一闪一闪"的来源。
 *
 * 为什么不能直接删掉滤镜：同位置逐像素比对（动画冻结）hero 有 10.82% 像素变化、
 * 最大通道差 43，肉眼是"柔和扩散的墨"变成"硬边碎片"，不是同一个东西。
 * 所以改成**算一次、存成图**：`_verify/bake_material_textures.cjs` 通过开发服务器
 * import 真实的 `inkLayers.ts` / `svgInk.ts` 取到与线上一致的 SVG（带滤镜），
 * 渲染成 20 张透明底 PNG（4 院 × 5 层，共 2.49MB）放进 `frontend/public/ink-mat/`。
 * 贴图按 viewBox 原始宽高比（600×380 的 2 倍）烘焙、不裁切，
 * 显示端用 `background-size: cover` 复原 `preserveAspectRatio="xMidYMid slice"` 的裁切行为。
 *
 * 改了 `inkLayers.ts` / `svgInk.ts` / 院色变量之后，**必须重跑** `bake_material_textures.cjs`，
 * 否则页面还是旧贴图（不会报错，只是不再反映改动）。
 *
 * 离屏/后台暂停：IntersectionObserver + html.motion-paused（motion.ts）。
 */
export default function PaperMaterial({
  house,
  variant = "card",
  sheen = true,
  className = "",
}: {
  house: string;
  variant?: "card" | "hero" | "orb" | "stage" | "strip";
  sheen?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const key: HouseKey = (KEYS as readonly string[]).includes(house) ? (house as HouseKey) : "dragon";
  const idx = KEYS.indexOf(key);
  const compose = COMPOSE[key];

  // 离屏暂停装饰运动（不重渲染，仅切类名）
  useEffect(() => {
    const el = ref.current;
    if (!el || getLevel() === "reduced") return;
    const io = new IntersectionObserver(
      (entries) => { for (const en of entries) el.classList.toggle("paused", !en.isIntersecting); },
      { rootMargin: "140px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const lite = getLevel() === "reduced";
  // 贴图路径按 (院, 层) 取名；.edge 用**同一张**贴图再画一次并挖空中心，
  // 只让边缘更实（原先它还额外套一个实时位移滤镜，那正是 13 个滤镜里的 2 个）
  const layer = (name: (typeof LAYERS)[number], cls: string, withEdge = false) => {
    const url = `url(/ink-mat/${key}-${name}.png)`;
    return (
      <div className={`mat-layer ${cls}`}>
        <div className="mat-inner" style={{ transform: compose, backgroundImage: url }} />
        {withEdge && <div className="edge" style={{ backgroundImage: url }} />}
      </div>
    );
  };

  return (
    <div ref={ref} className={`ink-mat ink-mat-${variant} ${className}`} aria-hidden="true">
      {layer("deep", "mat-deep")}
      {layer("bleed", "mat-bleed", true)}
      {layer("glyph", "mat-glyph")}
      {layer("flow", "mat-flow", true)}
      {layer("fine", "mat-fine")}
      <div
        className="mat-grain"
        style={{ backgroundImage: CARD_GRAIN_URL, maskImage: GRAIN_MASKS[idx], WebkitMaskImage: GRAIN_MASKS[idx] } as React.CSSProperties}
      />
      <div
        className="mat-mottle"
        style={{ backgroundImage: SOFT_NOISE_URL, maskImage: GRAIN_MASKS[idx], WebkitMaskImage: GRAIN_MASKS[idx] } as React.CSSProperties}
      />
      <div className="mat-veil" />
      {sheen && !lite && <div className="mat-sheen" />}
    </div>
  );
}
