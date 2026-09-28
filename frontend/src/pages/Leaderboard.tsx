import { Link } from "react-router-dom";
import { LeaderboardTable } from "./Practice";
import PaperMaterial from "../design/PaperMaterial";
import Reveal from "../components/Reveal";
import { IconArrowLeft } from "../design/icons";

/**
 * 独立排行榜页（/leaderboard）。
 *
 * 为什么要有独立页，而不是让顶栏"排行榜"跳到练习页的榜单标签：
 *   ① 语义不对：点"排行榜"应该直接看到榜，而不是先进入练习页的答题流程；
 *      现场体验里这被明确反映为"点排行榜怎么跳去答题了"。
 *   ② 榜单本身就是日常模式的主视觉：四个榜（正确率/速度/连对/House 荣誉）、
 *      门槛说明、隐私口径都要有位置写清楚，塞在练习页的一个 tab 里只能挤。
 *
 * 内容与练习页里的榜单**共用同一个实现**（LeaderboardTable），避免两处走样。
 */
export default function LeaderboardPage() {
  return (
    <div className="container page">
      <div className="lb-hero has-ink">
        <PaperMaterial house="tiger" variant="stage" sheen={false} />
        <div className="lb-hero-inner">
          <p className="ch-kicker">日常操练 · 榜单</p>
          <h1 className="section-title">排行榜</h1>
          <p className="lb-hero-sub">
            四个榜看四件事：谁最准、谁最快、谁连对最多、哪个 House 练得最扎实。
            榜单每 30 秒刷新一次，累计作答满门槛即自动上榜。
          </p>
          <div className="row lb-hero-actions">
            <Link className="btn btn-primary" to="/practice?mode=daily">去练习</Link>
            <Link className="btn" to="/?mode=daily"><IconArrowLeft size={15} /> 回首页</Link>
          </div>
        </div>
      </div>

      <Reveal>
        <div className="card mt-5">
          <LeaderboardTable />
        </div>
      </Reveal>

      <p className="note mt-4">
        榜单只统计日常练习；正式比赛的学院成绩在竞赛模式的首页与结果页查看，两者口径不同，不要混看。
      </p>
    </div>
  );
}
