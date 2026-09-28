import { GRAIN_TILE } from "./grain";

/**
 * 水墨材质 SVG（字符串形式，由 PaperMaterial 注入）。
 *
 * 为什么用字符串：这些图层包含几十条确定性生成的路径，用 React 元素逐条写会非常臃肿；
 * 这里的内容 100% 由本仓库常量生成（svgInk.ts + grain.ts），不含任何外部输入，
 * 因此通过 dangerouslySetInnerHTML 注入是安全的、也是体积最小的做法。
 *
 * 特别注意（本轮修掉的一个真实缺陷）：把这些字符串当作 React 子节点直接使用，
 * React 会按「文本节点」渲染 —— SVG 一个都不会出现在 DOM 里，材质层等于没画。
 * 必须交给 HTML 解析器（dangerouslySetInnerHTML / innerHTML）。
 *
 * 颜色与形状分离：形状在字符串里，颜色走 CSS 变量（--ink-a/b/c/accent），
 * 同一份 SVG 可被四个 House 复用。颗粒在 SVG 内部用 <pattern> 定义（自洽，不跨 SVG 引用）。
 */

const VB_W = 600;
const VB_H = 380;
const PAD = 46; // 覆盖余量：容器 inset -22%、缩放/位移后仍不露边
const FULL = `x="${-PAD}" y="${-PAD}" width="${VB_W + PAD * 2}" height="${VB_H + PAD * 2}"`;

export interface ShapeSet { blobs: string[]; strokes: string; specks: string; fine: string }

const head = (id: string, extraDefs = "") =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${VB_W} ${VB_H}" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">` +
  `<defs>${extraDefs}${grainPattern(id)}${lightBand(id)}</defs>`;

const tail = "</svg>";

/**
 * 「亮墨带」遮罩 —— 材质预算的核心手段：
 * 浅色墨（screen 层）只集中在卡片上部（该区只放 22–28px 院名，WCAG 大字号门槛 3:1），
 * 卡片下部承载 12–13px 正文，由 mask 与 veil 双重收口，实测最坏情况下仍 ≥4.5:1。
 * 这样做比「整体压低材质」更好：上部保留明显的水墨层次，下部文字始终清楚稳定。
 */
const lightBand = (id: string) =>
  `<linearGradient id="${id}-band" x1="0" y1="0" x2="0" y2="1">` +
  `<stop offset="0%" stop-color="#fff" stop-opacity="1"/>` +
  `<stop offset="40%" stop-color="#fff" stop-opacity="0.95"/>` +
  `<stop offset="58%" stop-color="#fff" stop-opacity="0.42"/>` +
  `<stop offset="76%" stop-color="#fff" stop-opacity="0.06"/>` +
  `<stop offset="88%" stop-color="#fff" stop-opacity="0"/>` +
  `</linearGradient>` +
  `<mask id="${id}-lightmask" maskUnits="userSpaceOnUse" x="${-PAD}" y="${-PAD}" width="${VB_W + PAD * 2}" height="${VB_H + PAD * 2}">` +
  `<rect ${FULL} fill="url(#${id}-band)"/></mask>`;

/** 细颗粒 pattern（飞白感）；用 soft-light 叠在色块上，让墨色有颗粒与浓淡 */
const grainPattern = (id: string) =>
  `<pattern id="${id}-grain" patternUnits="userSpaceOnUse" width="150" height="150">` +
  `<image href="${grainHref()}" x="0" y="0" width="150" height="150" preserveAspectRatio="none"/></pattern>`;

let _href = "";
function grainHref() {
  if (!_href) {
    const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='150' height='150'><filter id='c'><feTurbulence type='fractalNoise' baseFrequency='0.95' numOctaves='2' stitchTiles='stitch' seed='29'/><feColorMatrix type='saturate' values='0'/><feComponentTransfer><feFuncA type='linear' slope='0.52' intercept='-0.12'/></feComponentTransfer></filter><rect width='150' height='150' filter='url(%23c)'/></svg>`;
    _href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }
  return _href;
}

