import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { useAuth } from "../App";
import { HOUSE_ICON } from "../design/icons";

interface RegInfo {
  open: boolean;
  needs_invite_code: boolean;
  needs_review: boolean;
  houses: Array<{ key: string; name_cn: string; name_en: string; color_light: string; color_deep: string }>;
}

/**
 * 学生自助注册。
 *
 * 关键产品规则：**学院必选，且注册后自己不能更改**。
 * 这个约束必须让学生在选择之前就看清（而不是注册完才发现选错了），因此：
 *   · House 用大卡片单选，选中态明显；
 *   · 选中后立即出现"确认后不可自行更改"的提示，而不是等到最后才说；
 *   · 页脚说明如何修改（找管理员），给出补救路径而不是让人卡住。
 *
 * 三个"看起来像没做"的坑，这里都堵住了：
 *   1. **学院列表为空时不能静默渲染空区域** —— 那正是"注册页不显示选择学院"的观感来源。
 *      列表为空就直说"学院尚未配置"，并给出找谁。
 *   2. **接口出错不能被当成"未开放注册"** —— 两者提示完全不同：一个是等管理员开，
 *      一个是网络/服务异常可以重试。混在一起会让人往错的方向排查。
 *   3. 这一页**不再用 `.narrow`（470px）那套窄栏**：四张学院卡片挤在 470px 里每张不到 90px 宽，
 *      字被压成竖排，看上去"没有学院可选"。改用宽栏 + 2×2 网格。
 *
 * 参赛规则已改为"按学院参赛"：绑定学院即可代表本院参加正式比赛，不需要任何额外资格，
 * 所以这一页也不再出现"参加正式比赛需由管理员加入参赛名单"这类说法。
 */
