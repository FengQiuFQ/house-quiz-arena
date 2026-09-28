import { useEffect, useMemo, useRef, useState } from "react";
import type { Snapshot } from "../api/types";
import Countdown from "./Countdown";
import PaperMaterial from "../design/PaperMaterial";

/**
 * 选手流程的三块非答题界面：**签到 → 等待室 → 开赛过渡**。
 *
 * 为什么单独抽出来：原来的 Play.tsx 把"签到/等待/答题"三件事揉在一个组件里，
 * 靠一串条件表达式分流，读起来像猜谜，也容易漏掉状态。
 * 现在由服务端给出唯一权威的 `you.flow`（checking/waiting/answering/done/spectate），
 * 前端只负责把对应那一屏画好 —— 分工清楚，也不会出现"该进等待室却进了答题页"。
 *
 * 一处重要的产品判断：**倒计时归零不等于开赛**。
 * 真正开赛由主持人执行 start_match；所以归零后显示"等待主持人开赛"，
 * 而不是假装已经能答题（那会让学生点进去发现答不了，现场更慌）。
 * 一旦状态真的变成 running，立刻播一次开赛过渡并进入答题界面。
 */

function FlowSteps({ current }: { current: number }) {
  return <ol className="flow-steps" aria-label="参赛流程">{["签到", "等待开赛", "作答", "揭晓"].map((label, i) => <li key={label} aria-current={i === current ? "step" : undefined}>{String(i + 1).padStart(2, "0")} {label}</li>)}</ol>;
}

export function CheckInPanel({
  snapshot, matchTitle, onCheckIn, busy, err,
}: {
  snapshot: Snapshot;
  matchTitle: string;
  onCheckIn: () => void;
  busy: boolean;
  err: string | null;
}) {
  const houses = snapshot.progress.houses;
  return (
    <div className="p-center flow-panel">
      <FlowSteps current={0} />
      <p className="p-cat">赛前签到</p>
      <h2 className="flow-title">{matchTitle}</h2>
      <div className="flow-alert"><strong>请在开赛前完成签到</strong><span>签到后才能正式参赛。比赛开始后，签到入口将关闭。</span></div>

      <div className="checkin-grid mt-6">
        {houses.map((h) => (
          <div className="ck-cell" key={h.key}>
            <span className="ck-name">
              <i className="house-dot" style={{ background: h.color_light }} aria-hidden="true" />
              {h.name}
            </span>
            <b className="ck-num num">{h.total}</b>
            <span className="ck-unit">人已签到</span>
          </div>
        ))}
      </div>

      {err && <div className="error-box mt-4" role="alert">{err}</div>}

      <div className="row mt-6" style={{ justifyContent: "center" }}>
        <button className="btn btn-primary btn-xl" onClick={onCheckIn} disabled={busy}>
          {busy ? "签到中…" : "签到，准备参赛"}
        </button>
      </div>
      <p className="p-note">
        本场题量、作答时限与计分方式以赛前公布规则为准。提交后请确认页面显示“提交成功”。
      </p>
    </div>
  );
}

