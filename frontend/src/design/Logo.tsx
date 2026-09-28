/** MGUS 标识：按用户提供的屋檐与三拱图形绘制。 */

export function Monogram({ size = 28, title }: { size?: number; accent?: number; title?: string }) {
  return (
    <img
      src="/mgus-mark.svg"
      width={size}
      height={size}
      alt={title ?? ""}
      aria-hidden={title ? undefined : true}
      draggable={false}
    />
  );
}

/** 字标 + 文字组合：中文承担阅读，英文只作辅助标识；窄宽度由 CSS 逐级收起副行。 */
export function Wordmark({ compact = false }: { compact?: boolean }) {
  return (
    <span className="wordmark">
      <Monogram size={compact ? 32 : 38} />
      <span className="wordmark-text">
        <span className="wordmark-latin">MGUS</span>
        <span className="wordmark-cn">国贸枫禾</span>
        <span className="wordmark-sub">House 知识竞赛</span>
      </span>
    </span>
  );
}

export default Wordmark;
