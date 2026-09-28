import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { getLevel } from "../design/motion";
import Select from "../components/Select";
import SubjectQuota from "../components/SubjectQuota";
import QuestionsTab from "./AdminQuestions";
import type { ResultsData } from "../api/types";

interface MatchItem { id: string; title: string; mode: string; status: string; state_version: number; planned_questions: number; fixed_k: number | null; competition_mode?: string }
interface MatchDetail {
  match: { id: string; title: string; status: string; state_version: number; planned_questions: number; fixed_k: number | null; min_valid_questions: number | null; display_seconds: number; answer_seconds: number; interlude_seconds: number; aid_enabled: boolean; aid_window_seconds: number; ceremony_start_at: string | null; reveal_at: string | null; spectator_mode: string; houses_scope: string[] | null; scoring_mode: string; scoring_method: string; weight_accuracy: number; weight_speed: number; weight_time: number; weight_participation: number; scoring_note: string; buzz: boolean; competition_mode: string; questions_per_house: number | null;
    // 题单实际来自哪个题库（后端由题单反推）：official 竞赛题库 / practice 日常题库
    question_pool?: string | null; question_pool_label?: string | null;
    question_pools?: Array<{ pool: string; label: string; count: number }> };
  roster: Array<{ entry_id: string; username: string; house: string | null; house_key: string | null; alias: string; is_captain: boolean; checked_in: boolean; scoring_status: string; exclusion_reason: string | null }>;
  rounds: Array<{ id: string; status: string; attempt_no: number; display_at: string | null; open_at: string | null; deadline_at: string | null; void_reason: string | null }>;
  aid: Array<{ house: string | null; house_key: string | null; remaining: number }>;
}
interface QItem { question_id: string; version_id: string; version_no: number; subject: string; pool: string; status: string; review_state: string; prompt: string; options: Array<{ option_id: string; text: string }>; correct_option_id: string }
interface UserItem {
  id: string; username: string; display_name: string | null; role: string; status: string;
  house: { key: string; name_cn?: string } | null;
}

const TABS = ["matches", "questions", "users", "settings", "audit"] as const;
type Tab = typeof TABS[number];

/** 主持台上看到的是"做什么"，不是内部命令名。 */
const ACTION_LABEL: Record<string, string> = {
  open_lobby: "开启签到", start_match: "开始比赛", pause: "暂停", resume: "继续",
  lock: "结束作答", calculate: "结算成绩", schedule_reveal: "排期揭晓",
  cancel: "取消比赛", recalculate: "生成更正版本", publish_correction: "发布更正版本",
};
/** 会改动比赛进程或对外可见状态的命令，执行前弹确认。 */
const NEEDS_CONFIRM = ["start_match", "pause", "lock", "calculate", "cancel", "schedule_reveal", "recalculate", "publish_correction"];
const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", lobby: "签到中", running: "进行中", paused: "已暂停",
  locked: "作答结束", calculated: "已结算", reveal_scheduled: "待揭晓",
  revealed: "已揭晓", archived: "已归档", cancelled: "已取消",
};

/** 弹窗从触发位置展开：轮廓先出现、内容随后进入；关闭回到触发位置，并把焦点还给触发元素。 */
function OriginModal({ rect, onClose, children }: { rect: DOMRect | null; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const prevFocus = useRef<HTMLElement | null>(null);

  // 焦点管理：进入即聚焦首个可操作项；Tab 循环；Esc 关闭；卸载时焦点归位
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    prevFocus.current = document.activeElement as HTMLElement | null;
    const focusables = () => Array.from(el.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"))
      .filter((n) => !n.hasAttribute("disabled") && n.offsetParent !== null);
    focusables()[0]?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); doClose(); return; }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (!items.length) return;
      const firstEl = items[0], lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) { e.preventDefault(); lastEl.focus(); }
      else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); firstEl.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      prevFocus.current?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!rect || getLevel() !== "full") return;
    const r = el.getBoundingClientRect();
    const dx = rect.left + rect.width / 2 - (r.left + r.width / 2);
    const dy = rect.top + rect.height / 2 - (r.top + r.height / 2);
    const s = Math.max(0.35, Math.min(0.9, rect.width / Math.max(r.width, 1)));
    el.style.transform = `translate(${dx}px, ${dy}px) scale(${s})`;
    el.style.opacity = "0";
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.classList.add("m-anim");
      el.style.transform = "none";
      el.style.opacity = "1";
    }));
  }, [rect]);

  const doClose = () => {
    const el = ref.current;
    if (!el || !rect || getLevel() !== "full") { onClose(); return; }
    const r = el.getBoundingClientRect();
    const dx = rect.left + rect.width / 2 - (r.left + r.width / 2);
    const dy = rect.top + rect.height / 2 - (r.top + r.height / 2);
    const s = Math.max(0.35, Math.min(0.9, rect.width / Math.max(r.width, 1)));
    el.classList.add("m-anim");
    el.style.transform = `translate(${dx}px, ${dy}px) scale(${s})`;
    el.style.opacity = "0";
    window.setTimeout(onClose, 240);
  };

  return (
    <div className="modal-scrim" onClick={doClose}>
      <div
        className="modal-panel card"
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label="危险操作确认"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 560, width: "92vw" }}
      >
        {children}
      </div>
    </div>
  );
}

export default function Admin() {
  const [tab, setTab] = useState<Tab>("matches");
  return (
    <div className="container page">
      <div className="spread">
        <h1 className="section-title">管理后台</h1>
        <span className="push note" style={{ border: 0, paddingLeft: 0 }}>危险操作均需确认，并写入审计日志。</span>
      </div>
      <div className="admin-tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)} role="tab" aria-selected={tab === t}>
            {{ matches: "比赛与控制", questions: "题库审核", users: "账号与角色",
               settings: "注册与设置", audit: "审计日志" }[t]}
          </button>
        ))}
      </div>
      {tab === "matches" && <MatchesTab />}
      {tab === "questions" && <QuestionsTab />}
      {tab === "users" && <UsersTab />}
      {tab === "settings" && <SettingsTab />}
      {tab === "audit" && <AuditTab />}
    </div>
  );
}

/** 数据大屏 + 注册开关 + 待审队列。
 *  为什么把这三块放同一个标签页：它们是同一个工作流 ——
 *  "开注册 → 看谁来了 → 审掉/拒绝 → 看各院人数是否均衡"。分散到三个页面会来回跳。 */
