import { useLayoutEffect, useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { flushSync } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { HouseDetail as HD, HomeData } from "../api/types";
import { withViewTransition } from "../design/motion";
import Reveal from "../components/Reveal";
import PaperMaterial from "../design/PaperMaterial";
import { HOUSE_ICON, IconArrowLeft } from "../design/icons";

/** House 详情：由首页卡片「连续展开」而来（共享水墨材质 house-hero）。
 *  背景水墨与卡片同源（同一套 --ink-* 与形状），因此展开过程中材质是延续的； */
export default function HouseDetail() {
  const { key = "" } = useParams();
  const nav = useNavigate();
  const identity = useQueryClient().getQueryData<HomeData>(["home"])?.houses.find((h) => h.key === key);
  const heroRef = useRef<HTMLDivElement>(null);
  const busy = useRef(false);
  const { data, isLoading } = useQuery({ queryKey: ["house", key], queryFn: () => api.get<HD>(`/api/v1/houses/${key}`) });

  useLayoutEffect(() => {
    const surface = heroRef.current?.querySelector<HTMLElement>(".ink-mat");
    surface?.style.setProperty("view-transition-name", "house-hero");
    return () => { surface?.style.removeProperty("view-transition-name"); };
  }, []);

  const back = () => {
    if (busy.current) return;
    busy.current = true;
    heroRef.current?.querySelector<HTMLElement>(".ink-mat")?.style.setProperty("view-transition-name", "house-hero");
    void withViewTransition(() => { flushSync(() => nav("/"));
      if (!sessionStorage.getItem("home-scroll")) window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior }); }, "collapse").finally(() => { busy.current = false; });
  };

  const Icon = HOUSE_ICON[key] ?? HOUSE_ICON.dragon;
  const hasReal = !!data && data.history.length > 0 && !data.demo;

  return (
    <div className={`house-${key}`}>
      {/* 主视觉：承接首页卡片的水墨材质与院色 */}
      <section className="hero has-ink" ref={heroRef}>
        <PaperMaterial house={key} variant="hero" />
        <div className="hero-inner">
          <div className="wrap-inner">
            <span className="hero-en">{data?.name_en ?? identity?.name_en ?? "HOUSE"}</span>
            <h1 >
              <span className="house-badge" style={{ display: "inline-grid", verticalAlign: "middle", marginRight: 14 }}>
                <Icon size={22} />
              </span>
              {data?.name_cn ?? identity?.name_cn ?? "学院档案"}
            </h1>
            <p className="hero-note">{data?.note ?? "正在读取该院已公布成绩…"}</p>
          </div>
        </div>
      </section>

      <div className="container page detail-enter">
        <div className="spread">
          <button className="btn btn-sm" onClick={back}>
            <IconArrowLeft size={15} /> 返回首页
          </button>
          <span className="push tag">
            {/* 只去掉自检口吻的"真实"二字（暗示别处可能有假数据）；"成绩版本"是术语，按要求保留 */}
            {hasReal ? "本页数据来自已公布的成绩版本" : "本页暂无已公布成绩"}
          </span>
        </div>

        {isLoading || !data ? (
          <>
            <div className="stat-grid">
              {[0, 1, 2, 3].map((i) => (
                <div className="stat" key={i}><span className="k">加载中…</span><div className="v">—</div></div>
              ))}
            </div>
            <div className="panel mt-5" style={{ minHeight: 180 }} />
          </>
        ) : (
          <>
            <div className="stat-grid">
              <div className="stat">
                <span className="k">已公布期数</span>
                <div className="v num">{data.matches_count}<span className="unit">场</span></div>
              </div>
              <div className="stat">
                <span className="k">夺冠次数</span>
                <div className="v num">{data.championships}<span className="unit">次</span></div>
                <div className="s">并列冠军不计入</div>
              </div>
              <div className="stat">
                <span className="k">已公布场次平均分</span>
                <div className="v num">{data.avg_score === null ? "—" : data.avg_score}</div>
                <div className="s">House 平均分口径</div>
              </div>
              <div className="stat">
                <span className="k">参与人数</span>
                <div className="v">见各场次</div>
                <div className="s">人数较少时不展示个人明细</div>
              </div>
            </div>

            <div className="panel mt-5">
              <h2 className="subsection-title">历届已公布成绩</h2>
              {data.history.length === 0 ? (
                <p className="lead mt-3">
                  {/* 原文是「此处不生成示例名次，以免与真实记录混淆」—— 那是说给复核者听的
                      （解释"为什么这里是空的"），学生读到只会想"难道本来会放假名次"。
                      空态只需要说清"现在没有"和"什么时候会有"。 */}
                  该院还没有已公布的成绩。比赛揭晓后，成绩会出现在这里。
                </p>
              ) : (
                <div className="table-scroll mt-3">
                  <table className="table">
                    <thead>
                      <tr><th>场次</th><th>公布时间</th><th>House 分</th><th>名次</th><th>参与人数</th><th>版本</th></tr>
                    </thead>
                    <tbody>
                      {data.history.map((h) => (
                        <tr key={`${h.match_id}-${h.version_no}`}>
                          <td>
                            <a href={`/matches/${h.match_id}/results`}>{h.title}</a>
                            {h.is_demo && <> <span className="tag-box">演示</span></>}
                          </td>
                          <td className="num">{h.published_at?.slice(0, 16).replace("T", " ")}</td>
                          <td className="num">{h.score.toFixed(2)}</td>
                          <td className="num">第 {h.rank}{h.is_tie ? "（并列）" : ""}</td>
                          <td className="num">{h.participants}</td>
                          <td className="num">v{h.version_no}{!h.is_official && "（非正式）"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <p className="note mt-4">
              历史成绩来自冻结的成绩版本：题库修改、学院更名都不会改写已公布的赛果；赛后更正会生成新版本并在上表标注。
            </p>
          </>
        )}
      </div>
    </div>
  );
}
