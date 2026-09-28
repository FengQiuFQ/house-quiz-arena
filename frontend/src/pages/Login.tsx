import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAuth } from "../App";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [needsTransfer, setNeedsTransfer] = useState(false);
  const [transferCode, setTransferCode] = useState("");
  const [busy, setBusy] = useState(false);
  const { reload } = useAuth();
  const nav = useNavigate();
  const loc = useLocation() as { state?: { from?: string }; search?: string };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    /** 登录一次；需要时由调用方决定是否重试。 */
    const post = (extra: Record<string, unknown>) =>
      api.post("/api/v1/auth/login", { username, password, ...extra });

    try {
      try {
        await post(needsTransfer ? { transfer_code: transferCode } : {});
      } catch (ex) {
        /* 已有活跃会话时**先自动接管一次**，因为"是否需要交接码"由服务端按实际状态判定：
         *   · 管理员换机、签到阶段、题间过渡、原设备静默超过 1 分钟 → 服务端会直接放行，
         *     这一层重试就悄悄完成了登录，用户根本不该看到"已有活跃会话"这种提示；
         *   · 只有"另一台设备**正在答题**"才会继续返回 409，那时才显示交接码输入框。
         * 以前是让用户自己点"强制接管"，等于把服务端的判断推给现场的人去猜。 */
        const code = ex instanceof ApiError ? ex.code : "";
        const needsIt = ex instanceof ApiError
          && (ex.details as { needs_transfer?: boolean } | undefined)?.needs_transfer === true;
        if (code === "SESSION_ACTIVE" && !needsIt && !transferCode) {
          await post({ force: true });     // 服务端认为无需交接 → 直接接管
        } else {
          throw ex;
        }
      }
      reload();
      // 支持 ?next=<path>：从答题页/观战页被送来登录时，登完回到原处继续，
      // 而不是一律丢回首页（现场选手被弹出去登录后再找回来会很慌）。
      const next = new URLSearchParams(loc.search).get("next");
      nav(next && next.startsWith("/") ? next : (loc.state?.from ?? "/"));
    } catch (ex) {
      if (ex instanceof ApiError && ex.code === "SESSION_ACTIVE") setNeedsTransfer(true);
      else if (ex instanceof ApiError) setErr(ex.message);
      else setErr("网络错误，请重试。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="player narrow">
      <h1>登录</h1>
      <p className="lead mt-2">
        账号由管理员统一创建。一个账号同一时间只在一台设备活跃；比赛中换机需要原设备交接码，或由主持人核验后处理。
      </p>

      {err && <div className="error-box" role="alert">{err}</div>}

      <form onSubmit={submit} className="mt-5">
        <div className="field">
          <label htmlFor="u">用户名</label>
          <input id="u" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </div>
        <div className="field">
          <label htmlFor="p">密码</label>
          <input id="p" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </div>
        {needsTransfer && (
          <div className="field">
            <label htmlFor="t">交接码（原设备在答题页生成）</label>
            <input id="t" value={transferCode} onChange={(e) => setTransferCode(e.target.value)} placeholder="8 位交接码" />
          </div>
        )}
        <button className="btn btn-primary btn-lg" type="submit" style={{ width: "100%" }} disabled={busy}>
          {busy ? "登录中…" : "登录"}
        </button>
      </form>

      <p className="note mt-4">
        还没有账号？<a href="/register">注册一个</a>（需管理员开放注册）。
      </p>

      {needsTransfer && (
        <p className="note mt-4">
          这台账号的<b>另一台设备正在答题</b>（答题中的账号不能在两台设备上同时作答）。
          请在原设备生成一次性交接码后在此输入；或联系主持人核验后由管理员吊销旧会话。
          若原设备已经不用了（关掉页面超过 1 分钟），直接重新点「登录」即可。
        </p>
      )}
    </div>
  );
}