function SettingsTab() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [codeDraft, setCodeDraft] = useState<string | null>(null);

  const overview = useQuery({
    queryKey: ["admin-overview"],
    queryFn: () => api.get<{
      houses: Array<{
        house_key: string; house_name: string; color_light: string; color_deep: string;
        members: number; active_members: number; banned: number;
        students: number; advisors: number;
      }>;
      unassigned: { members: number; active_members: number; banned: number };
      totals: { users: number; banned: number; self_registered: number; by_role: Record<string, number> };
      settings: {
        registration_open: boolean; registration_code: string;
        registration_default_role: string; registration_review: boolean;
      };
      imbalance: null | { spread_pct: number; max: number; min: number; hint: string };
    }>("/api/v1/admin/overview"),
    refetchInterval: 10000,
  });
  const pending = useQuery({
    queryKey: ["admin-pending"],
    queryFn: () => api.get<{ items: Array<{ id: string; username: string; display_name: string | null; house: { key: string; name_cn: string } | null; created_at: string }>; total: number }>(
      "/api/v1/admin/users/pending"),
    refetchInterval: 8000,
  });

  const patch = async (body: Record<string, unknown>, note: string) => {
    setMsg(null); setErr(null);
    try {
      await api.patch("/api/v1/admin/settings", body);
      setMsg(note);
      void qc.invalidateQueries({ queryKey: ["admin-overview"] });
      void qc.invalidateQueries({ queryKey: ["registration-info"] });
    } catch (e) { setErr(e instanceof ApiError ? e.message : "保存失败。"); }
  };

  const review = async (id: string, approve: boolean) => {
    setMsg(null); setErr(null);
    try {
      await api.post(`/api/v1/admin/users/${id}/review`,
        { approve, reason: approve ? null : "注册未通过审核" });
      setMsg(approve ? "已通过，该账号现在可以登录。" : "已拒绝，该账号无法登录。");
      void qc.invalidateQueries({ queryKey: ["admin-pending"] });
      void qc.invalidateQueries({ queryKey: ["admin-overview"] });
    } catch (e) { setErr(e instanceof ApiError ? e.message : "操作失败。"); }
  };

  const s = overview.data?.settings;
  const ov = overview.data;

  return (
    <div className="mt-5">
      {msg && <div className="ok-box" role="status">{msg}</div>}
      {err && <div className="error-box" role="alert">{err}</div>}

      {/* ── 数据大屏 ── */}
      <h3 className="subsection-title">各学院人数</h3>
      <p className="note">
        「可上场」不含被封禁与待审核的账号 —— 这是能真正计入学院分的人数。
      </p>
      {ov && (
        <>
          <div className="ov-grid mt-4">
            {ov.houses.map((h) => (
              <div className="ov-cell" key={h.house_key}>
                <span className="ov-name">
                  <i className="house-dot" style={{ background: h.color_light }} aria-hidden="true" />
                  {h.house_name}
                </span>
                <b className="ov-big num">{h.active_members}</b>
                <span className="ov-sub">可上场</span>
                <dl className="ov-facts">
                  <div><dt>总账号</dt><dd>{h.members}</dd></div>
                  <div><dt>已封禁</dt><dd>{h.banned}</dd></div>
                  <div><dt>学生</dt><dd>{h.students}</dd></div>
                  <div><dt>智囊</dt><dd>{h.advisors}</dd></div>
                </dl>
              </div>
            ))}
          </div>
          <div className="row mt-4">
            <span className="tag">账号合计 {ov.totals.users}</span>
            <span className="tag">已封禁 {ov.totals.banned}</span>
            <span className="tag">自助注册 {ov.totals.self_registered}</span>
            <span className="tag">未归属学院 {ov.unassigned.members}（可上场 {ov.unassigned.active_members}）</span>
          </div>
          {ov.imbalance && (
            <div className="info-box mt-4">
              <b>人数不均衡提醒</b>：最多 {ov.imbalance.max} 人、最少 {ov.imbalance.min} 人，
              相差 {ov.imbalance.spread_pct}%。{ov.imbalance.hint}
            </div>
          )}
        </>
      )}

      {/* ── 注册开关 ── */}
      <h3 className="subsection-title mt-6">自助注册</h3>
      {s && (
        <div className="panel-card mt-4">
          <label className="sw-row">
            <input type="checkbox" checked={s.registration_open}
                   onChange={(e) => patch({ registration_open: e.target.checked },
                     e.target.checked ? "已开放自助注册。" : "已关闭自助注册。")} />
            <span>
              <b>允许学生自助注册</b>
              <em>关闭后只有管理员预建的账号能登录；已注册的账号不受影响。</em>
            </span>
          </label>

          <label className="sw-row">
            <input type="checkbox" checked={s.registration_review}
                   onChange={(e) => patch({ registration_review: e.target.checked },
                     e.target.checked
                       ? "已开启审核：新注册账号需你在下方通过后才能登录。"
                       : "已关闭审核：注册后可立即登录使用。")} />
            <span>
              <b>注册需要管理员审核</b>
              <em>
                开启后：学生选完学院提交 → 账号进入待审状态（不能登录）→ 你在下方通过后才可用。
                关闭后：选完学院立即可以使用。
              </em>
            </span>
          </label>

          <div className="sw-row">
            <span>
              <b>邀请码</b>
              <em>留空表示不需要邀请码。填写后学生注册必须带对。</em>
            </span>
            <div className="row" style={{ marginLeft: "auto" }}>
              <input
                type="text" placeholder={s.registration_code ? "已设置（留空则清除）" : "未设置"}
                value={codeDraft ?? ""}
                onChange={(e) => setCodeDraft(e.target.value)}
                style={{ minWidth: 200 }}
              />
              <button className="btn btn-sm" disabled={codeDraft === null}
                      onClick={() => { void patch({ registration_code: codeDraft ?? "" }, "邀请码已更新。"); setCodeDraft(null); }}>
                保存
              </button>
            </div>
          </div>

          {/* 自助注册的角色不再是可选项：学生就是学生。
              以前这里能在 student / entrant 之间切，而两者在"能不能参赛"上的差别
              已经取消了 —— 参赛只看有没有绑定学院。留着这个开关只会让人以为
              "选错了就上不了场"，所以整段删掉，改为说明当前口径。 */}
          <div className="sw-row">
            <span>
              <b>自助注册账号的角色</b>
              <em>
                自助注册一律是学生。学生绑定学院后即可代表本院参加正式比赛，不需要额外加资格；
                未绑定学院的账号仍可练习与上排行榜，需要时由你在「账号与角色」里补上学院。
              </em>
            </span>
          </div>
        </div>
      )}

      {/* ── 待审队列 ── */}
      <h3 className="subsection-title mt-6">待审核注册</h3>
      {(pending.data?.total ?? 0) === 0 ? (
        <p className="note mt-3">
          当前没有待审核的注册。
          {!s?.registration_review && "（注册审核当前是关闭的，新注册会直接可用。）"}
        </p>
      ) : (
        <table className="table mt-4">
          <caption className="sr-only">待审核的注册账号</caption>
          <thead>
            <tr>
              <th scope="col">用户名</th><th scope="col">显示名</th>
              <th scope="col">申请学院</th><th scope="col">提交时间</th><th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {pending.data!.items.map((u) => (
              <tr key={u.id}>
                <td>{u.username}</td>
                <td>{u.display_name ?? "—"}</td>
                <td>{u.house?.name_cn ?? "—"}</td>
                <td className="num">{u.created_at?.slice(0, 16).replace("T", " ")}</td>
                <td>
                  <div className="row">
                    <button className="btn btn-sm btn-primary" onClick={() => review(u.id, true)}>通过</button>
                    <button className="btn btn-sm" onClick={() => review(u.id, false)}>拒绝</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function MatchesTab() {
  const qc = useQueryClient();
  const matches = useQuery({ queryKey: ["admin-matches"], queryFn: () => api.get<{ items: MatchItem[] }>("/api/v1/admin/matches"), refetchInterval: 5000 });
  const [sel, setSel] = useState<string | null>(null);
  const detail = useQuery({ queryKey: ["admin-match", sel], enabled: !!sel, queryFn: () => api.get<MatchDetail>(`/api/v1/admin/matches/${sel}`), refetchInterval: 5000 });
  const [cmdErr, setCmdErr] = useState<string | null>(null);
  const [cmdNote, setCmdNote] = useState<string | null>(null);
  const [origin, setOrigin] = useState<DOMRect | null>(null);
  const [form, setForm] = useState({ title: "[演示] 新 House 赛", mode: "fixed_k", fixed_k: 2, planned_questions: 4, display_seconds: 0, answer_seconds: 20, interlude_seconds: 5, aid_enabled: true, spectator_mode: "mirror", scoring_method: "avg", scoring_mode: "composite", weight_accuracy: 700, weight_speed: 300, weight_participation: 0,
    // 题库大类：official 竞赛题库（默认）/ practice 日常题库。与小类（主题题量）两级一起决定抽哪些题。
    question_pool: "official" as "official" | "practice",
    // 小类配额：主题 → 题量。空对象 = 不传 blueprint，后端按随机（或难度配比）抽题。
    // 原来是自由文本框「科学常识:2,生活与安全:1」，主题名打错要到点建赛才知道；
    // 改成勾选后，可用量与合计在现场就能看见（见 SubjectQuota 的说明）。
    blueprintMap: {} as Record<string, number>,
    // 赛制：抢答（一次竞赛一个大题库、四院同题）或接力（每院固定题目数量、比总用时）
    competition_mode: "shared_buzz", questions_per_house: 5, weight_time: 300 });
  const [confirming, setConfirming] = useState<{ action: string; payload: Record<string, unknown> } | null>(null);
  // 揭晓排期：默认「现在 +30 秒开始仪式、+60 秒揭晓」，主机只需要微调
  const [revealAt, setRevealAt] = useState(() => new Date(Date.now() + 60000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16));
  // 赛后更正：逐个勾选要剔除的人 + 填理由（过去的做法是让主机手写 JSON 用户名，现场几乎必错）
  const [excl, setExcl] = useState<Record<string, string>>({});
  const consoleRef = useRef<HTMLDivElement>(null);

  const pl = detail.data;
  const st = pl?.match.status;

  /* 点「管理」之后**必须把控制台滚进视野**。
     为什么：控制台渲染在 50 行的比赛列表**下面**，实测它的 top 在 3849px，
     而视口只有 900px —— 点完按钮页面纹丝不动，看起来就是"管理按钮没反应"。
     同一件事也是"建完赛只看到草稿、不知道下一步点哪"的成因：
     新赛的草稿确实建好了、控制台也确实渲染了，只是永远在屏幕外面。

     为什么要一次性判断（不能每次 pl 变化都滚）：
       · 依赖里必须有 `pl` —— 点下去时详情还在请求中，控制台尚未挂载、
         `consoleRef.current` 是 null，只靠 `[sel]` 会**永远错过**这一跳；
       · 但详情每 5 秒轮询一次，`pl` 会不断换引用，
         不做一次性判断就会每 5 秒把管理员从页面下方硬拽回控制台。
     一次选择只滚一次，之后的刷新不再打扰。
     ⚠️ 但"只滚一次"按比赛来记是不够的（实测复现的第二个坑）：**再点已经在管理中的那一场，不会滚**。
     老实现只依赖 [sel, pl]，而再点同一场传进去的是同一个 id，
     React 对同值 setState 会跳过更新，effect 根本不重跑 ——
     现场表现是"我又点了一次管理，还是没反应"。
     所以改成显式的"跳转请求"：每次点击都记一次（计数器保证依赖一定变化），
     effect 消费掉请求后自己清标记。于是点几次都滚，而轮询仍不会把页面拽走。 */
  const pendingJump = useRef(false);
  const [jumpTick, setJumpTick] = useState(0);
  const selectMatch = (id: string) => {
    setSel(id);
    pendingJump.current = true;
    setJumpTick((n) => n + 1);   // 即使 sel 没变，也强制 effect 重跑
  };
  useEffect(() => {
    if (!pendingJump.current || !sel || !pl) return;
    // 等**这一场**的详情到位再滚：点新比赛时 pl 还停留在上一场，
    // 那时滚动会按旧内容的高度算落点，位置会偏。
    if (pl.match.id !== sel) return;
    const el = consoleRef.current;
    if (!el) return;
    pendingJump.current = false;
    el.scrollIntoView({ behavior: getLevel() === "reduced" ? "auto" : "smooth", block: "start" });
  }, [sel, pl, jumpTick]);

  /** 刷新后台数据。`mid` 用来指定"刚操作的那一场"：
   *  建赛/发命令之后 `sel` 这个 state 在本轮闭包里还是旧值，
   *  直接 `refresh()` 会把**上一场**的详情作废掉，而真正改动的那一场要等
   *  5 秒轮询才更新 —— 表现就是"刚点了创建并发布，控制台标题还写着草稿"。 */
  const refresh = (mid?: string | null) => {
    void qc.invalidateQueries({ queryKey: ["admin-match", mid ?? sel] });
    void qc.invalidateQueries({ queryKey: ["admin-matches"] });
  };

  /**
   * 发命令：**先取最新 state_version，再发**；撞上 409 只自动重试一次。
   *
   * 这里踩过坑：`expected_state_version` 取自 5 秒一刷的详情查询，而命令会让版本 +1，
   * 于是"刚点完一个按钮马上点下一个"必然拿旧版本 → 409「状态版本过期」。
   * 主机看到的是"操作失败"，其实只是自己的界面慢了一拍。
   */
  const execute = async (mid: string, act: string, payload: Record<string, unknown>, retry = true) => {
    setCmdErr(null); setCmdNote(null);
    try {
      const fresh = await api.get<MatchDetail>(`/api/v1/admin/matches/${mid}`);
      await api.post(`/api/v1/admin/matches/${mid}/commands`, {
        command_id: `web-${act}-${crypto.randomUUID().slice(0, 12)}`,
        action: act, expected_state_version: fresh.match.state_version, payload,
      });
      setCmdNote(`已执行「${ACTION_LABEL[act] ?? act}」。`);
      refresh(mid);
    } catch (e) {
      if (retry && e instanceof ApiError && e.code === "STATE_VERSION_CONFLICT") {
        await execute(mid, act, payload, false);   // 状态变了就按最新版本再试一次
        return;
      }
      setCmdErr(e instanceof Error ? e.message : "命令失败");
      refresh(mid);
    }
  };

  const requestOrExecute = (act: string, payload: Record<string, unknown>, e?: React.MouseEvent) => {
    if (NEEDS_CONFIRM.includes(act)) {
      if (e) setOrigin((e.currentTarget as HTMLElement).getBoundingClientRect());
      setConfirming({ action: act, payload });
      return;
    }
    void execute(sel!, act, payload);
  };

  const patchMatch = async (body: Record<string, unknown>) => {
    if (!sel) return;
    setCmdErr(null); setCmdNote(null);
    try {
      await api.patch(`/api/v1/admin/matches/${sel}`, body);
      setCmdNote("设置已保存。");
      refresh();
    } catch (e) {
      setCmdErr(e instanceof Error ? e.message : "保存失败");
    }
  };

  /**
   * 建赛。`publish` 决定建完之后停在哪一步 —— 这是管理员在表单底部**明确选一次**的：
   *   · false（保存为草稿）：只建，不动状态。适合"先把赛制、题量、评分口径摆好，
   *     临场再开"——比如赛前一周就把决赛配好，避免现场手忙脚乱。
   *   · true（创建并发布）：建完立刻 `open_lobby`，选手马上能签到。
   *     适合"现在就要用"的场景；原先只有草稿一条路，管理员建完还得自己滚到控制台
   *     再找「开启签到」，那正是"怎么只能存草稿"的由来。
   *
   * 为什么发布走 `open_lobby` 而不是把状态直接写成 running：
   *   签到是选手真正进场的唯一入口（entry 在这一步才冻结 House、分配匿名别名），
   *   跳过它就没有名单，题也发不出去。所以"发布"= 开放签到，再由主持人点「开始比赛」。
   *   两步都在现场由人按，比赛的开始时刻永远不会是"建赛那一刻"。
   */
  const createMatch = async (publish: boolean) => {
    setCmdErr(null); setCmdNote(null);
    try {
      /* 小类配额 → blueprint。
         一个主题都不勾时**不传 blueprint**（而不是传空对象）：传空对象会被后端判成
         "主题配额题源却没给蓝图"而报错，而管理员的意图显然是"随便抽几题"。
         `?? undefined` 同时让 JSON.stringify 把整个字段丢掉。 */
      const blueprint: Record<string, number> | undefined =
        Object.keys(form.blueprintMap).length ? { ...form.blueprintMap } : undefined;
      const body: Record<string, unknown> = {
        title: form.title, mode: form.mode, fixed_k: form.mode === "fixed_k" ? Number(form.fixed_k) : undefined,
        planned_questions: plannedTotal, blueprint,
        // 题库大类：先定从哪个池抽，再由 blueprint 定小类配额
        question_pool: form.question_pool,
        display_seconds: Number(form.display_seconds), answer_seconds: Number(form.answer_seconds),
        interlude_seconds: Number(form.interlude_seconds), aid_enabled: form.aid_enabled,
        spectator_mode: form.spectator_mode,
        // 赛制：抢答全场共用一份题库；接力按"每院题量 × 学院数"自动推导全场题量
        competition_mode: form.competition_mode,
        questions_per_house: form.competition_mode === "relay" ? Number(form.questions_per_house) : undefined,
        weight_time: form.competition_mode === "relay" && form.scoring_mode === "composite"
          ? Number(form.weight_time) : undefined,
        // 计分口径：赛前冻结，结果页会把它原文展示给学生
        scoring_method: form.scoring_method,
        scoring_mode: form.scoring_mode,
        weight_accuracy: form.scoring_mode === "composite" ? Number(form.weight_accuracy) : undefined,
        // 参与度是**显式开启**的第三维：默认 0，管理员勾选后才进入权重合成
        weight_participation: form.scoring_mode === "composite"
          ? Number(form.weight_participation) : undefined,
        weight_speed: form.competition_mode === "shared_buzz" && form.scoring_mode === "composite"
          ? Number(form.weight_speed) : undefined,
      };
      const r = await api.post<{ match_id: string }>("/api/v1/admin/matches", body);
      // 走 selectMatch 而不是 setSel：新建的赛也要把控制台滚进视野，
      // 否则"建完赛只看到草稿、不知道下一步点哪"这个老问题会从另一个入口回来。
      selectMatch(r.match_id);
      if (publish) {
        // 复用统一发命令的入口：它会先取最新 state_version 再发，撞 409 自动重试一次，
        // 并把「已执行…」/错误写进 cmdNote / cmdErr —— 这里不另起一套。
        await execute(r.match_id, "open_lobby", {});
        setCmdNote(`已创建并发布：「${form.title}」现在是签到中，选手可以进场签到了。`);
      } else {
        setCmdNote(`已保存为草稿：「${form.title}」。选手看不到它，等你开放签到才会出现。`);
      }
      refresh(r.match_id);
    } catch (e) {
      setCmdErr(e instanceof Error ? e.message : "创建失败");
    }
  };

  /** 主持台上真正会按的按钮，按赛程顺序排列；与状态不匹配的一律禁用而不是点了报错。 */
  const steps: Array<{ action: string; payload: Record<string, unknown> | (() => Record<string, unknown>); enabled: boolean; why: string }> = [
    { action: "open_lobby", payload: {}, enabled: st === "draft", why: "进入签到阶段，选手开始签到" },
    { action: "start_match", payload: {}, enabled: st === "lobby", why: "正式开赛，第一题立刻进入抢答" },
    { action: "pause", payload: {}, enabled: st === "running", why: "暂停：突发情况先停住，作答窗口随之冻结" },
    { action: "resume", payload: {}, enabled: st === "paused", why: "继续比赛" },
    { action: "lock", payload: {}, enabled: st === "running" || st === "paused", why: "结束作答，之后不能再收答案" },
    { action: "calculate", payload: {}, enabled: st === "locked", why: "结算成绩（生成待揭晓版本，不对外发布）" },
    { action: "schedule_reveal", payload: () => {
        // 输入框是本地时间，转成 UTC 的 Z 串再发（服务端只认 UTC）
        const t = new Date(revealAt).getTime();
        const iso = (ms: number) => new Date(ms).toISOString().slice(0, 19) + "Z";
        return { ceremony_start_at: iso(t - 30000), reveal_at: iso(t) };
      }, enabled: st === "calculated", why: "设定揭晓时刻，到点自动公布" },
    { action: "cancel", payload: () => ({ reason: "主持人取消" }), enabled: ["draft", "lobby", "running", "paused"].includes(st ?? ""), why: "取消本场比赛（不可恢复）" },
  ];

  const activeRoster = (pl?.roster ?? []).filter((x) => x.scoring_status === "active");
  // 计分口径只在开赛前可改（服务端同样会拒；这里先禁用，避免点了报错）
  const canEditScoring = st === "draft" || st === "lobby";

  /* 全场题数：抢答赛由管理员填，接力赛由"每院题量 × 4 院"推导 ——
     与后端 create_match 的算法一致。两处算法必须一样，否则小类配额的"已配平"
     会在服务端被判成不符。 */
  const plannedTotal = form.competition_mode === "relay"
    ? Math.max(1, Number(form.questions_per_house) || 0) * 4
    : Math.max(1, Number(form.planned_questions) || 0);

  /* 小类配额的成立条件：不选主题 = 随机抽题（成立）；选了就必须合计等于全场题数。 */
  const bpKeys = Object.keys(form.blueprintMap);
  const bpSum = bpKeys.reduce((s, k) => s + (Number(form.blueprintMap[k]) || 0), 0);
  const quotaOk = bpKeys.length === 0 || bpSum === plannedTotal;
  const quotaWhy = bpKeys.length === 0
    ? ""
    : (bpSum < plannedTotal
      ? `小类配额还差 ${plannedTotal - bpSum} 题（已分配 ${bpSum}，本场共 ${plannedTotal} 题）。点上方的「平均分配」可以一键配平，或清空全部小类改为随机抽题。`
      : `小类配额多了 ${bpSum - plannedTotal} 题（已分配 ${bpSum}，本场共 ${plannedTotal} 题）。请调小某个主题的题量，或点「平均分配」。`);

  return (
    <>
      <div className="card">
        <h3>建新赛（先选题库大类，再按小类分配题量；系统自动预留 20% 封存备用题）</h3>

        {/* 建赛表单按"先定赛制 → 再定规模 → 最后定口径"分成三组带标题的字段。
            原来是一整行塞十几个控件，现场主持要在里面找输入框，很容易改错一个数。
            分组之后每一组只回答一个问题，改错了也看得出是哪一组。 */}
        <fieldset className="mk-group">
          <legend>一、这场怎么打</legend>
          <div className="mk-grid">
            <label className="field mk-wide">
              <span>赛制</span>
              <Select ariaLabel="赛制" value={form.competition_mode}
                      onChange={(v) => setForm({ ...form, competition_mode: v })}
                      options={[
                        { value: "shared_buzz", label: "抢答赛", hint: "一次竞赛一个大题库，四院同题抢答" },
                        { value: "relay", label: "接力赛", hint: "每院固定题目数量，比总用时与每题用时" },
                      ]} />
            </label>
            <label className="field">
              <span>{form.competition_mode === "relay" ? "每院题量" : "全场题数"}</span>
              {form.competition_mode === "relay" ? (
                <input aria-label="每院题量" type="number" min={1} max={60}
                       value={form.questions_per_house}
                       onChange={(e) => setForm({ ...form, questions_per_house: Number(e.target.value) })} />
              ) : (
                <input aria-label="全场题数" type="number" min={1} max={100}
                       value={form.planned_questions}
                       onChange={(e) => setForm({ ...form, planned_questions: Number(e.target.value) })} />
              )}
              <em className="mk-hint">
                {form.competition_mode === "relay"
                  ? `全场共 ${Number(form.questions_per_house) * 4} 题（每院题量 × 4 院），各院并行作答`
                  : `全场共用这 ${form.planned_questions} 题，四院抢答同一道题`}
              </em>
            </label>
            <label className="field">
              <span>读题秒（0 = 看到题就能答）</span>
              <input aria-label="读题秒" type="number" min={0} value={form.display_seconds}
                     onChange={(e) => setForm({ ...form, display_seconds: Number(e.target.value) })} />
              <em className="mk-hint">
                {form.display_seconds === 0 ? "当前是抢答：题目一出现即可作答" : "当前是先读题后作答"}
              </em>
            </label>
            <label className="field">
              <span>每题作答秒</span>
              <input aria-label="作答秒" type="number" min={1} value={form.answer_seconds}
                     onChange={(e) => setForm({ ...form, answer_seconds: Number(e.target.value) })} />
            </label>
            <label className="field">
              <span>两题之间间隔秒</span>
              <input aria-label="题间隔" type="number" min={0} value={form.interlude_seconds}
                     onChange={(e) => setForm({ ...form, interlude_seconds: Number(e.target.value) })} />
            </label>
          </div>
        </fieldset>

        <fieldset className="mk-group">
          <legend>二、题目从哪来</legend>
          <div className="mk-grid">
            {/* 两级选择：先选**大类**（哪个题库），再选**小类**（哪些主题、各几题）。
                为什么大类要放在这里而不是藏进默认值：竞赛题库与日常题库都能办赛，
                默认是竞赛题库；切到日常题库时必须在眼皮底下说清后果。 */}
            <label className="field">
              <span>题库（大类）</span>
              <Select ariaLabel="题库大类" value={form.question_pool}
                      onChange={(v) => setForm({ ...form, question_pool: v as "official" | "practice" })}
                      options={[
                        { value: "official", label: "竞赛题库", hint: "正式比赛专用题，学生练习看不到" },
                        { value: "practice", label: "日常题库", hint: "日常练习题池，学生可能已经练过" },
                      ]} />
              <em className="mk-hint">
                {form.question_pool === "official"
                  ? "从竞赛题库抽题；学生日常刷题看不到这里的题。"
                  : "从日常题库抽题：这些题学生平时就能练到，本场成绩的区分度会下降，适合院内小型赛。"}
              </em>
            </label>
            <SubjectQuota pool={form.question_pool} planned={plannedTotal}
                          value={form.blueprintMap}
                          onChange={(v) => setForm({ ...form, blueprintMap: v })} />
            <label className="field">
              <span>比赛标题</span>
              <input aria-label="比赛标题" value={form.title}
                     onChange={(e) => setForm({ ...form, title: e.target.value })} />
            </label>
            <label className="field">
              <span>席位模式</span>
              <Select ariaLabel="席位模式" value={form.mode}
                      onChange={(v) => setForm({ ...form, mode: v })}
                      options={[
                        { value: "fixed_k", label: "固定席位", hint: "每院取前 K 名计入成绩" },
                        { value: "open", label: "不限人数", hint: "本院所有签到的人一起算" },
                      ]} />
            </label>
            <label className="field">
              <span>{form.mode === "fixed_k" ? "每院席位 K" : "开赛最低人数"}</span>
              <input aria-label="K 或最低人数" type="number" min={1} value={form.fixed_k}
                     onChange={(e) => setForm({ ...form, fixed_k: Number(e.target.value) })} />
            </label>
          </div>
        </fieldset>

        <fieldset className="mk-group">
          <legend>三、怎么算分、怎么观战</legend>
          <div className="mk-grid">
            <label className="field">
              <span>评分方式</span>
              <Select ariaLabel="评分方式" value={form.scoring_mode}
                      onChange={(v) => setForm({ ...form, scoring_mode: v })}
                      options={[
                        { value: "pure", label: "只看正确率", hint: "不把速度算进得分" },
                        { value: "composite", label: "综合评估", hint: "正确率 + 抢答速度按权重合成" },
                      ]} />
            </label>
            {form.scoring_mode === "pure" ? (
              <label className="field mk-wide">
                <span>正确率口径</span>
                <Select ariaLabel="正确率口径" value={form.scoring_method}
                        onChange={(v) => setForm({ ...form, scoring_method: v })}
                        options={[
                          { value: "avg", label: "人均平均", hint: "学院成员个人分平均，人多人少一样重" },
                          { value: "total", label: "总分累加", hint: "人多占优" },
                          { value: "top_n", label: "前 N 名平均", hint: "抑制人海" },
                          { value: "accuracy", label: "作答正确率", hint: "不作答不计入" },
                        ]} />
              </label>
            ) : null}
            <label className="field mk-wide">
              <span>观战档位（观众能看到什么）</span>
              <Select ariaLabel="观战档位" value={form.spectator_mode}
                      onChange={(v) => setForm({ ...form, spectator_mode: v })}
                      options={[
                        { value: "mirror", label: "同屏镜像", hint: "看到与选手相同的题目画面（不含答案）" },
                        { value: "blur", label: "题目模糊", hint: "只看得到有人在作答" },
                        { value: "summary", label: "仅本院进度", hint: "完全不显示题目" },
                      ]} />
            </label>
            <label className="field">
              <span>场外援助</span>
              <Select ariaLabel="启用援助" value={form.aid_enabled ? "1" : "0"}
                      onChange={(v) => setForm({ ...form, aid_enabled: v === "1" })}
                      options={[
                        { value: "1", label: "启用", hint: "智囊可在求援窗口给出建议" },
                        { value: "0", label: "关闭" },
                      ]} />
            </label>
          </div>
        </fieldset>

        {/* 综合评估的权重：只在选了综合评估时出现。
            权重之和不必是 100 —— 后端会按总和归一化，界面上直接显示折算后的百分比，
            避免管理员为了"凑够 100"去反复拖滑块。 */}
        {form.scoring_mode === "composite" && (
          <fieldset className="mk-group">
            <legend>四、综合评估的权重</legend>
            <div className="mk-grid">
              <label className="field mk-wide">
                <span>正确率权重</span>
                <input aria-label="正确率权重" type="range" min={0} max={1000} step={50} value={form.weight_accuracy}
                  onChange={(e) => setForm({
                    ...form, weight_accuracy: Number(e.target.value),
                    ...(form.competition_mode === "relay"
                      ? { weight_time: 1000 - Number(e.target.value) }
                      : { weight_speed: 1000 - Number(e.target.value) }),
                  })} />
                <em className="mk-hint">当前 {Math.round(form.weight_accuracy / 10)}%</em>
              </label>
              {form.competition_mode === "relay" ? (
                <label className="field mk-wide">
                  <span>用时权重</span>
                  <input aria-label="用时权重" type="range" min={0} max={1000} step={50} value={form.weight_time}
                    onChange={(e) => setForm({ ...form, weight_time: Number(e.target.value) })} />
                  <em className="mk-hint">
                    当前 {Math.round(form.weight_time / 10)}%（总用时 + 每题用时）
                  </em>
                </label>
              ) : (
                <label className="field mk-wide">
                  <span>抢答速度权重</span>
                  <input aria-label="速度权重" type="range" min={0} max={1000} step={50} value={form.weight_speed}
                    onChange={(e) => setForm({ ...form, weight_speed: Number(e.target.value) })} />
                  <em className="mk-hint">当前 {Math.round(form.weight_speed / 10)}%</em>
                </label>
              )}
              {/* 参与度是可选维度：默认 0。启用后按"本院实际上场人数 ÷ 冻结名单人数"加权，
                  适合"希望全员上场"的场次；不启用则完全不参与计分（不会悄悄影响名次）。 */}
              <label className="field mk-wide">
                <span>参与度权重（0 = 不计入）</span>
                <input aria-label="参与度权重" type="range" min={0} max={1000} step={50}
                  value={form.weight_participation}
                  onChange={(e) => setForm({ ...form, weight_participation: Number(e.target.value) })} />
                <em className="mk-hint">
                  当前 {Math.round(form.weight_participation / 10)}%（本院实际上场人数占冻结名单的比例）
                </em>
              </label>
            </div>
          </fieldset>
        )}

        <div className="mk-submit">
          <button className="btn btn-primary" onClick={() => void createMatch(true)}
                  disabled={!quotaOk} title={quotaOk ? undefined : quotaWhy}>
            创建并发布（开放签到）
          </button>
          <button className="btn" onClick={() => void createMatch(false)}
                  disabled={!quotaOk} title={quotaOk ? undefined : quotaWhy}>
            仅保存为草稿
          </button>
          {/* 配额不成立时**禁用并写明原因**，而不是让管理员点了再收服务端的 422：
              服务端那句话（"配额合计 3 题，与计划 4 题不符"）虽然准确，但现场要的是
              "我该改哪个框"，所以这里给出可执行的下一步。 */}
          {!quotaOk && <span className="sq-warn" role="alert">{quotaWhy}</span>}
          <span className="mk-hint">
            <b>发布</b>：立刻开放签到，选手能进场签到；正式开赛仍由你在控制台按「开始比赛」，
            比赛的开始时刻永远由主持人掌握。<br />
            <b>草稿</b>：只把这场的配置存下来，选手看不到；适合赛前先把决赛配好，临场再开放。
            <br />
            {form.scoring_mode === "pure"
              ? "只看正确率：答得慢但答得准不吃亏。"
              : `综合评估：正确率 ${Math.round(form.weight_accuracy / 10)}%`
                + (form.competition_mode === "relay"
                  ? ` + 用时 ${Math.round(form.weight_time / 10)}%`
                  : ` + 抢答速度 ${Math.round(form.weight_speed / 10)}%`)
                + (form.weight_participation > 0
                  ? ` + 参与度 ${Math.round(form.weight_participation / 10)}%` : "")
                + "。"}
            计分口径建赛后可改到「开启签到」为止，开赛后锁定（保证各院可比）。
          </span>
        </div>
      </div>

      <div className="card">
        <h3>比赛列表</h3>
        <table className="table">
          <thead><tr><th>标题</th><th>赛制</th><th>模式</th><th>状态</th><th>版本</th><th></th></tr></thead>
          <tbody>
            {(matches.data?.items ?? []).map((m) => (
              <tr key={m.id} className={sel === m.id ? "row-sel" : undefined}>
                <td>{m.title}</td>
                <td>{m.competition_mode === "relay" ? "接力" : "抢答"}</td>
                <td>{m.mode}{m.fixed_k ? ` K=${m.fixed_k}` : ""}</td><td>{STATUS_LABEL[m.status] ?? m.status}</td><td className="num">v{m.state_version}</td>
                <td>
                  <button className="btn btn-sm" onClick={() => selectMatch(m.id)}
                          aria-expanded={sel === m.id}>
                    {sel === m.id ? "正在管理" : "管理"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {sel && pl && (
        <div className="card" ref={consoleRef}>
          <h3>控制台 · {pl.match.title}（{STATUS_LABEL[pl.match.status] ?? pl.match.status} · v{pl.match.state_version}）</h3>
          {/* 本场用的是哪个题库：日常题库也能办赛，现场必须能一眼确认，
              而不是事后从题目内容去猜。题库由题单反推（后端 question_pool）。 */}
          {pl.match.question_pool_label && (
            <p className="mk-hint" data-match-pool={pl.match.question_pool}>
              题单来自：<b>{pl.match.question_pool_label}</b>
              {pl.match.question_pool === "practice"
                ? "（日常题库）—— 这些题学生平时能练到，本场区分度会低于竞赛题库。"
                : "（正式比赛专用题，学生日常练习看不到）。"}
            </p>
          )}
          {cmdErr && <div className="error-box">{cmdErr}</div>}
          {cmdNote && <div className="ok-box">{cmdNote}</div>}

          {/* 赛程步骤：现场只按顺序点，不需要懂 action 名字 */}
          <div className="row" style={{ marginBottom: 12 }}>
            {steps.map((s) => (
              <button key={s.action} className="btn"
                disabled={!s.enabled}
                title={s.enabled ? s.why : `当前阶段不可用：${s.why}`}
                style={{ padding: "7px 16px", fontSize: 13, opacity: s.enabled ? 1 : .45 }}
                onClick={(e) => requestOrExecute(s.action, typeof s.payload === "function" ? s.payload() : s.payload, e)}>
                {ACTION_LABEL[s.action] ?? s.action}
              </button>
            ))}
            <a className="btn" href={`/matches/${sel}/screen`}>投屏</a>
            <a className="btn" href={`/matches/${sel}/watch`}>镜像回放 / 观战</a>
            <a className="btn" href={`/matches/${sel}/results`} target="_blank" rel="noreferrer">结果页</a>
            <button className="btn" onClick={async () => {
              try {
                const r = await api.get<ResultsData>(`/api/v1/admin/matches/${sel}/preview-results`);
                setCmdErr(null);
                setCmdNote(`预览（未公开）：${r.houses.map((h) => `${h.name} ${h.score}分${h.is_tie ? "(并列)" : ""}`).join(" · ") || "暂无成绩"} | 正式排名条件：${r.is_official ? "满足" : "不满足"}${r.notes ? `（${r.notes}）` : ""}`);
              } catch (e) {
                setCmdNote(null);
                setCmdErr(e instanceof Error ? e.message : "尚无成绩版本");
              }
            }}>预览成绩</button>
          </div>

          {/* 揭晓时刻：主机只需要挑一个时间 */}
          {["calculated"].includes(pl.match.status) && (
            <div className="row" style={{ marginBottom: 12 }}>
              <label style={{ fontSize: 13, color: "var(--ink-2)" }}>揭晓时刻（本机时间）
                <input type="datetime-local" value={revealAt} onChange={(e) => setRevealAt(e.target.value)} />
              </label>
              <button className="btn btn-primary" style={{ padding: "7px 16px", fontSize: 13 }}
                onClick={(e) => requestOrExecute("schedule_reveal", (steps.find((s) => s.action === "schedule_reveal")!.payload as () => Record<string, unknown>)(), e)}>
                排期揭晓
              </button>
              <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>到点由服务端自动公布，不依赖前台页面开着。</span>
            </div>
          )}

          {/* 赛后更正：勾人 + 填理由，替代过去手写 JSON 用户名 */}
          {pl.match.status === "calculated" || pl.match.status === "reveal_scheduled" ? (
            <div className="card" style={{ background: "var(--bg-page)", marginBottom: 12 }}>
              <h3 style={{ fontSize: 15 }}>赛后更正（剔除无效/违规答卷）</h3>
              <p style={{ fontSize: 13, color: "var(--ink-2)", margin: "4px 0 10px" }}>
                勾选要剔除的选手并写明理由，系统会生成新一版成绩并留下审计记录；四院适用同一规则。
              </p>
              {activeRoster.length === 0 && <div style={{ fontSize: 13, color: "var(--ink-2)" }}>本场暂无已签到的选手。</div>}
              {activeRoster.map((x) => (
                <div key={x.entry_id} className="row" style={{ marginBottom: 6 }}>
                  <label style={{ fontSize: 13, minWidth: 220 }}>
                    <input type="checkbox" checked={!!excl[x.username]}
                      onChange={(e) => setExcl((p) => { const n = { ...p }; if (e.target.checked) n[x.username] = ""; else delete n[x.username]; return n; })} />
                    {" "}{x.alias || x.username}（{x.house ?? "未绑定"}）
                  </label>
                  {excl[x.username] !== undefined && (
                    <input placeholder="剔除理由（必填，会写进结果说明）" style={{ flex: 1, minWidth: 220, background: "var(--bg-card)", border: "1px solid var(--line-2)", borderRadius: 8, color: "var(--ink-1)", padding: "7px 10px" }}
                      value={excl[x.username]} onChange={(e) => setExcl((p) => ({ ...p, [x.username]: e.target.value }))} />
                  )}
                </div>
              ))}
              <button className="btn btn-primary" style={{ marginTop: 6 }}
                onClick={(e) => {
                  const picked = Object.entries(excl).filter(([, r]) => r.trim());
                  if (!picked.length) { setCmdErr("请至少勾选一人并填写剔除理由。"); return; }
                  requestOrExecute("recalculate", { exclusions: Object.fromEntries(picked), note: "赛后更正" }, e);
                }}>生成更正版本</button>
            </div>
          ) : null}

          {confirming && (
            <OriginModal rect={origin} onClose={() => setConfirming(null)}>
              <h3>确认执行「{ACTION_LABEL[confirming.action] ?? confirming.action}」？</h3>
              <p style={{ fontSize: 13.5, color: "var(--ink-2)", margin: "8px 0 16px" }}>
                {steps.find((s) => s.action === confirming.action)?.why ?? ""}
                该操作会在服务器事务内完成状态变更与版本递增，并写入审计日志。
              </p>
              <div className="row">
                <button className="btn btn-primary" onClick={() => { const c = confirming; setConfirming(null); void execute(sel, c.action, c.payload); }}>确认执行</button>
                <button className="btn" onClick={() => setConfirming(null)}>取消</button>
              </div>
            </OriginModal>
          )}

          <h3>观战与参赛范围</h3>
          <div className="row" style={{ marginBottom: 12, alignItems: "flex-end" }}>
            <label className="field">
              <span>观战档位</span>
              <Select ariaLabel="观战档位" size="sm" minWidth={150}
                      value={pl.match.spectator_mode}
                      onChange={(v) => void patchMatch({ spectator_mode: v })}
                      options={[
                        { value: "mirror", label: "同屏镜像", hint: "看到与选手相同的题目画面（不含答案）" },
                        { value: "blur", label: "题目模糊", hint: "只看得到有人在作答" },
                        { value: "summary", label: "仅本院进度", hint: "完全不显示题目" },
                      ]} />
            </label>
            <label className="field">
              <span>场外援助</span>
              <Select ariaLabel="援助开关" size="sm" minWidth={110}
                      value={pl.match.aid_enabled ? "1" : "0"}
                      onChange={(v) => void patchMatch({ aid_enabled: v === "1" })}
                      options={[
                        { value: "1", label: "启用", hint: "智囊可在求援窗口给出建议" },
                        { value: "0", label: "关闭" },
                      ]} />
            </label>
            <span style={{ fontSize: 13, color: "var(--ink-2)", paddingBottom: 8 }}>
              参赛学院范围：{pl.match.houses_scope ? pl.match.houses_scope.join("、") : "全部四院"}
              （范围只能在开赛前调整）
            </span>
          </div>

          <h3>赛制与评分方式</h3>
          <div className="row" style={{ marginBottom: 8, alignItems: "flex-end" }}>
            <span style={{ fontSize: 13, color: "var(--ink-1)", paddingBottom: 8 }}>
              <b>{pl.match.competition_mode === "relay" ? "接力赛" : "抢答赛"}</b>
              {pl.match.competition_mode === "relay"
                ? ` · 每院 ${pl.match.questions_per_house ?? "?"} 题 · 全场 ${pl.match.planned_questions} 题 · 各院并行`
                : ` · 全场共用 ${pl.match.planned_questions} 题`}
              <br />
              <span style={{ color: pl.match.buzz || pl.match.competition_mode === "relay" ? "var(--state-ok)" : "var(--ink-2)" }}>
                {pl.match.competition_mode === "relay"
                  ? "看到题就能答（接力按院并行）"
                  : pl.match.buzz ? "看到题就能答" : `先读题 ${pl.match.display_seconds} 秒，再作答`}
              </span>
            </span>
            <label className="field">
              <span>评分方式</span>
              <Select ariaLabel="评分方式" size="sm" minWidth={180}
                      disabled={!canEditScoring}
                      value={pl.match.scoring_mode}
                      onChange={(v) => void patchMatch({ scoring_mode: v })}
                      options={[
                        { value: "pure", label: "只看正确率", hint: "不把速度算进得分" },
                        { value: "composite", label: "综合评估", hint: `正确率 + ${pl.match.competition_mode === "relay" ? "用时" : "抢答速度"}` },
                      ]} />
            </label>
            {pl.match.scoring_mode === "composite" ? (
              <>
                <label className="field">
                  <span>正确率权重</span>
                  <input aria-label="正确率权重" type="range" min={0} max={1000} step={50}
                    disabled={!canEditScoring}
                    value={pl.match.weight_accuracy}
                    onChange={(e) => void patchMatch({ weight_accuracy: Number(e.target.value) })} />
                  <em className="mk-hint">当前 {Math.round(pl.match.weight_accuracy / 10)}%</em>
                </label>
                <label className="field">
                  <span>{pl.match.competition_mode === "relay" ? "用时" : "速度"}权重</span>
                  <input aria-label="速度权重" type="range" min={0} max={1000} step={50}
                    disabled={!canEditScoring}
                    value={pl.match.competition_mode === "relay" ? pl.match.weight_time : pl.match.weight_speed}
                    onChange={(e) => void patchMatch(pl.match.competition_mode === "relay"
                      ? { weight_time: Number(e.target.value) } : { weight_speed: Number(e.target.value) })} />
                  <em className="mk-hint">
                    当前 {Math.round((pl.match.competition_mode === "relay" ? pl.match.weight_time : pl.match.weight_speed) / 10)}%
                  </em>
                </label>
                <label className="field">
                  <span>参与度权重</span>
                  <input aria-label="参与度权重" type="range" min={0} max={1000} step={50}
                    disabled={!canEditScoring}
                    value={pl.match.weight_participation}
                    onChange={(e) => void patchMatch({ weight_participation: Number(e.target.value) })} />
                  <em className="mk-hint">
                    当前 {Math.round(pl.match.weight_participation / 10)}%（0 = 不计入）
                  </em>
                </label>
              </>
            ) : (
              <label className="field">
                <span>正确率口径</span>
                <Select ariaLabel="正确率口径" size="sm" minWidth={160}
                        disabled={!canEditScoring}
                        value={pl.match.scoring_method}
                        onChange={(v) => void patchMatch({ scoring_method: v })}
                        options={[
                          { value: "avg", label: "人均平均", hint: "学院成员个人分平均" },
                          { value: "total", label: "总分累加", hint: "人多占优" },
                          { value: "top_n", label: "前 N 名平均", hint: "抑制人海" },
                          { value: "accuracy", label: "作答正确率", hint: "不作答不计入" },
                        ]} />
              </label>
            )}
            {!canEditScoring && (
              <span style={{ fontSize: 13, color: "var(--state-warn)", paddingBottom: 8 }}>
                已开赛：计分口径锁定（改动会让已打过的回合与新规则不一致，四院不可比）
              </span>
            )}
          </div>
          <p style={{ fontSize: 12.5, color: "var(--ink-2)", marginBottom: 12 }}>
            学生看到的说明：{pl.match.scoring_note}
          </p>

          <h3>名单（{pl.roster.length}）</h3>
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>别名</th><th>用户名</th><th>House</th><th>队长</th><th>签到</th><th>计分</th></tr></thead>
              <tbody>
                {pl.roster.map((e) => (
                  <tr key={e.entry_id}>
                    <td className="mono">{e.alias || "—"}</td><td>{e.username}</td><td>{e.house || "—"}</td>
                    <td>{e.is_captain ? "是" : "—"}</td><td>{e.checked_in ? "已签到" : "未签到"}</td>
                    <td>{e.scoring_status}{e.exclusion_reason ? `（${e.exclusion_reason}）` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            <span style={{ fontSize: 13, color: "var(--ink-2)" }}>援助额度：{pl.aid.map((a) => `${a.house} 剩 ${a.remaining}`).join(" · ") || "未启用"}</span>
            <span style={{ fontSize: 13, color: "var(--ink-2)" }}>回合：{pl.rounds.length}</span>
          </div>
        </div>
      )}
    </>
  );
}


/** 角色现在**只加能力、不拦参赛**：绑定了学院的学生就能代表本院上场，
 *  不存在"练习号 / 参赛号"的区别，因此这里只有三种角色，且都写清能做什么。 */
const ROLE_OPTIONS = [
  { value: "student", label: "学生", hint: "练习、上排行榜，并可代表本院参加正式比赛" },
  { value: "advisor", label: "智囊", hint: "在比赛求援窗口为本院提供建议（必须绑定学院）" },
  { value: "admin", label: "管理员", hint: "管理后台、主持比赛、审核题库" },
];
const ROLE_LABEL: Record<string, string> = Object.fromEntries(ROLE_OPTIONS.map((r) => [r.value, r.label]));
const HOUSE_OPTIONS = [
  { value: "tiger", label: "白虎" }, { value: "dragon", label: "青龙" },
  { value: "tortoise", label: "玄武" }, { value: "phoenix", label: "朱雀" },
];
const HOUSE_LABEL: Record<string, string> = Object.fromEntries(HOUSE_OPTIONS.map((h) => [h.value, h.label]));

function UsersTab() {
  const qc = useQueryClient();
  const [role, setRole] = useState("");
  const [house, setHouse] = useState("");
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [form, setForm] = useState({ username: "", password: "", role: "student", house_key: "tiger" });
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const users = useQuery({
    queryKey: ["admin-users", role, q],
    queryFn: () => {
      const p = new URLSearchParams({ size: "200" });
      if (role) p.set("role", role);
      if (q) p.set("q", q);
      return api.get<{ items: UserItem[] }>(`/api/v1/admin/users?${p}`);
    },
  });

  const act = async (fn: () => Promise<unknown>, note: string) => {
    setMsg(null); setErr(null);
    try { await fn(); setMsg(note); void qc.invalidateQueries({ queryKey: ["admin-users"] }); }
    catch (e) { setErr(e instanceof ApiError ? e.message : "操作失败。"); }
  };

  const create = () => act(
    () => api.post("/api/v1/admin/users", { ...form, must_change_password: true }),
    `已创建 ${form.username}。首次登录需要设置自己的密码。`);

  const setUserRole = (id: string, username: string, r: string) => act(
    () => api.patch(`/api/v1/admin/users/${id}`, { role: r }),
    `已把 ${username} 设为${ROLE_LABEL[r] ?? r}。`);

  const setUserHouse = (id: string, username: string, h: string) => act(
    () => api.post(`/api/v1/admin/users/${id}/house`, { house_key: h, reason: "管理员调整归属" }),
    `已把 ${username} 的学院改为${HOUSE_LABEL[h] ?? h}。`);

  const ban = (id: string, username: string, on: boolean) => act(
    () => api.post(`/api/v1/admin/users/${id}/ban`, { banned: on, reason: on ? "管理员封禁" : "管理员解封" }),
    on ? `已封禁 ${username}，其会话同时失效。` : `已解封 ${username}。`);

  const revoke = (id: string, username: string) => act(
    () => api.post(`/api/v1/admin/users/${id}/revoke-session`),
    `已吊销 ${username} 的活跃会话。`);

  const items = users.data?.items ?? [];
  // 学院筛选在本地做：账号量在千级以内，多一次往返不如让筛选立刻生效
  const shown = house ? items.filter((u) => u.house?.key === house) : items;

  return (
    <>
      <div className="card">
        <h3>创建账号</h3>
        <p className="note">
          学生只要绑定了学院，就可以代表本院参加正式比赛，不需要额外加资格。
          没有绑定学院的学生仍可练习与上排行榜，等要参赛时再补。
        </p>
        <div className="u-create">
          <label className="field"><span>用户名</span>
            <input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
          </label>
          <label className="field"><span>初始密码（至少 8 位）</span>
            <input type="text" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </label>
          <label className="field"><span>角色</span>
            <Select ariaLabel="新账号角色" value={form.role} minWidth={170}
                    onChange={(v) => setForm({ ...form, role: v })} options={ROLE_OPTIONS} />
          </label>
          <label className="field"><span>学院</span>
            <Select ariaLabel="新账号学院" value={form.house_key} minWidth={140}
                    onChange={(v) => setForm({ ...form, house_key: v })} options={HOUSE_OPTIONS} />
          </label>
          <button className="btn btn-primary" disabled={!form.username || form.password.length < 8}
                  onClick={create}>创建</button>
        </div>
        <p className="note">
          初始密码是明文显示的：它只用于第一次登录，学生登录后必须改成自己的密码。
        </p>
      </div>

      <div className="card">
        <div className="q-toolbar">
          <form className="q-search" onSubmit={(e) => { e.preventDefault(); setQ(search.trim()); }}>
            <input value={search} onChange={(e) => setSearch(e.target.value)}
                   placeholder="按用户名搜索…" aria-label="搜索账号" />
            <button className="btn btn-sm btn-primary" type="submit">搜索</button>
            {q && <button className="btn btn-sm" type="button" onClick={() => { setSearch(""); setQ(""); }}>清除</button>}
          </form>
          <Select ariaLabel="角色筛选" size="sm" minWidth={130} value={role} onChange={setRole}
                  options={[{ value: "", label: "全部角色" }, ...ROLE_OPTIONS.map((r) => ({ value: r.value, label: r.label }))]} />
          <Select ariaLabel="学院筛选" size="sm" minWidth={130} value={house} onChange={setHouse}
                  options={[{ value: "", label: "全部学院" }, ...HOUSE_OPTIONS]} />
          <span className="q-toolbar-end note" style={{ border: 0, padding: 0 }}>
            共 {shown.length} 个账号
          </span>
        </div>
      </div>

      {msg && <div className="ok-box mt-4" role="status">{msg}</div>}
      {err && <div className="error-box mt-4" role="alert">{err}</div>}

      <div className="card">
        <h3>账号列表</h3>
        <div className="table-scroll">
          <table className="table">
            <caption className="sr-only">账号列表，可调整角色、学院与会话</caption>
            <thead>
              <tr>
                <th scope="col">用户名</th><th scope="col">显示名</th><th scope="col">角色</th>
                <th scope="col">学院</th><th scope="col">状态</th><th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((u) => (
                <tr key={u.id} className={u.status === "banned" ? "u-banned" : ""}>
                  <td className="mono">{u.username}</td>
                  <td>{u.display_name || "—"}</td>
                  <td>
                    <Select ariaLabel={`${u.username} 的角色`} size="sm" minWidth={110}
                            value={u.role} onChange={(v) => setUserRole(u.id, u.username, v)}
                            options={ROLE_OPTIONS} />
                  </td>
                  <td>
                    <Select ariaLabel={`${u.username} 的学院`} size="sm" minWidth={110}
                            value={u.house?.key ?? ""}
                            onChange={(v) => setUserHouse(u.id, u.username, v)}
                            options={[{ value: "", label: "未绑定", hint: "不能参加正式比赛" }, ...HOUSE_OPTIONS]} />
                  </td>
                  <td>
                    {u.status === "banned" ? <span className="q-chip st-retired">已封禁</span>
                      : u.status === "pending" ? <span className="q-chip rv-pending">待审核</span>
                      : <span className="q-chip st-published">正常</span>}
                  </td>
                  <td>
                    <div className="row" style={{ gap: 6 }}>
                      <button className="btn btn-sm" onClick={() => revoke(u.id, u.username)}>吊销会话</button>
                      {u.status === "banned"
                        ? <button className="btn btn-sm" onClick={() => ban(u.id, u.username, false)}>解封</button>
                        : <button className="btn btn-sm q-danger" onClick={() => ban(u.id, u.username, true)}>封禁</button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {shown.length === 0 && <p className="note">没有符合条件的账号。</p>}
        <p className="r-note">
          角色只决定额外能力，不决定能不能参赛：绑定了学院的学生都能代表本院上场。
          封禁与调整归属都会写入审计日志；已开赛的比赛以当时冻结的名单为准，改动不影响历史成绩。
        </p>
      </div>
    </>
  );
}

function AuditTab() {
  const logs = useQuery({ queryKey: ["admin-audit"], queryFn: () => api.get<{ items: Array<{ id: number; actor: string | null; action: string; entity_type: string; entity_id: string | null; detail: Record<string, unknown>; created_at: string }> }>("/api/v1/admin/audit?limit=100"), refetchInterval: 8000 });
  return (
    <div className="card">
      <h3>审计日志（最近 100 条）</h3>
      <table className="table">
        <thead><tr><th>时间</th><th>动作</th><th>对象</th><th>操作者</th><th>详情</th></tr></thead>
        <tbody>
          {(logs.data?.items ?? []).map((a) => (
            <tr key={a.id}>
              <td className="num">{a.created_at?.slice(0, 19).replace("T", " ")}</td>
              <td className="mono">{a.action}</td>
              <td className="mono">{a.entity_type}:{(a.entity_id ?? "").slice(0, 8)}</td>
              <td className="mono">{(a.actor ?? "system").slice(0, 8)}</td>
              <td className="mono" style={{ maxWidth: 420, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{JSON.stringify(a.detail)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
