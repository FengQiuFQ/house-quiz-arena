import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useAuth } from "../App";
import SubjectPicker, { availableCount } from "../components/SubjectPicker";

interface PQuestion {
  question_version_id: string;
  subject: string | null;
  difficulty: number | null;
  prompt: string;
  options: Array<{ option_id: string; text: string }>;
}
interface PSummary {
  answered_count: number; correct_count: number; accuracy: number;
  best_streak: number; total_ms: number | null; ranked: boolean;
  ranked_threshold: number; note: string;
}
interface PSession {
  session_id: string; learner: string; anonymous: boolean;
  questions: PQuestion[];
  /** 这场练习涉及的主题。单选主题时是那一个；多选时是勾选的那几个；不限主题时是实际抽到的。 */
  subjects?: string[];
}
interface AnswerOut {
  is_correct: boolean; correct_option_id?: string | null; explanation?: string | null;
  already_answered?: boolean; elapsed_ms?: number; counted_for_speed?: boolean;
  summary: PSummary;
}
interface LbItem {
  rank: number; display_name: string; answered_count: number; correct_count: number;
  accuracy: number; best_streak: number; avg_correct_ms: number | null; speed_sample: number;
}
interface LbHouse {
  rank: number; house_key: string; house_name: string; learners: number;
  answered_total: number; correct_total: number; accuracy: number;
}

const MODES: Array<{ key: string; label: string; count: number; hint: string }> = [
  { key: "quick", label: "快问快答", count: 5, hint: "5 题 · 约 2 分钟" },
  { key: "quiz", label: "小测", count: 20, hint: "20 题 · 可上榜" },
  { key: "challenge", label: "长练", count: 30, hint: "30 题 · 攒题量" },
];

function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/**
 * 榜单组件。
 *
 * 抽出来是为了让**独立排行榜页**（/leaderboard）与练习页内的榜单用**同一份实现**：
 * 两处各写一份必然走样（口径文案、隐私说明、空态门槛都会漂），
 * 而"榜上写的是什么口径"是学生唯一能核对的东西。
 */
