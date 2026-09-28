import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import type { Me } from "./api/types";
import { withViewTransition, initLiquidPress, initVisibilityPause, applyLevel, detectLevel, applyDisplay, readModeFromLocation, rememberMode, modeToHref, type MotionLevel, type UiMode, type VtKind } from "./design/motion";
import Layout from "./components/Layout";
import Home from "./pages/Home";
import HouseDetail from "./pages/HouseDetail";
import Login from "./pages/Login";
import Register from "./pages/Register";
import Account from "./pages/Account";
import Practice from "./pages/Practice";
import LeaderboardPage from "./pages/Leaderboard";
import Play from "./pages/Play";
import Screen from "./pages/Screen";
import Watch from "./pages/Watch";
import Replay from "./pages/Replay";
import Results from "./pages/Results";
import Admin from "./pages/Admin";

interface AuthCtx {
  me: Me | null;
  loading: boolean;
  reload: () => void;
  motion: MotionLevel;
  setMotion: (l: MotionLevel) => void;
  /** 双模式：daily 日常操练 / comp 正式竞赛。由 URL 参数 → localStorage 逐级决定。 */
  mode: UiMode;
  setMode: (m: UiMode) => void;
}
interface NavCtx { vtNav: (to: string, dir?: VtKind) => void; busy: () => boolean }

const AuthContext = createContext<AuthCtx>({ me: null, loading: true, reload: () => {}, motion: "full", setMotion: () => {}, mode: "daily", setMode: () => {} });
const NavContext = createContext<NavCtx>({ vtNav: () => {}, busy: () => false });
export const useAuth = () => useContext(AuthContext);
export const useVTNav = () => useContext(NavContext);

