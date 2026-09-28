import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { Snapshot } from "../api/types";
import { useRt, serverOffset } from "../rt/store";
import { getLevel } from "../design/motion";
import Countdown from "../components/Countdown";
import PaperMaterial from "../design/PaperMaterial";
import {
  CheckInPanel, MatchStartCurtain, WaitingForReveal, WaitingRoom,
} from "../components/PlayerFlow";
import { useAuth } from "../App";

/** 答题页顶部状态条（三条信息：身份 / 进度 / 连接）。
 *  抽成组件是因为四个流程界面都要这一段，重复四遍容易改漏。 */
function TopStrip({ snapshot, conn, matchId }: {
  snapshot: Snapshot; conn: string; matchId: string;
}) {
  const entry = snapshot.you?.entry;
  return (
    <>
      <div className="p-topbar">
        <span className="pill">
          {entry ? <>{entry.house_name} · {entry.alias}{entry.is_captain ? " · 队长" : ""}</> : "本场观众"}
        </span>
        <span className="num">已完成 {snapshot.progress.rounds_done ?? 0} 回合</span>
        <span className={`conn ${conn === "open" ? "" : "off"}`} style={{ marginLeft: "auto" }}>
          <i />{conn === "open" ? "已连接" : conn === "polling" ? "连接中断 · 快照恢复中" : "连接中…"}
        </span>
      </div>
      <div className="p-progress">
        <i style={{ width: `${Math.min(100, ((snapshot.progress.rounds_done ?? 0) / Math.max(1, snapshot.round?.total ?? snapshot.match.planned_questions ?? 1)) * 100)}%` }} />
      </div>
      <span className="sr-only">比赛编号 {matchId}</span>
    </>
  );
}

type SubmitState = "idle" | "picked" | "submitting" | "accepted" | "unknown" | "timeout";

/** 抽题仪式：抽象题卡聚合 → 错位流动 → 当前题卡前移 → 展开题目。
 *  题卡只有抽象背面，不含未来题干/选项/答案；点击可跳过；作答开放立即结束，不占作答时间。 */
function DrawCeremony({ onDone }: { onDone: () => void }) {
  const [merged, setMerged] = useState(false);
  const [revealQ, setRevealQ] = useState(false);
  useEffect(() => {
    const t1 = window.setTimeout(() => setMerged(true), 600);
    const t2 = window.setTimeout(() => setRevealQ(true), 980);
    const t3 = window.setTimeout(onDone, 1200);
    return () => { window.clearTimeout(t1); window.clearTimeout(t2); window.clearTimeout(t3); };
  }, [onDone]);
  const houses = ["dragon", "tortoise", "phoenix", "tiger", "dragon"];
  const ends: Array<[string, string, string]> = [
    ["-58px", "-12px", "-7deg"], ["48px", "6px", "5deg"], ["-24px", "16px", "-3deg"],
    ["30px", "-18px", "8deg"], ["2px", "-2px", "1deg"],
  ];
  return (
    <div className={`draw-stage ${merged ? "merged" : ""} ${revealQ ? "reveal-q" : ""}`} onClick={onDone} role="presentation" title="点击跳过演出">
      {houses.map((h, i) => (
        <div
          key={i}
          className={`draw-card house-${h} c${i} ${i === 4 ? "leader" : ""}`}
          style={{ ["--sx" as string]: ends[i][0], ["--sy" as string]: ends[i][1], ["--sr" as string]: ends[i][2] }}
        >
          <PaperMaterial house={h} variant="orb" sheen={false} />
        </div>
      ))}
      <p className="p-cat center" style={{ position: "absolute", bottom: -2, left: "50%", transform: "translateX(-50%)", whiteSpace: "nowrap" }}>
        抽题中（点击跳过）
      </p>
    </div>
  );
}