export function WaitingRoom({
  snapshot, matchTitle, startAt, offset, compactNote, onSpectate,
}: {
  snapshot: Snapshot;
  matchTitle: string;
  startAt: string | null;
  offset: number;
  compactNote?: string;
  onSpectate?: () => void;
}) {
  const entry = snapshot.you?.entry;
  const houses = snapshot.progress.houses;
  const paused = snapshot.match.status === "paused";

  // 倒计时归零后不等于开赛：显示等待主持人
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!startAt) return;
    const t = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(t);
  }, [startAt]);
  const remainSec = startAt ? Math.max(0, (new Date(startAt).getTime() - (now + offset)) / 1000) : null;
  const reached = remainSec !== null && remainSec <= 0;

  const groups = useMemo(
    () => houses.map((h) => ({ ...h, rate: h.total ? h.submitted / h.total : 0 })),
    [houses],
  );

  return (
    <div className="p-center flow-panel">
      <FlowSteps current={1} />
      <p className="p-cat">{paused ? "比赛暂停中" : "签到成功 · 等待室"}</p>
      <h2 className="flow-title">{matchTitle}</h2>

      {entry && (
        <p className="flow-me">
          <span className={`house-dot h-${entry.house_key}`} aria-hidden="true" />
          你将代表 <b>{entry.house_name}</b> 参赛 · 你的编号 <span className="num">{entry.alias || "待分配"}</span>
          {entry.is_captain && <span className="tag ok" style={{ marginLeft: 8 }}>队长</span>}
        </p>
      )}

      {/* 主时钟：有预定开赛时间就给倒计时；没有就说"即将开始"，不伪造一个数字 */}
      <div className="wait-clock">
        {paused ? (
          <p className="wait-hint">主持人暂停了比赛，请留在本页，恢复后会自动继续。</p>
        ) : remainSec === null ? (
          <>
            <p className="wait-big">即将开始</p>
            <p className="wait-hint">你已完成签到。请留在本页，主持人开赛后会自动进入答题。</p>
          </>
        ) : reached ? (
          <>
            <p className="wait-big ready">准备开始</p>
            <p className="wait-hint">正在等待主持人发令，开始后本页会自动进入答题。</p>
          </>
        ) : (
          <>
            <div className="cb-clock wait-clock-row" aria-label="距开赛剩余时间">
              <Countdown deadline={startAt!} serverOffset={offset} size="big" />
            </div>
            <p className="wait-hint">开赛后本页会自动进入答题，不用刷新。</p>
          </>
        )}
      </div>

      <div className="checkin-grid mt-6">
        {groups.map((h) => (
          <div className="ck-cell" key={h.key}>
            <span className="ck-name">
              <i className="house-dot" style={{ background: h.color_light }} aria-hidden="true" />
              {h.name}
            </span>
            <b className="ck-num num">{h.total}</b>
            <span className="ck-unit">人已签到</span>
          </div>
        ))}
      </div>

      {compactNote && <p className="p-note">{compactNote}</p>}

      <div className="row mt-5" style={{ justifyContent: "center" }}>
        {onSpectate && (
          <button className="btn" onClick={onSpectate}>先看观战画面</button>
        )}
        <span className="tag">已完成签到</span>
      </div>
    </div>
  );
}

/** 开赛过渡：一次性、约 1.1 秒，把"从等待到答题"这件事演出来。
 *  不是加载遮罩（不阻塞），只是让界面切换有因果感；
 *  减弱档直接跳过（由调用方判断）。 */
export function MatchStartCurtain({ house, title }: { house: string; title: string }) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <div className="start-curtain" ref={ref} role="status" aria-live="assertive">
      <div className="sc-ink" aria-hidden="true">
        <PaperMaterial house={house} variant="stage" sheen={false} />
      </div>
      <div className="sc-text">
        <p className="sc-kicker">比赛开始</p>
        <h2 className="sc-title">{title}</h2>
        <p className="sc-sub">第 1 题马上呈现 · 请把手指放在选项上</p>
      </div>
    </div>
  );
}

/** 比赛已进入锁定/结算阶段：选手看到的等待揭晓界面。 */
export function WaitingForReveal({
  snapshot, matchTitle, onResults,
}: {
  snapshot: Snapshot;
  matchTitle: string;
  onResults: () => void;
}) {
  const reveal = snapshot.reveal;
  return (
    <div className="p-center flow-panel">
      <FlowSteps current={3} />
      <p className="p-cat">作答已结束</p>
      <h2 className="flow-title">{matchTitle}</h2>
      <p className="r-sub mt-3">
        本场作答已结束。已成功提交的答案已保存，最终排名将在统一揭晓后公布。
      </p>
      {reveal && (
        <p className="wait-hint num mt-4">
          公开时间 {new Date(reveal.reveal_at).toLocaleString("zh-CN", { hour12: false })}
        </p>
      )}
      <div className="row mt-6" style={{ justifyContent: "center" }}>
        <button className="btn" onClick={onResults}>查看揭晓页</button>
        {snapshot.you?.entry && (
          <span className="tag">已提交答案不会被改动</span>
        )}
      </div>
    </div>
  );
}
