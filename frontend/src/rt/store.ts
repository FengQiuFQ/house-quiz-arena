import { create } from "zustand";

/** 实时比赛状态（§14.3）：
 * - WS 订阅先建立并暂存消息 → 应用 HTTP 快照 → 只应用比快照新的进度；
 * - WS 失败但 HTTP 可用时，以 2–3s 带抖动轮询快照；连接恢复后停止轮询；
 * - 权威状态永远可经 /snapshot 恢复，断线不延长任何截止时间。
 */
export type ConnState = "idle" | "connecting" | "open" | "reconnecting" | "polling";

interface RtState {
  matchId: string | null;
  conn: ConnState;
  snapshot: import("../api/types").Snapshot | null;
  lastProgressSeq: number | null;
  /** 大屏模式：走匿名可读的 /board（不含题目内容），WS 断线只做轮询，不做答题逻辑。 */
  boardMode: boolean;
  connect: (matchId: string) => void;
  connectBoard: (matchId: string) => void;
  disconnect: () => void;
  refresh: () => Promise<void>;
}

let ws: WebSocket | null = null;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let wsBroken = false;
let buffered: import("../api/types").WsMessage[] = [];
let appliedSnapshot = false;

export const useRt = create<RtState>((set, get) => ({
  matchId: null,
  conn: "idle",
  snapshot: null,
  lastProgressSeq: null,
  boardMode: false,

  refresh: async () => {
    const mid = get().matchId;
    if (!mid) return;
    const boardMode = get().boardMode;
    const path = boardMode
      ? `/api/v1/matches/${mid}/board`
      : `/api/v1/matches/${mid}/snapshot`;
    try {
      const snap = await fetch(path, { credentials: "include" }).then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      });
      appliedSnapshot = true;
      buffered = buffered.filter((m) => (m.progress_seq ?? 0) > (get().lastProgressSeq ?? 0));
      /* 投屏大屏走的是**轮询**，它按设计永远不会开 WebSocket ——
         所以 `conn === "open"` 这个条件在 board 模式下永远不成立。
         以前这里只 set snapshot、不动 conn，于是一块挂在走廊的大屏**永远**显示
         「连接恢复中」，看起来像系统坏了（实测确认：投屏页那个指示恒为"连接恢复中"）。
         修法：board 模式下"轮询成功"就是健康态 → 置为 open；
         失败则置为 reconnecting，这样指示器才真的能区分好坏。 */
      if (boardMode) set({ conn: "open", snapshot: snap });
      else set({ snapshot: snap });
      // 应用暂存的、比快照新的消息（§14.3 竞态处理）
      for (const m of buffered.splice(0)) applyMessage(m, set, get);
    } catch {
      if (boardMode) set({ conn: "reconnecting" });
      else set({ conn: wsBroken ? "polling" : get().conn });
    }
  },

  /**
   * 大屏模式（投屏 / 走廊希沃）。
   *
   * 为什么单独一个入口：大屏是挂在墙上没人登录的屏幕，/snapshot 需要登录会一直 401。
   * 这里改读匿名可读的 /board（**不含题目内容**），并按固定 3 秒轮询刷新——
   * 不建 WS：大屏只需要进度，轮询更省心，也不会因为 WS 断开就白屏。
   */
  connectBoard: (matchId: string) => {
    get().disconnect();
    buffered = [];
    appliedSnapshot = false;
    set({ matchId, boardMode: true, conn: "polling", snapshot: null, lastProgressSeq: null });
    void get().refresh();
    startPolling(set, get);
  },

  connect: (matchId: string) => {
    if (get().matchId === matchId && ws && !get().boardMode) return;
    get().disconnect();
    buffered = [];
    appliedSnapshot = false;
    set({ matchId, boardMode: false, conn: "connecting", snapshot: null, lastProgressSeq: null });
    void get().refresh();

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws?match=${encodeURIComponent(matchId)}`);
    ws.onopen = () => {
      wsBroken = false;
      stopPolling();
      set({ conn: "open" });
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as import("../api/types").WsMessage;
        if (!appliedSnapshot) {
          buffered.push(msg);
          return;
        }
        applyMessage(msg, set, get);
      } catch { /* 忽略坏帧 */ }
    };
    ws.onclose = () => {
      ws = null;
      wsBroken = true;
      set({ conn: "polling" });
      startPolling(set, get);
      // 指数退避简化为固定 3s 重连（现场规模小，§14.3 允许轮询兜底）
      setTimeout(() => {
        if (get().matchId === matchId && get().conn !== "open") get().connect(matchId);
      }, 3000);
    };
    ws.onerror = () => ws?.close();
  },

  disconnect: () => {
    stopPolling();
    if (ws) { try { ws.close(); } catch { /* noop */ } ws = null; }
    set({ matchId: null, conn: "idle", snapshot: null, lastProgressSeq: null });
  },
}));

function applyMessage(
  msg: import("../api/types").WsMessage,
  set: (partial: Partial<RtState> | ((st: RtState) => Partial<RtState>)) => void,
  get: () => RtState,
) {
  if (msg.type === "progress.snapshot") {
    const seq = msg.progress_seq ?? 0;
    const last = get().lastProgressSeq ?? 0;
    if (seq <= last) return; // 过期快照丢弃
    set((st) => {
      const snap = st.snapshot;
      if (!snap) return {};
      const payload = msg.payload as Record<string, unknown>;
      const { you, ...rest } = payload as { you?: unknown };
      const progress = { ...snap.progress, ...(rest as object) };
      return {
        lastProgressSeq: seq,
        snapshot: {
          ...snap,
          progress: progress as typeof snap.progress,
          you: you !== undefined ? { ...snap.you, ...(you as object) } as typeof snap.you : snap.you,
        },
      };
    });
  } else if (msg.type === "round.opened" || msg.type === "round.closed" || msg.type === "match.state") {
    void get().refresh(); // 关键阶段变化 → 重新同步权威快照（§14.3）
  }
}

function startPolling(set: (p: Partial<RtState>) => void, get: () => RtState) {
  if (pollTimer) return;
  const tick = async () => {
    pollTimer = null;
    if (!get().matchId || get().conn === "open") return;
    await get().refresh();
    if (get().conn !== "open") {
      const delay = 2000 + Math.random() * 1000; // 2–3s 带抖动（§14.3）
      pollTimer = setTimeout(tick, delay);
    }
  };
  pollTimer = setTimeout(tick, 300);
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

/**
 * 服务器时钟偏移估计（毫秒），用于让倒计时与本机时钟快慢无关。
 *
 * ⚠️ 这里必须**按 server_time 的值缓存**，不能每次调用都算 `server_time - Date.now()`，
 * 也不能只按对象身份缓存（见下）。
 *
 * 为什么（这是一个真实故障，不是洁癖）：
 *   `server_time` 是**快照生成那一刻**的服务器时间，是个固定字符串；
 *   而 `Date.now()` 每毫秒都在走。若每次渲染都重算，得到的是
 *       offset = server_time − now      （now 越大，offset 越小）
 *   把它代回倒计时公式：
 *       remain = deadline − (now + offset)
 *              = deadline − (now + server_time − now)
 *              = deadline − server_time        ← now 被消掉了，成了常量
 *   结果就是**倒计时永远停在"快照到达时的剩余时间"上，一秒都不走**。
 *   实测：等待室时钟固定显示 4:56，等 20 秒后仍是 4:56，
 *   因此永远到不了 0、永远不显示"准备开始"，选手会以为页面卡死。
 *
 * 为什么不能只按**对象身份**缓存：WS 每秒推一次 `progress.snapshot`，
 * store 用 `{...snap, progress}` 合并出**新对象**，但 `server_time` 沿用旧值 ——
 * 对象换了、时间没换，按身份的缓存每次都 miss，等于没缓存。
 * 所以这里按 `server_time` 字符串本身缓存：同一个时间戳永远得到同一个偏移，
 * 服务器真正下发新时间戳时才重新校准一次。
 *
 * 用 Map + 容量上限而不是 WeakMap：键是字符串，且同一场次里活跃时间戳只有一两个，
 * 上限只是防御长期不刷新的页面把 map 撑大。
 */
const skewByServerTime = new Map<string, number>();
const SKEW_CACHE_MAX = 64;

export function serverOffset(snap: { server_time: string } | null): number {
  if (!snap?.server_time) return 0;
  const key = snap.server_time;
  const hit = skewByServerTime.get(key);
  if (hit !== undefined) return hit;
  const server = new Date(key).getTime();
  // 解析失败（脏数据）时退化为 0：宁可用本机时钟，也不要 NaN 把倒计时变成空白
  const skew = Number.isFinite(server) ? server - Date.now() : 0;
  if (skewByServerTime.size >= SKEW_CACHE_MAX) {
    // 只清最旧的一半，避免和正在渲染的快照反复打架
    const keys = [...skewByServerTime.keys()].slice(0, SKEW_CACHE_MAX / 2);
    for (const k of keys) skewByServerTime.delete(k);
  }
  skewByServerTime.set(key, skew);
  return skew;
}