export default function Play() {
  const { matchId = "" } = useParams();
  const { me, reload } = useAuth();
  const nav = useNavigate();
  const qc = useQueryClient();
  const { snapshot, conn, connect, disconnect, refresh } = useRt();
  const [submitState, setSubmitState] = useState<SubmitState>("idle");
  const [picked, setPicked] = useState<string | null>(null);
  const [aidMsg, setAidMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [drawSeed, setDrawSeed] = useState<number | null>(null);
  const [drawRound, setDrawRound] = useState<string>("");
  const [checkingIn, setCheckingIn] = useState(false);
  const controlToken = useRef<string>("");
  const submittedFor = useRef<string>("");
  const lastRound = useRef<string>("");

  /* 预定开赛时间不在 snapshot 里（它属于"展示"信息，由首页接口给出）。
     等待室需要它来倒计时，所以这里单独取一次；取不到就显示"即将开始"，
     而不是伪造一个倒计时数字。 */
  const live = useQuery({
    queryKey: ["home-live", matchId],
    queryFn: () => api.get<{ live_match: { match_id: string; start_at?: string | null } | null }>("/api/v1/home"),
    refetchInterval: 15000,
  });
  const matchLiveStart = live.data?.live_match?.match_id === matchId
    ? (live.data.live_match.start_at ?? null) : null;

  useEffect(() => {
    connect(matchId);
    return () => disconnect();
  }, [matchId, connect, disconnect]);

  const offset = useMemo(() => serverOffset(snapshot), [snapshot]);

  useEffect(() => {
    if (!me) return;
    api.post<{ control_token: string }>("/api/v1/auth/control/claim")
      .then((r) => { controlToken.current = r.control_token; })
      .catch((e) => setErr(e instanceof ApiError ? e.message : "控制权认领失败，请刷新重试。"));
  }, [me]);

  useEffect(() => {
    const rd = snapshot?.round;
    if (!rd || rd.round_instance_id === lastRound.current) return;
    const first = lastRound.current === "";
    lastRound.current = rd.round_instance_id;
    // 落笔动画只在"题目已出现但还不能作答"的读题窗口里播。
    // 抢答模式没有这个窗口，动画若照播就会盖在抢答界面上——那是纯装饰挡住计分操作。
    if (rd.phase === "display" && !snapshot?.match.buzz && getLevel() === "full" && !document.hidden) {
      setDrawRound(rd.round_instance_id);
      setDrawSeed(Date.now());
    } else if (!first) {
      setDrawSeed(null);
    }
  }, [snapshot?.round, snapshot?.match.buzz]);
  useEffect(() => {
    if (snapshot?.round?.phase === "open" && drawSeed !== null) setDrawSeed(null);
  }, [snapshot?.round?.phase, drawSeed]);

  const checkIn = async () => {
    setErr(null);
    setCheckingIn(true);
    try {
      await api.post(`/api/v1/matches/${matchId}/check-in`);
      await refresh(); void qc.invalidateQueries({ queryKey: ["me"] }); reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "签到失败。");
    } finally {
      setCheckingIn(false);
    }
  };

  /**
   * 幂等键：**必须定长**。
   *
   * 曾经的实现是 `web-${me.id}-${roundId}`：两个 id 各 32 位十六进制，加上前缀与分隔符
   * 共 69 字符，超过服务端 "8–64 字符" 的校验 —— 于是**每一次提交答案都被 422 拒绝**，
   * 学生点击选项后只会看到"结果未知 · 恢复中"（本项目实测踩到，属于阻断性缺陷）。
   *
   * 现在改为把三段拼起来做一次稳定摘要，固定 40 字符，与 id 长度无关。
   * 保持"同一用户 + 同一回合 → 同一键"的语义不变，重试仍是幂等回放。
   */
  const idemKey = (roundId: string) => {
    const raw = `${me?.id ?? "anon"}|${roundId}|v1`;
    let h1 = 0x811c9dc5, h2 = 0x01000193;
    for (let i = 0; i < raw.length; i++) {
      const c = raw.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 16777619) >>> 0;
      h2 = Math.imul(h2 + c, 2246822519) >>> 0;
    }
    const part = (n: number) => n.toString(16).padStart(8, "0");
    return `w${part(h1)}${part(h2)}${part((h1 ^ h2) >>> 0)}${part((h1 + h2) >>> 0)}${part(raw.length)}`;
  };

  const submit = async (optionId: string) => {
    const rd = snapshot?.round;
    if (!rd || !me) return;
    setPicked(optionId);
    setSubmitState("submitting");
    try {
      await api.post<{ receipt_id: string }>(
        `/api/v1/matches/${matchId}/rounds/${rd.round_instance_id}/answers`,
        { option_id: optionId },
        { "Idempotency-Key": idemKey(rd.round_instance_id), "X-Control-Token": controlToken.current },
      );
      submittedFor.current = rd.round_instance_id;
      setSubmitState("accepted");
      void refresh();
    } catch (caught) {
      /* 「尚未开放」是对齐误差，不是失败。
       *
       * 服务端控制器按 tick 推进回合，客户端依据快照里的 open_at 判断"已开放"可能比
       * 服务端真正放开早几百毫秒。这种情况下幂等键不变，稍后重发就是同一次提交，
       * 绝不能让学生看到"作答通道已关闭"而放弃——那正是本场演示四院全 0 分的观感来源。
       */
      let e: unknown = caught;
      const isTooEarly = (x: unknown): boolean => x instanceof ApiError
        && (x.details as { reason?: string } | null | undefined)?.reason === "too_early";
      if (isTooEarly(e)) {
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 250));
          try {
            await api.post<{ receipt_id: string }>(
              `/api/v1/matches/${matchId}/rounds/${rd.round_instance_id}/answers`,
              { option_id: optionId },
              { "Idempotency-Key": idemKey(rd.round_instance_id), "X-Control-Token": controlToken.current },
            );
            submittedFor.current = rd.round_instance_id;
            setSubmitState("accepted");
            void refresh();
            return;
          } catch (retryErr) {
            e = retryErr;
            if (!isTooEarly(retryErr)) break;
          }
        }
      }
      if (e instanceof ApiError && (e.code === "ROUND_CLOSED" || e.code === "ROUND_NOT_OPEN")) {
        setSubmitState("timeout"); setErr(e.message); void refresh();
      } else if (e instanceof ApiError && e.code === "CONTROL_TOKEN_INVALID") {
        setSubmitState("unknown"); setErr("控制权已失效，正在重新认领，请勿重复点击。");
        const r = await api.post<{ control_token: string }>("/api/v1/auth/control/claim").catch(() => null);
        if (r) controlToken.current = r.control_token;
        setSubmitState("idle"); setPicked(null);
      } else {
        setSubmitState("unknown"); setErr(e instanceof ApiError ? e.message : "提交结果未知，正在恢复…");
      }
    }
  };

  const requestAid = async () => {
    const rd = snapshot?.round;
    if (!rd) return;
    setAidMsg(null);
    try {
      const r = await api.post<{ aid_request_id: string }>(`/api/v1/matches/${matchId}/aid-requests`, { round_instance_id: rd.round_instance_id });
      setAidMsg(`已请求援助（编号 ${r.aid_request_id.slice(0, 6)}），等待本院智囊在窗口内给出建议。`);
      void refresh();
    } catch (e) {
      setAidMsg(e instanceof ApiError ? e.message : "求援失败。");
    }
  };

  /** 环境流动保持低强度，题干与选项区域稳定（材质层不进入文字区） */
  const washHouse = snapshot?.you?.entry?.house_key ?? "dragon";

  /* ── 开赛过渡 ──
     监视 flow 从 waiting/checking 变成 answering 的那一刻，播一次 1.1 秒的开赛仪式。
     为什么需要它：两个界面差异大（等待室是大时钟，答题页是题面+选项），
     直接硬切会让人以为"页面跳了"；一段明确的开场把因果讲清楚。
     只播一次（curtainDone 锁），且只在完整动效档播。 */
  const flow = snapshot?.you?.flow;
  const prevFlow = useRef<string | undefined>(undefined);
  const [curtain, setCurtain] = useState(false);
  useEffect(() => {
    const wasWaiting = prevFlow.current === "waiting" || prevFlow.current === "checking";
    if (wasWaiting && flow === "answering" && getLevel() === "full") {
      setCurtain(true);
      const t = window.setTimeout(() => setCurtain(false), 1150);
      prevFlow.current = flow;
      return () => window.clearTimeout(t);
    }
    prevFlow.current = flow;
  }, [flow]);

  if (!me) {
    // 竞赛相关一律需要登录（服务端强制），因此这里必须给出**可点的登录入口**，
    // 而不是只留一句提示让人不知下一步做什么。登录后回到本页继续。
    return (
      <div className="player">
        <div className="p-center flow-panel">
          <p className="p-cat">需要登录</p>
          <h2 className="flow-title">比赛页面需要登录</h2>
          <p className="r-sub mt-3">
            选手用管理员发的参赛账号登录；观众也需要登录后才能观战
            （未揭晓前只能观看自己学院参与的比赛）。
          </p>
          <div className="row mt-6" style={{ justifyContent: "center" }}>
            <button
              className="btn btn-primary btn-lg"
              onClick={() => nav(`/login?next=${encodeURIComponent(`/matches/${matchId}/play`)}`)}
            >
              去登录
            </button>
            <button className="btn btn-lg" onClick={() => nav("/register")}>还没有账号？注册</button>
          </div>
        </div>
      </div>
    );
  }

  // 快照还没到时不要闪空屏：等待室与答题页的骨架不同，先给中性占位
  if (!snapshot) {
    return <div className="player"><div className="p-center"><p className="p-cat">正在进入比赛…</p></div></div>;
  }

  const matchTitle = snapshot.match.title ?? "本场比赛";
  const offsetNow = serverOffset(snapshot);
  const startAt = (matchLiveStart as string | null) ?? null;

  if (curtain) {
    return <MatchStartCurtain house={washHouse} title={matchTitle} />;
  }

  // ① 未签到且可签到
  if (flow === "checking") {
    return (
      <div className="player">
        <TopStrip snapshot={snapshot} conn={conn} matchId={matchId} />
        <CheckInPanel
          snapshot={snapshot} matchTitle={matchTitle} onCheckIn={checkIn}
          busy={checkingIn} err={err}
        />
        <div className="play-ambient has-ink" aria-hidden="true">
          <PaperMaterial house={washHouse} variant="strip" sheen={false} />
        </div>
      </div>
    );
  }

  // ② 已签到、未开赛（或暂停）→ 等待室
  if (flow === "waiting") {
    return (
      <div className="player">
        <TopStrip snapshot={snapshot} conn={conn} matchId={matchId} />
        <WaitingRoom
          snapshot={snapshot} matchTitle={matchTitle} startAt={startAt} offset={offsetNow}
          compactNote="等待期间不影响任何截止时间；开赛后本页会自动进入答题。"
          onSpectate={() => nav(`/matches/${matchId}/watch`)}
        />
        <div className="play-ambient has-ink" aria-hidden="true">
          <PaperMaterial house={washHouse} variant="strip" sheen={false} />
        </div>
      </div>
    );
  }

  // ③ 作答已结束 → 等待揭晓
  if (flow === "done") {
    return (
      <div className="player">
        <TopStrip snapshot={snapshot} conn={conn} matchId={matchId} />
        <WaitingForReveal
          snapshot={snapshot} matchTitle={matchTitle}
          onResults={() => nav(`/matches/${matchId}/results`)}
        />
        <div className="play-ambient has-ink" aria-hidden="true">
          <PaperMaterial house={washHouse} variant="strip" sheen={false} />
        </div>
      </div>
    );
  }

  // ③ 有资格但**错过了签到**（比赛已开始）→ 明确说明，并给观战入口。
  //    不把人丢进答题页，因为服务端不会让未签到者提交（那只会让他白点一场）。
  if (flow === "missed_checkin") {
    return (
      <div className="player">
        <TopStrip snapshot={snapshot} conn={conn} matchId={matchId} />
        <div className="p-center flow-panel">
          <p className="p-cat">已错过签到</p>
          <h2 className="flow-title">{matchTitle}</h2>
          <p className="r-sub mt-3">
            比赛已经开始，签到入口已关闭，因此本场无法计入你的成绩。
            可以进入观战画面为本院加油；下一场记得提前签到。
          </p>
          <div className="row mt-6" style={{ justifyContent: "center" }}>
            <button className="btn btn-primary" onClick={() => nav(`/matches/${matchId}/watch`)}>
              进入观战
            </button>
            <button className="btn" onClick={() => nav(`/matches/${matchId}/screen`)}>
              展示大屏
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ④ 不在本场名单 → 旁观
  if (flow === "spectate") {
    return (
      <div className="player">
        <div className="p-center flow-panel">
          <p className="p-cat">观赛</p>
          <h2 className="flow-title">{matchTitle}</h2>
          <p className="r-sub mt-3">
            你不在本场比赛名单中。可以打开观战画面或投屏大屏旁观（未揭晓的比分不会显示）。
          </p>
          <div className="row mt-6" style={{ justifyContent: "center" }}>
            <button className="btn btn-primary" onClick={() => nav(`/matches/${matchId}/watch`)}>
              进入观战
            </button>
            <button className="btn" onClick={() => nav(`/matches/${matchId}/screen`)}>
              展示大屏
            </button>
          </div>
        </div>
      </div>
    );
  }

  const rd = snapshot?.round;
  const you = snapshot?.you;
  const entry = you?.entry;
  const myAnswer = you?.my_answer;
  const phase = rd?.phase;
  /* 抢答：读题窗口为 0，题目一出现就可以作答。
   *
   * 这里刻意**不**用 drawSeed（水墨落笔动画）挡住作答按钮：
   * 动画是我们自己加的装饰，抢答赛里那 0.x 秒可能是决定谁先抢到的关键，
   * 让装饰挡在计分链路前面是错的。动画照放，但只作为背景层，不参与任何判定；
   * 服务端也一视同仁地接受读题阶段的提交（见 submissions/router.py 的 base_ok）。
   */
  const buzz = snapshot?.match.buzz ?? false;
  const answerable = phase === "open" || (buzz && phase === "display");
  const ceremonyActive = drawSeed !== null && drawRound === rd?.round_instance_id;
  const canSubmit = answerable && !myAnswer && submitState !== "accepted" && submittedFor.current !== rd?.round_instance_id;
  const aidWindowOpen = rd?.phase === "display" && rd.aid_window && entry?.is_captain &&
    (snapshot?.progress.aid?.[entry.house_key]?.remaining ?? 0) > 0;

  return (
    <div className="player">
      <div className="p-topbar">
        <span className="pill">
          {entry ? <>{entry.house_name} · {entry.alias}{entry.is_captain ? " · 队长" : ""}</> : me.username}
        </span>
        <span className="num">已完成 {snapshot?.progress.rounds_done ?? 0} 回合</span>
        <span className={`conn ${conn === "open" ? "" : "off"}`} style={{ marginLeft: "auto" }}>
          <i />{conn === "open" ? "已连接" : conn === "polling" ? "连接中断 · 快照恢复中" : "连接中…"}
        </span>
      </div>
      <div className="p-progress">
        <i style={{ width: `${Math.min(100, ((snapshot?.progress.rounds_done ?? 0) / Math.max(1, rd?.total ?? 1)) * 100)}%` }} />
      </div>

      {err && <div className="error-box" role="alert">{err}</div>}

      {!entry && snapshot?.match.status === "lobby" && (
        <div className="p-center">
          <p className="p-cat">赛前大厅</p>
          <p className="p-q">{snapshot.match.title}</p>
          <p className="r-sub mt-3">
            已签到 {snapshot.progress.total_participants} 人（
            {snapshot.progress.houses.map((h) => `${h.name} ${h.total}`).join(" / ")}）
          </p>
          <div className="row mt-5" style={{ justifyContent: "center" }}>
            <button className="btn btn-primary" onClick={checkIn}>签到参赛</button>
            <a className="btn" href={`/matches/${matchId}/screen`}>打开投屏视图</a>
          </div>
          <p className="p-note">统一回合 · 同题同倒计时 · 服务端判分 · 错误或超时计零 · House 得分为成员平均分。</p>
        </div>
      )}

      {!entry && snapshot && snapshot.match.status !== "lobby" && (
        <div className="p-center">
          <p className="p-cat">观赛</p>
          <p className="p-q">{snapshot.match.title}</p>
          <p className="r-sub mt-3">你不在本场比赛名单中。可打开投屏视图旁观（不显示未揭晓数据）。</p>
          <div className="mt-5"><a className="btn" href={`/matches/${matchId}/screen`}>打开投屏视图</a></div>
        </div>
      )}

      {entry && !rd && (
        <div className="p-center">
          {snapshot?.reveal ? (
            <>
              <p className="p-cat">等待统一揭晓</p>
              <p className="p-q">主持人已安排揭晓仪式</p>
              <p className="r-sub num mt-3">公开时间 {snapshot.reveal.reveal_at?.replace("T", " ").slice(0, 19)}（UTC）</p>
              <div className="mt-5"><a className="btn btn-primary" href={`/matches/${matchId}/results`}>进入揭晓页</a></div>
            </>
          ) : (
            <>
              <p className="p-cat">{snapshot?.match.status === "paused" ? "比赛中断" : "等待室"}</p>
              <p className="p-q">{snapshot?.match.status === "paused" ? "主持人暂停中，请等待现场指示" : "等待下一回合…"}</p>
              <p className="r-sub mt-3">已提交的答案由服务器持久保存；等待不会影响任何截止时间。</p>
            </>
          )}
        </div>
      )}

      {entry && rd && (
        <>
          {/* 落笔动画只作背景：抢答赛里不能用它挡住作答（见 answerable 处的说明） */}
          {ceremonyActive && (
            <div className="p-center" aria-hidden="true">
              <DrawCeremony onDone={() => setDrawSeed(null)} />
            </div>
          )}

          <div className="p-center round-enter" key={rd.round_instance_id}>
            <div className="p-count num">
              <Countdown deadline={answerable ? rd.deadline_at : rd.open_at} serverOffset={offset} size="big" />
              <small>{answerable ? (buzz ? "抢答中" : "作答中") : "读题"}</small>
            </div>
            <div className="mt-6">
              <p className="p-cat">第 {rd.position ?? "?"} 题 / 共 {rd.total} 题{rd.attempt_no > 1 ? ` · 重开第 ${rd.attempt_no} 次` : ""}</p>
              <p className="p-q">{rd.prompt}</p>
            </div>
          </div>

          {aidWindowOpen && (
            <div className="row mt-4" style={{ justifyContent: "center" }}>
              <button className="btn" onClick={requestAid}>呼叫本院智囊（求援窗口内）</button>
              {aidMsg && <span style={{ fontSize: "var(--fs-sm)", color: "var(--ink-2)" }}>{aidMsg}</span>}
            </div>
          )}

          <div className="options" role="group" aria-label="选项">
            {rd.options.map((o, i) => {
              const mine = picked === o.option_id || myAnswer?.option_id === o.option_id;
              return (
                <button
                  key={o.option_id}
                  className={`opt ${mine ? "selected" : ""}`}
                  disabled={!canSubmit}
                  onClick={() => submit(o.option_id)}
                  aria-pressed={mine}
                >
                  <span className="letter">{String.fromCharCode(65 + i)}</span>
                  <span className="otext">{o.text}</span>
                  {myAnswer?.option_id === o.option_id || (submitState === "accepted" && picked === o.option_id) ? (
                    <span className="ostate">服务器已接收{myAnswer ? ` · 回执 ${myAnswer.receipt_id.slice(0, 10)}` : "…"}</span>
                  ) : submitState === "submitting" && picked === o.option_id ? (
                    <span className="ostate">提交中…</span>
                  ) : submitState === "unknown" && picked === o.option_id ? (
                    <span className="ostate" style={{ color: "var(--state-warn)" }}>结果未知 · 恢复中</span>
                  ) : !canSubmit && !answerable ? (
                    <span className="ostate">读题中 · 稍后作答</span>
                  ) : null}
                </button>
              );
            })}
          </div>

          <div className="p-foot">
            <span>当前题已提交 <b className="num" style={{ color: "var(--ink-2)" }}>{snapshot?.progress.total_submitted ?? 0} / {snapshot?.progress.total_participants ?? 0}</b> 人</span>
            <span>{answerable ? (buzz ? "抢答中 · 截止后自动关闭" : "作答阶段 · 截止后自动关闭") : "读题阶段（未开放提交）"}</span>
          </div>
          <p className="p-note">赛中不公布正确答案；提交以服务器接收时间为准，断线不延长统一截止时间。{
            snapshot?.match.buzz ? "本题为抢答：看到题目即可作答，作答速度计入综合评分（口径见结果页）。" : ""}</p>
        </>
      )}

      {/* 环境流动：底部一条低强度材质带（absolute + 全内容 z-index 更高），
          题干与选项区域完全不受影响，位置也不会漂移 */}
      <div className="play-ambient has-ink" aria-hidden="true">
        <PaperMaterial house={washHouse} variant="strip" sheen={false} />
      </div>
    </div>
  );
}