export default function Register() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [code, setCode] = useState("");
  const [house, setHouse] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<null | { pending: boolean; house: string; username: string }>(null);
  const { reload } = useAuth();
  const nav = useNavigate();

  const info = useQuery({
    queryKey: ["registration-info"],
    queryFn: () => api.get<RegInfo>("/api/v1/auth/registration"),
    retry: 1,
    /* 关闭状态下**持续轮询**：现场最常见的顺序是"学生先打开注册页 → 老师再去后台开放注册"，
       而这个页面本身不会因为后台改了开关就重查一次 —— 学生盯着"暂未开放注册"干等，
       老师那边明明已经点开了，看起来就是"点了开放注册还是不能注册"。
       开放之后就不必再轮询（表单已经在眼前，没有需要感知的远端变化）。 */
    refetchInterval: (q) => (q.state.data?.open ? false : 5000),
  });

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (!house) { setErr("请先选择你所属的学院。"); return; }
    if (password.length < 8) { setErr("密码至少 8 位。"); return; }
    setBusy(true);
    try {
      const r = await api.post<{ pending_review: boolean }>("/api/v1/auth/register", {
        username, password, house_key: house, display_name: displayName || null,
        invite_code: code || null,
      });
      if (r.pending_review) {
        // 需审模式：不能直接进站，给一个明确的"已完成、在等审核"结果页，
        // 而不是把学生丢回首页让他自己发现登不进去。
        setDone({ pending: true, house, username });
      } else {
        reload();
        nav("/", { replace: true });
      }
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : "网络错误，请重试。");
    } finally { setBusy(false); }
  };

  if (info.isLoading) return <div className="auth-wrap"><p className="note">载入中…</p></div>;

  // 接口失败 ≠ 未开放注册。分开说，否则学生会去找老师开通一个其实已经开着的开关。
  if (info.isError || !info.data) {
    return (
      <div className="auth-wrap">
        <h1 className="display" style={{ fontSize: "var(--fs-h1)" }}>注册信息暂时取不到</h1>
        <p className="lead mt-2">
          这一页需要从服务器读取可选的学院列表，刚才没有取到。请检查网络后重试；
          若一直如此，把这一屏截图发给管理员。
        </p>
        <div className="row mt-5">
          <button className="btn btn-primary" onClick={() => void info.refetch()}>重试</button>
          <a className="btn" href="/login">已有账号，去登录</a>
        </div>
      </div>
    );
  }

  if (!info.data.open) {
    return (
      <div className="auth-wrap">
        <h1 className="display" style={{ fontSize: "var(--fs-h1)" }}>暂未开放注册</h1>
        <p className="lead mt-2">
          目前不开放自助注册。请联系老师或管理员为你开通账号，开通后即可登录练习与参赛。
        </p>
        <div className="row mt-5">
          <a className="btn btn-primary" href="/login">已有账号，去登录</a>
        </div>
      </div>
    );
  }

  const houses = info.data.houses;

  // 学院没配置时**必须说出来**：否则这一页看上去就是"没有学院可选"，
  // 而学生对此无能为力，只会以为系统坏了。
  if (houses.length === 0) {
    return (
      <div className="auth-wrap">
        <h1 className="display" style={{ fontSize: "var(--fs-h1)" }}>学院尚未配置</h1>
        <p className="lead mt-2">
          系统里还没有可选的学院，因此暂时无法完成注册。请联系管理员在后台确认学院设置后再来。
        </p>
        <div className="row mt-5">
          <a className="btn" href="/login">已有账号，去登录</a>
        </div>
      </div>
    );
  }

  // 注册结果页（需审模式）
  if (done) {
    return (
      <div className="auth-wrap">
        <div className="reg-done">
          <span className="rd-mark" aria-hidden="true">已提交</span>
          <h1 className="display" style={{ fontSize: "var(--fs-h1)" }}>注册已提交</h1>
          <p className="lead mt-2">
            账号 <b>{done.username}</b> 已提交，归属学院为 <b>{
              (houses.find((h) => h.key === done.house) || {}).name_cn
            }</b>。
          </p>
          <div className="info-box mt-4">
            本系统当前需要管理员审核：审核通过后你才能用这个账号登录。
            老师审核通常很快；若急需使用，请直接联系管理员。
          </div>
          <div className="row mt-5">
            <a className="btn btn-primary" href="/login">去登录页等待</a>
            <a className="btn" href="/">先看看首页与排行榜</a>
          </div>
        </div>
      </div>
    );
  }

  const picked = houses.find((h) => h.key === house) || null;

  return (
    <div className="auth-wrap">
      <h1 className="display" style={{ fontSize: "var(--fs-h1)" }}>注册账号</h1>
      <p className="lead mt-2">
        选好学院即可开始练习、上排行榜，也可以代表你的学院参加正式比赛。
        {info.data.needs_review && <> 本系统当前需要管理员审核，提交后请等待通过。</>}
      </p>

      {err && <div className="error-box mt-4" role="alert">{err}</div>}

      <form onSubmit={submit} className="mt-5">
        <fieldset className="field">
          <legend>第一步 · 选择你的学院</legend>
          <p className="note" style={{ marginTop: 2 }}>
            学院一旦确定，<b>你自己不能更改</b>；填错请找管理员调整。
          </p>
          <div className="house-pick" role="radiogroup" aria-label="选择学院"
               onKeyDown={(e) => {
                 // 方向键在四个学院之间移动（原生 radio 组的行为，自定义控件要自己补）
                 const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1
                   : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
                 if (!d) return;
                 e.preventDefault();
                 const i = Math.max(0, houses.findIndex((h) => h.key === house));
                 const next = houses[(i + d + houses.length) % houses.length];
                 setHouse(next.key);
               }}>
            {houses.map((h) => {
              const Icon = HOUSE_ICON[h.key];
              const on = house === h.key;
              return (
                <button
                  key={h.key}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  tabIndex={on || (!house && h.key === houses[0].key) ? 0 : -1}
                  className={`hp-card h-${h.key}${on ? " on" : ""}`}
                  onClick={() => setHouse(h.key)}
                >
                  <span className="hp-ico" aria-hidden="true">{Icon ? <Icon size={22} /> : null}</span>
                  <span className="hp-name">{h.name_cn}</span>
                  <span className="hp-en">{h.name_en}</span>
                  <span className="hp-check" aria-hidden="true">{on ? "已选择" : ""}</span>
                </button>
              );
            })}
          </div>
          {picked && (
            <p className="ok-box" style={{ marginTop: "var(--sp-3)", marginBottom: 0 }} role="status">
              你将加入 <b>{picked.name_cn}</b>。
              确认后本人无法更改归属，需要调整请联系管理员。
            </p>
          )}
        </fieldset>

        <fieldset className="field">
          <legend>第二步 · 设置登录信息</legend>
          <div className="auth-grid">
            <div className="field">
              <label htmlFor="u">用户名（登录用，3–64 位字母/数字/._-）</label>
              <input id="u" value={username} autoComplete="username" required minLength={3}
                     pattern="[A-Za-z0-9_.\-]+" onChange={(e) => setUsername(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="p">密码（至少 8 位）</label>
              <input id="p" type="password" value={password} autoComplete="new-password"
                     required minLength={8} onChange={(e) => setPassword(e.target.value)} />
            </div>
            <div className="field auth-grid-wide">
              <label htmlFor="n">显示名（可选，会出现在排行榜上）</label>
              <input id="n" value={displayName} maxLength={32} placeholder="留空则显示为「枫禾学子」"
                     onChange={(e) => setDisplayName(e.target.value)} />
            </div>
            {info.data.needs_invite_code && (
              <div className="field auth-grid-wide">
                <label htmlFor="c">邀请码</label>
                <input id="c" value={code} required onChange={(e) => setCode(e.target.value)} />
              </div>
            )}
          </div>
        </fieldset>

        <button className="btn btn-primary btn-lg" type="submit" style={{ width: "100%" }}
                disabled={busy || !house}>
          {busy ? "注册中…" : house ? `加入${picked ? picked.name_cn : ""}并注册` : "请先选择学院"}
        </button>
      </form>

      <p className="note mt-4">
        已经有账号？<a href="/login">直接登录</a>。
        成绩按学院聚合，与年级无关。
      </p>
    </div>
  );
}
