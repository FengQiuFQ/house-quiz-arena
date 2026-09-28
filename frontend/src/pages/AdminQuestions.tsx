import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import Select from "../components/Select";
import { IconClose, IconArrowRight, IconStar } from "../design/icons";

/**
 * 题库审核（管理后台的主工作台）。
 *
 * 这一页要回答老师最常问的三件事，所以三件事都必须"一眼看到、一步做到"：
 *   1. 现在有多少题、哪些还没审？ → 左侧题库栏常驻显示 总数/草稿/已发布/待审/重复
 *   2. 我要找的那道题在哪？       → 搜索框 + 主题/难度/状态筛选 + 分页
 *   3. 这道题到底对不对？         → 行内直接展开题干、四个选项，**正确项标出来**
 *
 * 设计取舍：
 *   · **不做"点进去才知道内容"的列表**。原实现只有一行题干，要审一道题得靠记忆对答案，
 *     这是"审核系统不好用"的根源。现在每行可展开，展开就是完整题面 + 答案 + 解析 + 来源。
 *   · **批量优先**。开学前导入 300 道题，逐个点"通过"不现实：勾选 + 全选本页 + 批量通过/驳回。
 *   · **删除是有代价的操作**：被比赛用过的题不能删（会掏空历史赛果），界面上直接不提供删除按钮，
 *     只给「停用」；未被用过的题才出现删除，并说明理由。
 *   · 编辑走抽屉（Drawer）而不是跳页：审题时经常要对着列表来回看，跳页会丢掉上下文。
 *   · 所有"改了什么"都要能被追溯：改题 = 新建一版并回到待审，界面上明说这一点。
 *
 * 视觉上这一页是**高密度工具界面**，不是营销页：表格行高紧、分隔线细、
 * 数字用等宽字体对齐，颜色只用来表达状态（草稿/已发布/停用），不用来表达装饰。
 */

interface QItem {
  question_id: string; version_id: string; version_no: number;
  subject: string; difficulty: number; language: string;
  pool: string; status: string; review_state: string;
  prompt: string; options: Array<{ option_id: string; text: string }>;
  correct_option_id: string; correct_index: number;
  explanation: string | null; source: string | null;
  created_at: string | null; updated_at: string | null; used_in_matches: number;
}

interface QDetail extends QItem {
  history: Array<{ version_no: number; review_state: string; prompt: string; created_at: string | null }>;
}

interface QStats {
  pools: Record<string, { total: number; draft: number; published: number; retired: number; pending_review: number }>;
  subjects: Array<{ subject: string; total: number; draft: number; published: number; retired: number; official: number; practice: number }>;
  review_queue: number;
  duplicates: number;
}

/* 两个题库的对外称呼。与后端 `competitions.service.POOL_LABELS` 保持一致：
   建赛时管理员选的是同一个"大类"，两处说法不一样会让人以为是两种东西。
   内部键仍是 official/practice（不要改，接口与库里都是它）。 */
const POOL_LABEL: Record<string, string> = { official: "竞赛题库", practice: "日常题库" };
/* 一行副标题说清这个题库是干什么的 —— 概览卡上只有名字时，
   "哪一边会被比赛抽到"要靠记忆，而记错的后果是正式赛抽到了练习题的印象。 */
const POOL_DESC: Record<string, string> = {
  official: "正式比赛抽题用；练习永远看不到这里的题",
  practice: "学生日常刷题与排行榜用；也可以拿去办院内小赛",
};
const STATUS_LABEL: Record<string, string> = { draft: "草稿", published: "已发布", retired: "已停用" };
const REVIEW_LABEL: Record<string, string> = { pending: "待审核", approved: "已通过", rejected: "已驳回" };
const DIFFICULTY_LABEL: Record<number, string> = { 1: "基础", 2: "进阶", 3: "挑战" };
const PAGE_SIZE = 20;

function StatusChip({ status, review }: { status: string; review: string }) {
  return (
    <span className="q-chips">
      <span className={`q-chip st-${status}`}>{STATUS_LABEL[status] ?? status}</span>
      <span className={`q-chip rv-${review}`}>{REVIEW_LABEL[review] ?? review}</span>
    </span>
  );
}