export function LeaderboardTable() {
  const [board, setBoard] = useState<"accuracy" | "speed" | "streak" | "house">("accuracy");
  const q = useQuery({
    queryKey: ["leaderboard", board],
    queryFn: () => api.get<{ items: Array<LbItem & LbHouse>; note: string; min_questions?: number }>(
      `/api/v1/leaderboard?board=${board}&limit=50`),
    refetchInterval: board === "house" ? false : 30000,
  });
  const tabs: Array<{ k: typeof board; label: string }> = [
    { k: "accuracy", label: "正确率榜" },
    { k: "speed", label: "速度榜" },
    { k: "streak", label: "连对榜" },
    { k: "house", label: "House 荣誉榜" },
  ];
  const items = q.data?.items ?? [];
  /* 表格标题（读屏用户听到的那句话）必须用**给人看的榜名**，不能用 state 里的内部键
     （accuracy/speed/streak）。用 `board` 直接拼会读出「练习accuracy排行榜」——
     这是实测到的真实缺陷：肉眼看不见（caption 是 sr-only），但读屏用户会听到一串英文键名。 */
  const boardLabel = tabs.find((t) => t.k === board)?.label ?? "练习";

  return (
    <section className="lb" aria-label="练习排行榜">
      <div className="seg lb-tabs" role="tablist" aria-label="榜单类型">
        {tabs.map((t) => (
          <button key={t.k} role="tab" aria-selected={board === t.k}
                  aria-pressed={board === t.k} onClick={() => setBoard(t.k)}>
            {t.label}
          </button>
        ))}
      </div>

      {q.isLoading && <p className="note">载入中…</p>}
      {!q.isLoading && items.length === 0 && (
        <div className="info-box">
          本榜暂时还没有达到门槛的成绩。
          {board !== "house" && ` 累计作答满 ${q.data?.min_questions ?? 20} 题即可上榜。`}
        </div>
      )}

      {items.length > 0 && board !== "house" && (
        <table className="table lb-table">
          <caption className="sr-only">练习{boardLabel}</caption>
          <thead>
            <tr>
              <th scope="col">名次</th>
              <th scope="col">昵称</th>
              {board === "accuracy" && <th scope="col">正确率</th>}
              {board === "speed" && <th scope="col">答对平均用时</th>}
              {board === "streak" && <th scope="col">最长连对</th>}
              <th scope="col">题量</th>
              <th scope="col">答对</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={`${it.rank}-${it.display_name}`}>
                <td className="num">{it.rank}</td>
                <td>{it.display_name}</td>
                {board === "accuracy" && <td className="num">{it.accuracy.toFixed(2)}%</td>}
                {board === "speed" && (
                  <td className="num">
                    {fmtMs(it.avg_correct_ms)}
                    {it.avg_correct_ms !== null && <small className="dim">（{it.speed_sample} 题样本）</small>}
                  </td>
                )}
                {board === "streak" && <td className="num">{it.best_streak}</td>}
                <td className="num">{it.answered_count}</td>
                <td className="num">{it.correct_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {items.length > 0 && board === "house" && (
        <table className="table lb-table">
          <caption className="sr-only">House 练习荣誉榜</caption>
          <thead>
            <tr>
              <th scope="col">名次</th><th scope="col">House</th>
              <th scope="col">参与人数</th><th scope="col">累计题量</th><th scope="col">正确率</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={it.house_key}>
                <td className="num">{it.rank}</td>
                <td><span className={`house-dot h-${it.house_key}`} aria-hidden="true" />{it.house_name}</td>
                <td className="num">{it.learners}</td>
                <td className="num">{it.answered_total}</td>
                <td className="num">{it.accuracy.toFixed(2)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {q.data?.note && <p className="note mt-4">{q.data.note}</p>}
      <p className="note">
        隐私说明：排行榜只显示昵称。未登录或未设置昵称的学习者统一显示为「枫禾学子」并用序号区分，
        不显示真实姓名、账号、年级或班级。
      </p>
    </section>
  );
}

export default function Practice() {
  const { me } = useAuth();
  const [tab, setTab] = useState<"practice" | "leaderboard">("practice");  const [sess, setSess] = useState<PSession | null>(null);
  const [idx, setIdx] = useState(0);
  const [picked, setPicked] = useState<string | null>(null);
  const [out, setOut] = useState<AnswerOut | null>(null);
  const [summary, setSummary] = useState<PSummary | null>(null);
  /* 已选主题。空数组＝"全部主题（混合）"：与后端"不传 subjects / 传空"是同一种意思，
     所以这个状态既能表示"没选"，也能表示"清空了选择"，不必再分两种情况。 */
  const [pickedSubjects, setPickedSubjects] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const loc = useLocation();

  /* 顶栏的"排行榜"入口用 `/practice#board` 指过来：
     它要的不是"练习页顶部"而是**榜单本身**，所以带 hash 进来时直接切到榜单标签。
     用 hash 而不是新路由，是因为榜单与练习共享同一屏上下文（切换 tag 比换页更快）。 */
  useEffect(() => {
    if (loc.hash === "#board") setTab("leaderboard");
  }, [loc.hash]);

  const subjects = useQuery({
    queryKey: ["practice-subjects"],
    queryFn: () => api.get<{
      items: Array<{ subject: string; count: number }>; total: number; published: number;
    }>("/api/v1/practice/subjects"),
  });

  const subjectItems = subjects.data?.items ?? [];
  // 已选主题的可用题量合计（未选＝全部主题的题量）。抽题是服务端 shuffle 后截断，
  // 题不够时**不会报错、只会少出题**，所以"够不够"必须在点之前就判出来。
  const avail = availableCount(subjectItems, subjects.data?.total ?? 0, pickedSubjects);
  const short = MODES.filter((m) => m.count > avail);

  const start = async (count: number) => {
    setErr(null); setBusy(true);
    try {
      const r = await api.post<PSession>("/api/v1/practice/sessions",
        // 空数组一律发 null：后端把"没有主题"理解成全部主题，
        // 发 [] 虽然等价，但会让日志里同时出现两种写法，排查时容易看错。
        { subjects: pickedSubjects.length ? pickedSubjects : null, count });
      setSess(r); setIdx(0); setPicked(null); setOut(null); setSummary(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "无法开始练习。");
    } finally { setBusy(false); }
  };

  const answer = async (qid: string, opt: string) => {
    if (!sess || out) return;
    setPicked(opt);
    try {
      const r = await api.post<AnswerOut>(
        `/api/v1/practice/sessions/${sess.session_id}/answers`,
        { question_version_id: qid, option_id: opt });
      setOut(r);
      setSummary(r.summary);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "提交失败。");
    }
  };

  const q = sess?.questions[idx];

  return (
    <div className="player">
      <div className="spread">
        <h1 className="display" style={{ fontSize: "var(--fs-h1)" }}>日常知识练习</h1>
        <div className="seg" role="tablist" aria-label="练习与排行榜">
          <button role="tab" aria-selected={tab === "practice"} aria-pressed={tab === "practice"}
                  onClick={() => setTab("practice")}>开始练习</button>
          <button role="tab" aria-selected={tab === "leaderboard"} aria-pressed={tab === "leaderboard"}
                  onClick={() => setTab("leaderboard")}>排行榜</button>
        </div>
      </div>
      <p className="lead mt-2">
        跨年龄通识题：科学、生活与安全、地理文化、语言趣味、逻辑、科技与 AI。
        {me ? "练习成绩独立保存，不进入 House 正式排名。" : "无需登录即可练习；上榜统一显示为「枫禾学子」。"}
      </p>

      {err && <div className="error-box mt-4" role="alert">{err}</div>}

      {tab === "leaderboard" ? <div className="mt-6"><LeaderboardTable /></div> : (
        <>
          {!sess && (
            <div className="mt-6">
              {/* 主题多选。原来这里是一个自绘单选下拉框（"全部主题（混合）" + 11 个主题），
                  只能练一个主题；学生想连着刷两三个主题就得反复回来重选。
                  控件本身见 components/SubjectPicker.tsx（含"为什么不用 Select 改多选"）。 */}
              <SubjectPicker
                items={subjectItems}
                total={subjects.data?.total ?? 0}
                published={subjects.data?.published ?? 0}
                value={pickedSubjects}
                onChange={setPickedSubjects}
                loading={subjects.isLoading}
                error={subjects.isError}
              />
              <div className="row mt-4">
                {MODES.map((m) => {
                  const enough = m.count <= avail;
                  return (
                    <button key={m.key} className={`btn ${m.key === "quiz" ? "btn-primary" : ""}`}
                            disabled={busy || !enough} onClick={() => start(m.count)}
                            title={enough ? m.hint
                                          : `已选主题只有 ${avail} 题，开不了 ${m.count} 题的这一档`}>
                      {m.label}（{m.count} 题）
                      <small className="dim"> · {enough ? m.hint.split(" · ")[1] : "题量不足"}</small>
                    </button>
                  );
                })}
              </div>
              {/* 题不够时按钮禁用，并且**在按钮旁**说清差多少、差在哪一档 ——
                  只把按钮变灰会让人以为是卡住了；后端在这种情况下也确实是"静默少出题"
                  （chosen = ids[:count]），所以这里绝不能放行。
                  整句拼成一个表达式再渲染：JSX 会把"行尾换行"处的空格吃掉，
                  分几行写会渲染出"不足20 题"这种缺空格的句子。 */}
              {short.length > 0 && subjectItems.length > 0 && (
                <p className="sp-warn" role="alert">
                  {(pickedSubjects.length ? "已选主题" : "全部主题") + `只有 ${avail} 题，不足 `
                    + short.map((m) => `${m.count} 题（${m.label}）`).join("、")
                    + (pickedSubjects.length ? "。请再多选几个主题。" : "。练习池题量不足，请稍后再试。")}
                </p>
              )}
            </div>
          )}

          {sess && !q && (
            <div className="ok-box mt-6">
              {/* 这一整句拼成一个表达式：原来分成几行的 JSX 会把行尾换行处的空格吃掉，
                  实测渲染成"正确率 20.00%· 最长连对 1· 用时 2.0s"（缺空格）。 */}
              <p style={{ margin: 0 }}>
                {"本组完成：答对 "}<b className="num">{summary?.correct_count ?? 0}</b>
                {` / ${sess.questions.length} 题`}
                {summary && ` · 正确率 ${summary.accuracy.toFixed(2)}%`
                  + ` · 最长连对 ${summary.best_streak}`
                  + ` · 用时 ${fmtMs(summary.total_ms)}`}
                {/* 本场练了哪几个主题由服务端给出（多主题时只存得下单选那一个，见后端注释），
                    结课时回看一眼比在答题中途显示更不打扰。 */}
                {(sess.subjects?.length ?? 0) > 0 && ` · 本场主题：${sess.subjects?.join("、")}`}
              </p>
              {summary && (
                <p className="note" style={{ marginBottom: 0 }}>
                  {summary.ranked
                    ? "本组已计入排行榜（同一题重复作答只计首次，单题不足 0.4 秒不计入速度）。"
                    : summary.note}
                </p>
              )}              <div className="row mt-4">
                <button className="btn btn-primary" onClick={() => setSess(null)}>再来一组</button>
                <button className="btn" onClick={() => { setSess(null); setTab("leaderboard"); }}>查看排行榜</button>
              </div>
            </div>
          )}

          {sess && q && (
            <>
              <div className="spread mt-6">
                <p className="p-cat">
                  第 {idx + 1} 题 / 共 {sess.questions.length} 题
                  {q.subject && <> · {q.subject}</>}
                  {q.difficulty && <> · 难度 {"◆".repeat(q.difficulty)}</>}
                </p>
                <p className="p-cat num">
                  答对 {summary?.correct_count ?? 0} · 连对 {summary?.best_streak ?? 0}
                  {sess.anonymous && <> · {sess.learner}</>}
                </p>
              </div>
              <p className="p-q">{q.prompt}</p>
              <div className="options" role="group" aria-label="选项">
                {q.options.map((o, i) => {
                  const isCorrect = !!out && o.option_id === out.correct_option_id;
                  const isMineWrong = !!out && picked === o.option_id && !out.is_correct;
                  return (
                    <button key={o.option_id}
                            className={`opt ${isCorrect ? "correct" : ""} ${isMineWrong ? "wrong" : ""}`}
                            onClick={() => answer(q.question_version_id, o.option_id)}
                            disabled={!!out}
                            aria-pressed={picked === o.option_id}>
                      <span className="letter">{String.fromCharCode(65 + i)}</span>
                      <span className="otext">{o.text}</span>
                      {isCorrect && <span className="ostate" style={{ color: "var(--state-ok)" }}>正确答案</span>}
                      {isMineWrong && <span className="ostate" style={{ color: "var(--state-err)" }}>你的选择</span>}
                    </button>
                  );
                })}
              </div>
              {out && (
                <div className="mt-5">
                  <p className="note" style={{ margin: 0 }}>
                    本题用时 {fmtMs(out.elapsed_ms)}
                    {out.counted_for_speed === false && "（不足 0.4 秒，不计入速度榜）"}
                  </p>
                  {out.explanation && <p className="note">解析：{out.explanation}</p>}
                  <button className="btn btn-primary mt-4"
                          onClick={() => { setIdx((i) => i + 1); setPicked(null); setOut(null); }}>
                    {idx + 1 >= sess.questions.length ? "查看结果" : "下一题"}
                  </button>
                </div>
              )}
            </>
          )}
          {summary && sess && idx >= sess.questions.length && (
            <p className="sr-only">本组练习已完成</p>
          )}
        </>
      )}
    </div>
  );
}