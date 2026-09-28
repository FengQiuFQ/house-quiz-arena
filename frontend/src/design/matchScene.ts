/** 观赛与投屏共用阶段文案；比赛状态优先于可能滞留的回合数据。 */
export function matchScene(status: string, round?: { position: number | null; total: number; phase: string } | null) {
  const pregame = status === "lobby" || status === "draft";
  const active = status === "running" && !!round;
  if (active) return { pregame, active, title: `第 ${round.position ?? "—"} 题`, label: round.phase === "open" || round.phase === "answering" ? "正在作答" : "读题时间", note: `共 ${round.total} 题 · 提交进度实时更新` };
  const scenes: Record<string, [string, string, string]> = {
    draft: ["赛前准备", "筹备中", "比赛尚未开放，请等待主持人通知。"],
    lobby: ["等待开赛", "签到进行中", "参赛同学请先完成签到，开赛后答题页会自动进入比赛。"],
    running: ["准备下一题", "比赛进行中", "本题已结束，请稍候。下一题由主持人开启。"],
    paused: ["比赛暂停", "等待恢复", "请留在当前页面，主持人恢复比赛后将自动更新。"],
    locked: ["作答结束", "正在结算", "本场答题环节已结束，成绩将在统一揭晓后显示。"],
    calculated: ["等待揭晓", "结算完成", "成绩已完成结算，等待主持人安排统一公布。"],
    reveal_scheduled: ["等待揭晓", "公布已安排", "到达公布时间后，成绩将在本页自动显示。"],
    revealed: ["成绩已揭晓", "比赛结束", "本场最终名次已公布，可查看完整结果。"],
    archived: ["成绩已揭晓", "比赛已归档", "本场比赛已归档，可查看完整结果。"],
    cancelled: ["本场已取消", "比赛取消", "本场比赛不再继续，请返回首页查看其他活动。"],
  };
  const [title, label, note] = scenes[status] ?? ["同步比赛状态", "正在更新", "正在读取比赛信息，请稍候。"];
  return { pregame, active, title, label, note };
}
