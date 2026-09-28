import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
/* 引入顺序即层叠顺序：token（值）→ app（布局）→ motion（时间与运动）。
   三者职责分开，页面样式不得再写临时覆盖。 */
import "./design/tokens.css";
import "./design/app.css";
import "./design/motion.css";
import "./design/home.css";
import "./design/feedback.css";
import "./design/match-layout.css";
import { GRAIN_URL, SOFT_NOISE_URL, GRAIN_TILE } from "./design/grain";

// 噪点底纹以 CSS 变量注入（一处定义，全局复用，无重复 data-URI 字符串）
const root = document.documentElement;
root.style.setProperty("--grain-url", GRAIN_URL);
root.style.setProperty("--soft-noise-url", SOFT_NOISE_URL);
root.style.setProperty("--grain-tile", `${GRAIN_TILE}px`);

const qc = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5000 } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
