/**
 * 噪点底纹（data-URI SVG feTurbulence）。
 * 说明：
 *  - `--grain-url` 用于全局背景的纸张颗粒，平铺 190×190，`grain-scroll` 按瓦片尺寸位移 → 无缝循环。
 *  - `--card-grain-url` 用于 House 卡/主视觉的细颗粒覆盖（静态，不参与动画）。
 *  - `--soft-noise-url` 用于“浓淡/扩散边缘”的软噪声，作为遮罩给形状叠加墨色不均。
 * 只做一次字符串常量，运行时零计算。
 */

/** 190×190 有界噪点：作为无限平铺瓦片，位移一个瓦片即回到起点。 */
const GRAIN = (() => {
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='190' height='190'><filter id='g' x='0' y='0' width='100%' height='100%'><feTurbulence type='fractalNoise' baseFrequency='0.86' numOctaves='3' stitchTiles='stitch' seed='11'/><feColorMatrix type='saturate' values='0'/></filter><rect width='190' height='190' filter='url(%23g)' opacity='0.9'/></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg).replace(/%20/g, " ")}")`;
})();

/** 细颗粒：卡片上方的静态飞白颗粒 */
const CARD_GRAIN = (() => {
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='150' height='150'><filter id='c'><feTurbulence type='fractalNoise' baseFrequency='0.95' numOctaves='2' stitchTiles='stitch' seed='29'/><feColorMatrix type='saturate' values='0'/><feComponentTransfer><feFuncA type='linear' slope='0.52' intercept='-0.12'/></feComponentTransfer></filter><rect width='150' height='150' filter='url(%23c)'/></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg).replace(/%20/g, " ")}")`;
})();

/** 软噪声：低频、做墨色浓淡不均（作为 mask 叠在色块上） */
const SOFT_NOISE = (() => {
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300'><filter id='s'><feTurbulence type='fractalNoise' baseFrequency='0.045' numOctaves='4' stitchTiles='stitch' seed='7'/><feColorMatrix type='saturate' values='0'/></filter><rect width='300' height='300' filter='url(%23s)'/></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg).replace(/%20/g, " ")}")`;
})();

export const GRAIN_URL = GRAIN;
export const CARD_GRAIN_URL = CARD_GRAIN;
export const SOFT_NOISE_URL = SOFT_NOISE;

export const GRAIN_TILE = 190;