/**
 * 是否给 <g> 挂**实时** SVG 滤镜（feTurbulence + feDisplacementMap + feGaussianBlur）。
 *
 * ⚠️ 这是全站最贵的一处渲染开销，而且贵得很隐蔽：
 *   这些滤镜挂在**带 scale 动画**的层里，缩放会改变光栅化比例，
 *   浏览器无法复用上一次的滤镜结果，只能**每帧重跑一遍噪声**。
 * 有头实测（投屏页 1920×1080，鼠标移动/滚动时的最差帧间隔 p95）：
 *   基线 699.5ms → 只去掉这些滤镜 7.2ms（↓99%）；只去掉 scale 46.7ms；八度减半 40.7ms。
 *   也就是说"平滑掉 scale"或"降八度"都只是缓解，**只有不再实时算滤镜**才能回到 60fps。
 *
 * heavy=false 时只保留渐变、遮罩与路径本身：墨色与构图都在，丢掉的是
 * "边缘毛糙/扩散"这层质感。大面积表面（投影大屏、观赛大屏）用 false，
 * 小卡（card/orb）面积小、实测无感，保持 true。
 */
const fx = (heavy: boolean, id: string) => (heavy ? ` filter="url(#${id})"` : "");

const grainRect = (id: string, opacity: string) =>
  `<rect ${FULL} fill="url(#${id}-grain)" opacity="${opacity}"/>`;

/** ① 扩散层：大块墨色 + feTurbulence/feDisplacementMap 位移 → 真实扩散边缘与浓淡 */
export function bleedSvg(shapes: ShapeSet, id: string, heavy = true): string {
  const defs =
    `<filter id="${id}-disp" x="-25%" y="-25%" width="150%" height="150%" color-interpolation-filters="sRGB">` +
    `<feTurbulence type="fractalNoise" baseFrequency="0.011 0.016" numOctaves="4" seed="7" result="n"/>` +
    `<feDisplacementMap in="SourceGraphic" in2="n" scale="88" xChannelSelector="R" yChannelSelector="G"/>` +
    `<feGaussianBlur stdDeviation="6"/></filter>` +
    `<filter id="${id}-soft" x="-30%" y="-30%" width="160%" height="160%" color-interpolation-filters="sRGB">` +
    `<feGaussianBlur stdDeviation="24"/></filter>`;
  /** 墨色浓淡：形状本身用径向渐变而非纯色，避免「一块死色」 */
  const grads =
    `<radialGradient id="${id}-ga" cx="34%" cy="36%" r="76%">` +
    `<stop offset="0%" stop-color="var(--ink-a)" stop-opacity="0.95"/><stop offset="70%" stop-color="var(--ink-a)" stop-opacity="0.5"/><stop offset="100%" stop-color="var(--ink-a)" stop-opacity="0"/></radialGradient>` +
    `<radialGradient id="${id}-gb" cx="72%" cy="66%" r="72%">` +
    `<stop offset="0%" stop-color="var(--ink-b)" stop-opacity="0.85"/><stop offset="72%" stop-color="var(--ink-b)" stop-opacity="0.4"/><stop offset="100%" stop-color="var(--ink-b)" stop-opacity="0"/></radialGradient>`;
  const blobOps = [0.96, 0.8, 0.86];
  return (
    head(id, defs + grads) +
    `<g mask="url(#${id}-lightmask)">` +
    `<g${fx(heavy, `${id}-soft`)}>` +
    shapes.blobs.map((d, i) => `<path d="${d}" fill="url(#${id}-g${i % 2 === 0 ? "a" : "b"})" opacity="${blobOps[i] ?? 0.8}"/>`).join("") +
    `</g>` +
    `<g${fx(heavy, `${id}-disp`)}>` +
    `<path d="${shapes.blobs[0]}" fill="url(#${id}-ga)" opacity="0.9"/>` +
    `<path d="${shapes.blobs[1] ?? shapes.blobs[0]}" fill="url(#${id}-gb)" opacity="0.7"/>` +
    `</g>` +
    `</g>` +
    grainRect(id, "0.34") +
    tail
  );
}

/** ② 流动层：笔势与脊线（龙脊／虎纹／龟甲／凤羽各不相同）→ 局部清晰结构 */
export function flowSvg(shapes: ShapeSet, id: string, heavy = true): string {
  const defs =
    `<filter id="${id}-edge" x="-20%" y="-30%" width="140%" height="160%" color-interpolation-filters="sRGB">` +
    `<feTurbulence type="fractalNoise" baseFrequency="0.02 0.03" numOctaves="3" seed="23" result="n"/>` +
    `<feDisplacementMap in="SourceGraphic" in2="n" scale="36" xChannelSelector="R" yChannelSelector="G"/>` +
    `</filter>`;
  return (
    head(id, defs) +
    `<g mask="url(#${id}-lightmask)">` +
    `<g${fx(heavy, `${id}-edge`)} fill="none" stroke="var(--ink-accent)" stroke-width="2.6" stroke-linecap="round" opacity="0.52"><path d="${shapes.strokes}"/></g>` +
    `<g${fx(heavy, `${id}-edge`)} fill="none" stroke="var(--ink-b)" stroke-width="1.3" stroke-linecap="round" opacity="0.42"><path d="${shapes.fine}"/></g>` +
    `<g fill="none" stroke="var(--ink-accent)" stroke-width="3.1" stroke-linecap="round" opacity="0.34"><path d="${shapes.specks}"/></g>` +
    `</g>` +
    grainRect(id, "0.22") +
    tail
  );
}

