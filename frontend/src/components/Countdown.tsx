import { useEffect, useState } from "react";

/** 与服务器时间同步的倒计时（显示用；评分判定永远以服务端为准）。 */
export default function Countdown({ deadline, serverOffset, size }: { deadline: string | null; serverOffset: number; size?: "big" | "small" }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 250);
    return () => clearInterval(t);
  }, []);
  if (!deadline) return null;
  const remain = Math.max(0, new Date(deadline).getTime() - (Date.now() + serverOffset));
  const total = Math.ceil(remain / 1000);
  const label = total >= 60 ? `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}` : String(total);
  return (
    <span className={`num countdown countdown-${size ?? "small"}`} style={total <= 5 ? { color: "var(--state-warn)" } : undefined}>
      {label}
    </span>
  );
}
