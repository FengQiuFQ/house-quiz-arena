import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAuth } from "../App";

/** 账号与安全。首登/口令被重置后必须在此改密（服务端强制：未改密时业务接口返回
 *  PASSWORD_CHANGE_REQUIRED，前端在 App 层也做一次拦截，避免用户撞到 403 才明白）。
 *
 *  改密成功后服务端会**轮换会话令牌**（旧设备上残留的页面立即失效），
 *  因此这里要 await reload() 重新同步身份，而不是就地跳转。 */
export default function Account() {
  const { me, reload } = useAuth();
  const nav = useNavigate();
  const forced = !!me?.must_change_password;

  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setOk(false);
    if (newPw.length < 8) { setErr("新密码至少 8 位。"); return; }
    if (newPw !== confirm) { setErr("两次输入的新密码不一致。"); return; }
    if (newPw === oldPw) { setErr("新密码不能与原密码相同。"); return; }
    setBusy(true);
    try {
      await api.post("/api/v1/auth/change-password", {
        old_password: oldPw, new_password: newPw,
      });
      setOk(true);
      setOldPw(""); setNewPw(""); setConfirm("");
      await reload();
      window.setTimeout(() => nav("/", { replace: true }), 900);
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : "网络错误，请重试。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="player narrow">
      <h1>{forced ? "首次登录：请设置新密码" : "账号与安全"}</h1>
      <p className="lead mt-2">
        {forced
          ? "当前账号仍在使用初始密码。为保证比赛期间成绩不被他人代答，请立即设置只有你知道的新密码。"
          : "修改登录密码。修改成功后，其他设备上已登录的页面会立即失效，需要重新登录。"}
      </p>

      {me && (
        <dl className="kv mt-5">
          <div><dt>账号</dt><dd>{me.username}</dd></div>
          <div><dt>角色</dt><dd>{me.role}</dd></div>
          <div><dt>House</dt><dd>{me.house ? `${me.house.name_cn} ${me.house.name_en}` : "未绑定"}</dd></div>
        </dl>
      )}

      {err && <div className="error-box mt-4" role="alert">{err}</div>}
      {ok && <div className="ok-box mt-4" role="status">密码已更新，正在返回首页…</div>}

      <form onSubmit={submit} className="mt-5">
        <div className="field">
          <label htmlFor="old">当前密码</label>
          <input id="old" type="password" value={oldPw} autoComplete="current-password"
                 onChange={(e) => setOldPw(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor="new">新密码（至少 8 位）</label>
          <input id="new" type="password" value={newPw} autoComplete="new-password"
                 onChange={(e) => setNewPw(e.target.value)} required minLength={8} />
        </div>
        <div className="field">
          <label htmlFor="confirm">确认新密码</label>
          <input id="confirm" type="password" value={confirm} autoComplete="new-password"
                 onChange={(e) => setConfirm(e.target.value)} required minLength={8} />
        </div>
        <button className="btn btn-primary btn-lg" type="submit" style={{ width: "100%" }} disabled={busy}>
          {busy ? "提交中…" : "设置新密码"}
        </button>
      </form>

      {forced && (
        <p className="note mt-4">
          未完成改密前，签到、答题、练习等操作都会被服务器拒绝（PASSWORD_CHANGE_REQUIRED）。
          如果这不是你的账号，请直接联系主持人。
        </p>
      )}
    </div>
  );
}