/** ③ 沉墨层：只做「压深」，不铺整幅底色 —— 卡面底色由卡片自身的 --bg-panel 提供，
 *  这样四个 House 的墨色差异才不会被一块统一底色抹平。 */
export function deepSvg(shapes: ShapeSet, id: string, heavy = true): string {
  const defs =
    `<filter id="${id}-deep" x="-25%" y="-25%" width="150%" height="150%" color-interpolation-filters="sRGB">` +
    `<feTurbulence type="fractalNoise" baseFrequency="0.008 0.012" numOctaves="4" seed="41" result="n"/>` +
    `<feDisplacementMap in="SourceGraphic" in2="n" scale="118" xChannelSelector="R" yChannelSelector="G"/>` +
    `<feGaussianBlur stdDeviation="11"/></filter>` +
    `<radialGradient id="${id}-dc" cx="62%" cy="72%" r="78%">` +
    `<stop offset="0%" stop-color="var(--ink-c)" stop-opacity="1"/><stop offset="70%" stop-color="var(--ink-c)" stop-opacity="0.55"/><stop offset="100%" stop-color="var(--ink-c)" stop-opacity="0"/></radialGradient>`;
  return (
    head(id, defs) +
    `<g${fx(heavy, `${id}-deep`)}>` +
    `<path d="${shapes.blobs[2] ?? shapes.blobs[0]}" fill="url(#${id}-dc)" opacity="0.64"/>` +
    `<path d="${shapes.blobs[0]}" fill="#000" opacity="0.3"/>` +
    `</g>` +
    grainRect(id, "0.3") +
    tail
  );
}

/** ④ 细结构层：飞白颗粒与断续线（soft-light）→ 静止截图也有可读细节 */
export function fineSvg(shapes: ShapeSet, id: string): string {
  return (
    head(id) +
    `<path d="${shapes.specks}" fill="none" stroke="var(--ink-accent)" stroke-width="2.4" stroke-linecap="round" opacity="0.5"/>` +
    `<path d="${shapes.strokes}" fill="none" stroke="var(--ink-b)" stroke-width="1" stroke-dasharray="3 7" opacity="0.4"/>` +
    grainRect(id, "0.42") +
    tail
  );
}

/** ⑤ 角部笔触层：每院构图不同（虎纹右上 / 云气左上 / 叠甲右下 / 凤羽左下），
 *  让静止截图里四张卡一眼可辨；透明度受控，不进入文字区。 */
export function glyphSvg(glyph: string, id: string, heavy = true): string {
  const defs =
    `<filter id="${id}-g" x="-25%" y="-25%" width="150%" height="150%" color-interpolation-filters="sRGB">` +
    `<feTurbulence type="fractalNoise" baseFrequency="0.024 0.03" numOctaves="3" seed="61" result="n"/>` +
    `<feDisplacementMap in="SourceGraphic" in2="n" scale="26" xChannelSelector="R" yChannelSelector="G"/>` +
    `</filter>`;
  return (
    head(id, defs) +
    `<g mask="url(#${id}-lightmask)">` +
    `<g${fx(heavy, `${id}-g`)} fill="none" stroke="var(--ink-accent)" stroke-width="7" stroke-linecap="round" opacity="0.34">` +
    `<path d="${glyph}"/></g>` +
    `<g${fx(heavy, `${id}-g`)} fill="none" stroke="var(--ink-b)" stroke-width="2.4" stroke-linecap="round" opacity="0.5">` +
    `<path d="${glyph}"/></g>` +
    `</g>` +
    tail
  );
}

export const INK_VB = { w: VB_W, h: VB_H, pad: PAD, tile: GRAIN_TILE };
export type { ShapeSet as InkShapeSet };