/** 单题展开区：完整题面 + 选项（正确项标出）+ 解析 + 来源 + 变更历史。
 *  历史版本要单独查一次（列表为了跑得快只给当前版本），所以这里区分"还没查到"与"确实只有一版"。 */
function QuestionBody({ q, detail, onEdit }: { q: QItem; detail?: QDetail; onEdit: () => void }) {
  return (
    <div className="q-body">
      <ol className="q-opts">
        {q.options.map((o, i) => {
          const right = o.option_id === q.correct_option_id;
          return (
            <li key={o.option_id} className={right ? "right" : ""}>
              <span className="q-letter">{String.fromCharCode(65 + i)}</span>
              <span className="q-opt-text">{o.text}</span>
              {right && <span className="q-right">正确答案</span>}
            </li>
          );
        })}
      </ol>
      {q.explanation ? (
        <p className="q-explain"><b>解析</b>{q.explanation}</p>
      ) : (
        <p className="q-explain dim">这道题还没有写解析。学生答完后会看到解析，建议补上。</p>
      )}
      <div className="q-meta">
        <span>来源：{q.source || "未填写"}</span>
        <span>难度：{DIFFICULTY_LABEL[q.difficulty] ?? q.difficulty}</span>
        <span>主题：{q.subject}</span>
        <span>版本：v{q.version_no}</span>
        <span>更新：{q.updated_at?.slice(0, 16).replace("T", " ") ?? "—"}</span>
        <span className={q.used_in_matches > 0 ? "q-used" : ""}>
          {q.used_in_matches > 0 ? `已被 ${q.used_in_matches} 场比赛使用` : "未被比赛使用"}
        </span>
      </div>
      {detail?.history && detail.history.length > 1 && (
        <details className="q-history">
          <summary>历史版本（{detail.history.length}）</summary>
          <ul>
            {detail.history.map((h) => (
              <li key={h.version_no}>
                <span className="num">v{h.version_no}</span>
                <span className={`q-chip rv-${h.review_state}`}>{REVIEW_LABEL[h.review_state] ?? h.review_state}</span>
                <span className="q-hist-prompt">{h.prompt}</span>
                <time className="num">{h.created_at?.slice(0, 16).replace("T", " ") ?? ""}</time>
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="q-actions">
        <button className="btn btn-sm" onClick={onEdit}>编辑此题（新建一版）</button>
      </div>
    </div>
  );
}

/** 编辑/新建抽屉：一个表单同时服务"新建"与"改题"，避免两套字段各写一遍。 */
function QuestionDrawer({
  editing, subjects, onClose, onSaved,
}: {
  editing: QItem | null;          // null = 新建
  subjects: string[];
  onClose: () => void;
  onSaved: (msg: string) => void;
}) {
  const isNew = editing === null;
  const [f, setF] = useState(() => ({
    pool: editing?.pool ?? "official",
    subject: editing?.subject ?? (subjects[0] ?? ""),
    difficulty: editing?.difficulty ?? 1,
    prompt: editing?.prompt ?? "",
    options: editing?.options.map((o) => o.text) ?? ["", "", "", ""],
    correct_index: editing?.correct_index ?? 0,
    explanation: editing?.explanation ?? "",
    source: editing?.source ?? "",
    approve: false,
  }));
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const firstField = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { firstField.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const setOption = (i: number, text: string) =>
    setF((p) => { const o = [...p.options]; o[i] = text; return { ...p, options: o }; });

  const addOption = () => setF((p) => (p.options.length >= 6 ? p : { ...p, options: [...p.options, ""] }));
  const removeOption = (i: number) => setF((p) => {
    if (p.options.length <= 2) return p;
    const o = p.options.filter((_, k) => k !== i);
    return { ...p, options: o, correct_index: Math.min(p.correct_index, o.length - 1) };
  });

  const save = async () => {
    setErr(null);
    const opts = f.options.map((t) => t.trim());
    if (f.prompt.trim().length < 3) { setErr("题干至少 3 个字。"); return; }
    if (opts.some((t) => !t)) { setErr("每个选项都要填写；不需要的选项请删掉。"); return; }
    if (!f.subject.trim()) { setErr("请填写主题（用于按主题抽题）。"); return; }
    setBusy(true);
    try {
      const body = {
        pool: f.pool, subject: f.subject.trim(), difficulty: Number(f.difficulty),
        prompt: f.prompt.trim(),
        options: opts.map((text) => ({ text })),
        correct_index: Number(f.correct_index),
        explanation: f.explanation.trim() || null,
        source: f.source.trim() || null,
        ...(isNew ? { approve: f.approve } : {}),
      };
      if (isNew) {
        await api.post("/api/v1/admin/questions", body);
        onSaved(f.approve ? "已新建并直接发布，可被正式比赛抽到。" : "已新建为草稿，审核通过后才会进抽题池。");
      } else {
        await api.put(`/api/v1/admin/questions/${editing.question_id}`, body);
        onSaved("已保存为新版本，回到待审核；审核通过后新版本才会生效。");
      }
      onClose();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "保存失败。");
    } finally { setBusy(false); }
  };

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true"
             aria-label={isNew ? "新建题目" : "编辑题目"} onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <h2>{isNew ? "新建题目" : `编辑 v${editing.version_no}`}</h2>
          <button className="drawer-x" aria-label="关闭" onClick={onClose}><IconClose size={18} /></button>
        </header>

        <div className="drawer-body">
          {err && <div className="error-box" role="alert">{err}</div>}
          {!isNew && (
            <div className="info-box">
              保存会新建一个版本，并把题目退回待审核，历史版本保留。
              已经被比赛用过的回合仍然按当时的版本计分，不受这次修改影响。
            </div>
          )}

          <div className="q-form-row">
            <label className="field">
              <span>题库</span>
              <Select ariaLabel="题库" value={f.pool} minWidth={140}
                      onChange={(v) => setF({ ...f, pool: v })}
                      options={[
                        { value: "official", label: "竞赛题库", hint: "正式比赛抽题用" },
                        { value: "practice", label: "日常题库", hint: "学生日常练习用" },
                      ]} />
            </label>
            <label className="field">
              <span>难度</span>
              <Select ariaLabel="难度" value={String(f.difficulty)} minWidth={120}
                      onChange={(v) => setF({ ...f, difficulty: Number(v) })}
                      options={[1, 2, 3].map((d) => ({ value: String(d), label: DIFFICULTY_LABEL[d] }))} />
            </label>
            <label className="field">
              <span>主题（用于按主题抽题）</span>
              <input list="q-subjects" value={f.subject} onChange={(e) => setF({ ...f, subject: e.target.value })} />
              <datalist id="q-subjects">{subjects.map((s) => <option key={s} value={s} />)}</datalist>
            </label>
          </div>

          <label className="field">
            <span>题干</span>
            <textarea ref={firstField} rows={3} value={f.prompt}
                      onChange={(e) => setF({ ...f, prompt: e.target.value })}
                      placeholder="题目正文。不要写「A. 」这类选项前缀，选项填在下面。" />
          </label>

          <fieldset className="field q-opts-edit">
            <legend>选项（勾选左边圆点标记正确答案）</legend>
            {f.options.map((t, i) => (
              <div key={i} className={`q-opt-row${f.correct_index === i ? " right" : ""}`}>
                <label className="q-opt-pick">
                  <input type="radio" name="correct" checked={f.correct_index === i}
                         onChange={() => setF({ ...f, correct_index: i })} />
                  <span className="sr-only">把第 {i + 1} 项设为正确答案</span>
                </label>
                <span className="q-letter">{String.fromCharCode(65 + i)}</span>
                <input value={t} onChange={(e) => setOption(i, e.target.value)} placeholder={`选项 ${i + 1}`} />
                {f.options.length > 2 && (
                  <button type="button" className="q-opt-del" aria-label={`删除选项 ${i + 1}`}
                          onClick={() => removeOption(i)}><IconClose size={14} /></button>
                )}
              </div>
            ))}
            {f.options.length < 6 && (
              <button type="button" className="btn btn-sm" onClick={addOption}>再加一个选项</button>
            )}
          </fieldset>

          <label className="field">
            <span>解析（学生答完后看到；留空则显示"暂无解析"）</span>
            <textarea rows={2} value={f.explanation} onChange={(e) => setF({ ...f, explanation: e.target.value })} />
          </label>

          <label className="field">
            <span>来源 / 出处（可选，便于核查）</span>
            <input value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })} />
          </label>

          {isNew && (
            <label className="sw-row">
              <input type="checkbox" checked={f.approve} onChange={(e) => setF({ ...f, approve: e.target.checked })} />
              <span>
                <b>保存后直接发布</b>
                <em>你自己录的题不必再审一遍。不勾选则存为草稿，审核通过后才进抽题池。</em>
              </span>
            </label>
          )}
        </div>

        <footer className="drawer-foot">
          <button className="btn btn-primary" disabled={busy} onClick={save}>
            {busy ? "保存中…" : isNew ? "新建" : "保存为新版本"}
          </button>
          <button className="btn" onClick={onClose}>取消</button>
        </footer>
      </aside>
    </div>
  );
}

export default function QuestionsTab() {
  const qc = useQueryClient();
  const [pool, setPool] = useState("official");
  const [status, setStatus] = useState("");           // "" = 全部
  const [subject, setSubject] = useState("");         // "" = 全部
  const [difficulty, setDifficulty] = useState("");   // "" = 全部
  const [review, setReview] = useState("");
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");                     // 已提交的搜索词（回车/按钮才生效）
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState("created_desc");
  const [openId, setOpenId] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<QItem | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [csv, setCsv] = useState("");

  // 换筛选条件时回到第一页并清空勾选：否则会停在"第 7 页"这种空页上，看起来像题库空了
  useEffect(() => { setPage(1); setPicked(new Set()); setOpenId(null); },
    [pool, status, subject, difficulty, review, q, sort]);

  const stats = useQuery({
    queryKey: ["q-stats"],
    queryFn: () => api.get<QStats>("/api/v1/admin/questions/stats"),
  });

  const list = useQuery({
    queryKey: ["admin-questions", pool, status, subject, difficulty, review, q, sort, page],
    queryFn: () => {
      const p = new URLSearchParams({ pool, sort, page: String(page), size: String(PAGE_SIZE) });
      if (status) p.set("status", status);
      if (subject) p.set("subject", subject);
      if (difficulty) p.set("difficulty", difficulty);
      if (review) p.set("review_state", review);
      if (q) p.set("q", q);
      return api.get<{ items: QItem[]; total: number; page: number; size: number }>(
        `/api/v1/admin/questions?${p.toString()}`);
    },
  });

  const detail = useQuery({
    queryKey: ["q-detail", openId],
    enabled: !!openId,
    queryFn: () => api.get<QDetail>(`/api/v1/admin/questions/${openId}`),
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["admin-questions"] });
    void qc.invalidateQueries({ queryKey: ["q-stats"] });
    void qc.invalidateQueries({ queryKey: ["q-detail"] });
  };

  /** 所有写操作走同一个壳：清消息 → 调接口 → 报结果 → 刷新。
   *  这样"操作后界面没变"这类问题不会因为某处忘了 invalidate 而复现。 */
  const run = useMutation({
    mutationFn: async (fn: () => Promise<string>) => fn(),
    onSuccess: (m) => { setErr(null); setMsg(m); invalidate(); },
    onError: (e) => { setMsg(null); setErr(e instanceof ApiError ? e.message : "操作失败。"); },
  });

  const reviewOne = (id: string, approve: boolean) => run.mutate(async () => {
    await api.post(`/api/v1/admin/questions/${id}/review?approve=${approve}`);
    return approve ? "已通过，这道题现在可以被抽题。" : "已驳回，题目留在草稿里。";
  });

  const retireOne = (id: string) => run.mutate(async () => {
    await api.post(`/api/v1/admin/questions/${id}/retire`);
    return "已停用。历史比赛成绩不受影响。";
  });

  const restoreOne = (id: string) => run.mutate(async () => {
    await api.post(`/api/v1/admin/questions/${id}/restore`);
    return "已恢复。若当前版本已审核通过，会回到已发布。";
  });

  const deleteOne = (id: string) => run.mutate(async () => {
    await api.del(`/api/v1/admin/questions/${id}`);
    return "已删除。";
  });

  const bulkReview = (approve: boolean) => run.mutate(async () => {
    const r = await api.post<{ updated: number; skipped: Array<{ id: string; why: string }> }>(
      "/api/v1/admin/questions/bulk-review", { ids: [...picked], approve });
    setPicked(new Set());
    return `已${approve ? "通过" : "驳回"} ${r.updated} 道`
      + (r.skipped.length ? `；${r.skipped.length} 道跳过（已停用或已不存在）` : "。");
  });

  const bulkRetire = () => run.mutate(async () => {
    const r = await api.post<{ updated: number; skipped: Array<{ id: string; why: string }> }>(
      "/api/v1/admin/questions/bulk-retire", { ids: [...picked] });
    setPicked(new Set());
    return `已停用 ${r.updated} 道。`;
  });

  const bulkSubject = (name: string) => run.mutate(async () => {
    const r = await api.post<{ updated: number }>("/api/v1/admin/questions/bulk-subject",
      { ids: [...picked], subject: name });
    setPicked(new Set());
    return `已把 ${r.updated} 道题的主题改为「${name}」。`;
  });

  const doImport = () => run.mutate(async () => {
    const r = await api.post<{ created: number; duplicates: number; error_total: number }>(
      "/api/v1/admin/questions/import", { pool, csv_text: csv });
    setCsv("");
    return `导入完成：新增 ${r.created}，重复跳过 ${r.duplicates}，格式错误 ${r.error_total}。`
      + "导入的题一律是草稿，审核通过后才进抽题池。";
  });

  const doExport = async () => {
    try {
      const r = await api.get<{ csv_text: string }>(`/api/v1/admin/questions/export?pool=${pool}`);
      const blob = new Blob([r.csv_text], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `questions-${pool}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { setErr(e instanceof ApiError ? e.message : "导出失败。"); }
  };

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const allPicked = items.length > 0 && items.every((x) => picked.has(x.question_id));
  const subjects = useMemo(() => (stats.data?.subjects ?? []).map((s) => s.subject), [stats.data]);
  const poolStats = stats.data?.pools?.[pool];

  const togglePick = (id: string) => setPicked((p) => {
    const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n;
  });
  const toggleAll = () => setPicked((p) => {
    const n = new Set(p);
    if (allPicked) items.forEach((x) => n.delete(x.question_id));
    else items.forEach((x) => n.add(x.question_id));
    return n;
  });

  const hasFilter = !!(status || subject || difficulty || review || q);
  const resetFilters = () => {
    setStatus(""); setSubject(""); setDifficulty(""); setReview(""); setSearch(""); setQ("");
  };

  return (
    <>
      {/* ── 顶部：题库概览。这是"一眼看到家底"的那一块，常驻不折叠。 ── */}
      <div className="q-overview">
        {(["official", "practice"] as const).map((p) => {
          const s = stats.data?.pools?.[p];
          const on = pool === p;
          return (
            <button key={p} className={`q-pool${on ? " on" : ""}`} onClick={() => setPool(p)}
                    aria-pressed={on}>
              <span className="q-pool-name">{POOL_LABEL[p]}</span>
              <span className="q-pool-num num">{s?.total ?? "—"}</span>
              <span className="q-pool-desc">{POOL_DESC[p]}</span>
              <span className="q-pool-facts">
                <em className={s?.pending_review ? "warn" : ""}>待审 {s?.pending_review ?? 0}</em>
                <em>草稿 {s?.draft ?? 0}</em>
                <em>已发布 {s?.published ?? 0}</em>
                <em>停用 {s?.retired ?? 0}</em>
              </span>
            </button>
          );
        })}
        <div className="q-overview-side">
          <span className="q-kpi">
            <b className="num">{stats.data?.review_queue ?? 0}</b>
            <em>全部待审</em>
          </span>
          <span className="q-kpi">
            <b className="num">{stats.data?.duplicates ?? 0}</b>
            <em>重复题干</em>
          </span>
          <span className="q-kpi">
            <b className="num">{stats.data?.subjects.length ?? 0}</b>
            <em>主题数</em>
          </span>
        </div>
      </div>

      {msg && <div className="ok-box mt-4" role="status">{msg}</div>}
      {err && <div className="error-box mt-4" role="alert">{err}</div>}

      {/* ── 工具条：搜索 + 筛选 + 新建/导入/导出。 ── */}
      <div className="q-toolbar">
        <form className="q-search" onSubmit={(e) => { e.preventDefault(); setQ(search.trim()); }}>
          <input value={search} onChange={(e) => setSearch(e.target.value)}
                 placeholder="搜索题干、主题、来源或解析…" aria-label="搜索题目" />
          <button className="btn btn-sm btn-primary" type="submit">搜索</button>
          {q && <button className="btn btn-sm" type="button" onClick={() => { setSearch(""); setQ(""); }}>清除</button>}
        </form>

        <Select ariaLabel="状态筛选" size="sm" minWidth={120} value={status}
                onChange={setStatus}
                options={[{ value: "", label: "全部状态" },
                          ...["draft", "published", "retired"].map((s) => ({ value: s, label: STATUS_LABEL[s] }))]} />
        <Select ariaLabel="审核筛选" size="sm" minWidth={120} value={review}
                onChange={setReview}
                options={[{ value: "", label: "全部审核态" },
                          ...["pending", "approved", "rejected"].map((s) => ({ value: s, label: REVIEW_LABEL[s] }))]} />
        <Select ariaLabel="难度筛选" size="sm" minWidth={110} value={difficulty}
                onChange={setDifficulty}
                options={[{ value: "", label: "全部难度" },
                          ...[1, 2, 3].map((d) => ({ value: String(d), label: DIFFICULTY_LABEL[d] }))]} />
        <Select ariaLabel="主题筛选" size="sm" minWidth={140} value={subject}
                onChange={setSubject}
                options={[{ value: "", label: "全部主题" },
                          ...(stats.data?.subjects ?? []).map((s) => ({
                            value: s.subject, label: s.subject, hint: `共 ${s.total} 道`,
                          }))]} />
        <Select ariaLabel="排序" size="sm" minWidth={140} value={sort} onChange={setSort}
                options={[
                  { value: "created_desc", label: "最新录入在前" },
                  { value: "created_asc", label: "最早录入在前" },
                  { value: "subject", label: "按主题" },
                  { value: "difficulty", label: "按难度" },
                ]} />
        {hasFilter && <button className="btn btn-sm" onClick={resetFilters}>重置筛选</button>}

        <span className="q-toolbar-end">
          <button className="btn btn-sm btn-primary"
                  onClick={() => { setEditing(null); setDrawerOpen(true); }}>新建题目</button>
          <button className="btn btn-sm" aria-expanded={importOpen}
                  onClick={() => setImportOpen((v) => !v)}>批量导入 / 导出</button>
        </span>
      </div>

      {importOpen && (
        <div className="card q-import">
          <h3>CSV 批量导入（导入到「{POOL_LABEL[pool]}」）</h3>
          <p className="note">
            列顺序：题干, 选项A, 选项B, 选项C, 选项D, 正确答案(A-D), 解析, 主题, 难度(1-3)。
            与题库中已有的题干重复的行会被跳过；导入的题一律是草稿，审核通过后才进抽题池。
          </p>
          <textarea rows={5} className="mono" value={csv} onChange={(e) => setCsv(e.target.value)}
                    aria-label="CSV 内容"
                    placeholder={"题干,选项A,选项B,选项C,选项D,正确答案,解析,主题,难度\n…,…,…,…,…,…,…,…,…"} />
          <div className="row mt-3">
            <button className="btn btn-primary" disabled={!csv.trim()} onClick={doImport}>导入为草稿</button>
            <button className="btn" onClick={() => void doExport()}>导出「{POOL_LABEL[pool]}」全部题目</button>
          </div>
        </div>
      )}

      {/* ── 批量操作条：只在有勾选时出现，避免平时占地方。 ── */}
      {picked.size > 0 && (
        <div className="q-bulk" role="region" aria-label="批量操作">
          <span className="q-bulk-count">已选 <b className="num">{picked.size}</b> 道</span>
          <button className="btn btn-sm btn-primary" onClick={() => bulkReview(true)}>批量通过</button>
          <button className="btn btn-sm" onClick={() => bulkReview(false)}>批量驳回</button>
          <button className="btn btn-sm" onClick={bulkRetire}>批量停用</button>
          <label className="q-bulk-subject">
            改主题
            <input list="q-subjects" placeholder="输入主题名后回车"
                   onKeyDown={(e) => {
                     if (e.key !== "Enter") return;
                     const v = (e.target as HTMLInputElement).value.trim();
                     if (v) { bulkSubject(v); (e.target as HTMLInputElement).value = ""; }
                   }} />
          </label>
          <button className="btn btn-sm" onClick={() => setPicked(new Set())}>取消选择</button>
        </div>
      )}

      {/* ── 列表 ── */}
      <div className="card q-list-card">
        <div className="q-list-head">
          <h3>
            {POOL_LABEL[pool]}
            {status && ` · ${STATUS_LABEL[status]}`}
            {subject && ` · ${subject}`}
            {q && ` · 含「${q}」`}
            <span className="q-count num">{total} 道</span>
          </h3>
          {pages > 1 && (
            <div className="q-pager">
              <button className="btn btn-sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>上一页</button>
              <span className="num">{page} / {pages}</span>
              <button className="btn btn-sm" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>下一页</button>
            </div>
          )}
        </div>

        {list.isLoading && <p className="note">载入中…</p>}
        {!list.isLoading && items.length === 0 && (
          <p className="note">
            这个条件下没有题目。
            {hasFilter
              ? "试试「重置筛选」，或者换一个题库看看。"
              : poolStats?.total === 0
                ? "这个题库还是空的 —— 用上面的「新建题目」或「批量导入」把题录进来。"
                : "。"}
          </p>
        )}

        {items.length > 0 && (
          <>
            <div className="q-selectall">
              <label>
                <input type="checkbox" checked={allPicked} onChange={toggleAll} />
                <span>选中本页 {items.length} 道</span>
              </label>
            </div>
            <ul className="q-rows">
              {items.map((x) => {
                const open = openId === x.question_id;
                return (
                  <li key={x.question_id} className={`q-row${open ? " open" : ""}`}>
                    <div className="q-row-main">
                      <input type="checkbox" className="q-pick" checked={picked.has(x.question_id)}
                             onChange={() => togglePick(x.question_id)}
                             aria-label={`选择：${x.prompt.slice(0, 20)}`} />
                      <button className="q-row-toggle" aria-expanded={open}
                              onClick={() => setOpenId(open ? null : x.question_id)}>
                        <span className="q-row-prompt">{x.prompt}</span>
                        <span className="q-row-tags">
                          <span className="q-chip">{x.subject}</span>
                          <span className="q-chip">{DIFFICULTY_LABEL[x.difficulty] ?? `难度${x.difficulty}`}</span>
                          <StatusChip status={x.status} review={x.review_state} />
                          {x.used_in_matches > 0 && <span className="q-chip used">用过 {x.used_in_matches} 次</span>}
                          {x.explanation ? null : <span className="q-chip warn">缺解析</span>}
                        </span>
                        <IconArrowRight size={14} className={`q-row-arrow${open ? " open" : ""}`} />
                      </button>
                      <div className="q-row-actions">
                        {x.review_state === "pending" && x.status !== "retired" && (
                          <>
                            <button className="btn btn-sm btn-primary" onClick={() => reviewOne(x.question_id, true)}>通过</button>
                            <button className="btn btn-sm" onClick={() => reviewOne(x.question_id, false)}>驳回</button>
                          </>
                        )}
                        {x.status === "retired" ? (
                          <button className="btn btn-sm" onClick={() => restoreOne(x.question_id)}>恢复</button>
                        ) : (
                          <button className="btn btn-sm" onClick={() => retireOne(x.question_id)}>停用</button>
                        )}
                        {x.used_in_matches === 0 && (
                          <button className="btn btn-sm q-danger"
                                  title="这道题没有被任何比赛用过，删除不会影响历史成绩"
                                  onClick={() => {
                                    if (window.confirm(`删除这道题？\n\n${x.prompt.slice(0, 60)}\n\n删除后无法恢复。`)) {
                                      deleteOne(x.question_id);
                                    }
                                  }}>删除</button>
                        )}
                      </div>
                    </div>
                    {open && (
                      <QuestionBody q={x}
                                    detail={detail.data?.question_id === x.question_id ? detail.data : undefined}
                                    onEdit={() => { setEditing(x); setDrawerOpen(true); }} />
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}

        <p className="r-note">
          未通过审核的题目不会进入抽题池。审核、改题、停用与删除都会写入审计日志。
          比赛用过的题不能删除（否则历史赛果会查不到题），请改用「停用」。
        </p>
      </div>

      {drawerOpen && (
        <QuestionDrawer editing={editing} subjects={subjects}
                        onClose={() => setDrawerOpen(false)}
                        onSaved={(m) => { setErr(null); setMsg(m); invalidate(); }} />
      )}
    </>
  );
}