function RequireRole({ roles, children }: { roles: string[]; children: JSX.Element }) {
  const { me, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="container page loading-screen">加载中…</div>;
  if (!me) return <Navigate to="/login" state={{ from: loc.pathname }} replace />;
  if (!roles.includes(me.role)) return <div className="container page"><div className="error-box">权限不足。</div></div>;
  return children;
}

/** 强制改密门（B1-E7）：服务端已经在业务接口上返回 403 PASSWORD_CHANGE_REQUIRED，
 *  这里再拦一层，让用户看到「为什么」而不是撞到一个错误码。 */
function RequirePasswordSet({ children }: { children: JSX.Element }) {
  const { me, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="container page loading-screen">加载中…</div>;
  if (me?.must_change_password && loc.pathname !== "/account") {
    return <Navigate to="/account" replace />;
  }
  return children;
}

export default function App() {
  const nav = useNavigate();
  const loc = useLocation();
  const busyRef = useRef(false);
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["me"],
    queryFn: () => api.get<Me>("/api/v1/auth/me").catch(() => null),
  });
  /* 顶栏需要知道"现在是否有比赛"，用于模式自动判定与"进行中"提示点。
     与首页共用同一个 query key（["home"]），React Query 会去重，不会多发请求。 */
  const { data: homeData } = useQuery({
    queryKey: ["home"],
    queryFn: () => api.get<{ live_match: { match_id: string; title: string; status: string } | null }>("/api/v1/home").catch(() => null),
    refetchInterval: 10000,
  });
  const [motionState, setMotionState] = useState<MotionLevel>(() => {
    const v = document.documentElement.dataset.motion as MotionLevel | undefined;
    return v ?? "full";
  });
  const setMotion = useCallback((l: MotionLevel) => {
    applyLevel(l);
    setMotionState(l);
  }, []);
  /* 显示档位已下线（见 design/motion.ts 的说明）：不再有"常规 / 希沃大屏"这个开关，
     页面固定用标准尺寸。这里只调用一次 applyDisplay()，把历史上可能存在的
     mgus-display="large" 记录清掉并固定标准档 —— 否则以前在希沃上被自动写成 large
     的机器升级后仍会读回大屏档，看起来像"取消了没生效"。 */
  useEffect(() => { applyDisplay(); }, []);

  /* 双模式：URL 参数优先（可分享/可投屏），其次 localStorage，最后按"后台是否已开赛"自动判定。
     自动判定的依据来自 home 接口的 live_match——所以初始值先给 daily，拿到数据后再纠正。 */
  const [modeState, setModeState] = useState<UiMode>(
    () => readModeFromLocation(window.location.search, false),
  );
  const modeTouched = useRef(false);
  const setMode = useCallback((m: UiMode) => {
    modeTouched.current = true;
    rememberMode(m);
    setModeState(m);
  }, []);
  useEffect(() => {
    if (modeTouched.current) return;
    if (new URLSearchParams(window.location.search).get("mode")) return;
    const live = !!homeData?.live_match;
    const auto = readModeFromLocation("", live);
    setModeState((cur) => (cur === auto ? cur : auto));
  }, [homeData?.live_match]);

  /* 路由 → 模式的单向同步。
     修的是这个真实缺陷：从竞赛模式点"去练习"跳到 /practice，
     页面已经是日常内容，但右上角**仍高亮"竞赛"** —— 指示器与实际内容不一致。
     规则按**模块归属**判定，而不是按"上次点过哪个按钮"：
       /practice*  → 日常（练习只存在于日常模式）
       /matches/*  → 竞赛（答题/观战/大屏/结果都属于竞赛）
       其它        → 不动（首页由用户显式选择；登录/注册/后台不改变模式）
     setMode 只更新状态与 localStorage、不触发跳转，因此不会形成回环。 */
  useEffect(() => {
    const p = loc.pathname;
    let want: UiMode | null = null;
    if (p.startsWith("/practice")) want = "daily";
    else if (p.startsWith("/matches/")) want = "comp";
    if (want && want !== modeState) setMode(want);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc.pathname]);

  /* 模式决定**整套视觉身份**（暖色竞赛 / 冷色日常）。
     做法：把模式写到 <html data-mode>，由 CSS 重映射语义 token（组件无需知道模式）。
     关键判断：**只有竞赛模式的首页才套暖色皮肤** ——
     答题页、观战页、投屏大屏在比赛期间保持默认冷色，
     否则"切模式"会把正在投屏的比赛页面一起改色，现场会非常混乱。 */
  useEffect(() => {
    const root = document.documentElement;
    const compHome = modeState === "comp" && loc.pathname === "/";
    if (compHome) root.dataset.mode = "comp";
    else delete root.dataset.mode;
  }, [modeState, loc.pathname]);

  useEffect(() => {
    const lvl = detectLevel();
    applyLevel(lvl);
    setMotionState(lvl);
    // 显示档已下线：固定标准尺寸（见 design/motion.ts）
    applyDisplay();
    const offPress = initLiquidPress();
    const offVis = initVisibilityPause();
    return () => { offPress(); offVis(); };
  }, []);

  /** 统一页面过渡入口：深入=forward、返回=back、同级=sibling、共享元素=expand/collapse。
   *  转场中再次点击 → 取消而非排队。 */
  const vtNav = useCallback(
    (to: string, dir: VtKind = "forward") => {
      if (busyRef.current) return;
      busyRef.current = true;
      void withViewTransition(() => { flushSync(() => nav(to)); }, dir).finally(() => { busyRef.current = false; });
    },
    [nav],
  );

  const scene = useMemo(() => {
    const h = loc.pathname.match(/^\/houses\/(\w+)/);
    if (h) return `house:${h[1]}`;
    if (loc.pathname.includes("/results")) return "results";
    if (loc.pathname.includes("/screen")) return "screen";
    if (loc.pathname.includes("/play")) return "play";
    if (loc.pathname.startsWith("/admin")) return "admin";
    if (loc.pathname.startsWith("/practice")) return "practice";
    if (loc.pathname.startsWith("/login")) return "login";
    return "home";
  }, [loc.pathname]);

  /* 比赛是否"正在进行"：只有 running / paused 才算。
     lobby（签到中）虽然是"有比赛"，但此刻还没有任何可看的内容，
     因此不配让模式切换偏离竞赛首页（实测过：把 lobby 也算作进行中，
     切「竞赛」会落到观战页而不是竞赛首页）。 */
  const liveMatchStatus = homeData?.live_match?.status ?? null;

  return (
    <AuthContext.Provider value={{ me: data ?? null, loading: isLoading, reload: () => void refetch(), motion: motionState, setMotion, mode: modeState, setMode }}>
      <NavContext.Provider value={{ vtNav, busy: () => busyRef.current }}>
        <Layout scene={scene} authed={!!data} live={!!homeData?.live_match}
          liveMatchId={homeData?.live_match?.match_id ?? null}
          liveMatchRunning={liveMatchStatus === "running" || liveMatchStatus === "paused"}>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/houses/:key" element={<HouseDetail />} />
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/account" element={<RequireRole roles={["admin", "advisor", "student"]}><Account /></RequireRole>} />
            <Route path="/practice" element={<Practice />} />
            <Route path="/leaderboard" element={<LeaderboardPage />} />
            <Route path="/matches/:matchId/play" element={<RequirePasswordSet><Play /></RequirePasswordSet>} />
            <Route path="/matches/:matchId/screen" element={<Screen />} />
            <Route path="/matches/:matchId/watch" element={<Watch />} />
            <Route path="/matches/:matchId/replay" element={<Replay />} />
            <Route path="/matches/:matchId/results" element={<Results />} />
            <Route
              path="/admin"
              element={
                <RequireRole roles={["admin"]}>
                  <RequirePasswordSet>
                    <Admin />
                  </RequirePasswordSet>
                </RequireRole>
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </NavContext.Provider>
    </AuthContext.Provider>
  );
}
