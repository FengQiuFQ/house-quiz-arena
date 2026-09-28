/** 与后端契约对齐的类型（contracts/openapi.json 为准，手工同步）。 */

export interface HouseBrief {
  key: string;
  name_cn: string;
  name_en: string;
  color_light: string;
  color_deep: string;
}

export interface HomeData {
  has_published: boolean;
  demo: boolean;
  live_match: {
    match_id: string;
    title: string;
    status: string;                 // lobby / running / paused
    mode?: string;
    planned_questions?: number;
    answer_seconds?: number;
    display_seconds?: number;
    start_at?: string | null;       // 预定开赛时间（UTC ISO）；为空则首页显示"即将开始"
    checked_in?: number;
    server_time?: string;           // 用于校准倒计时，不信任本机时钟
  } | null;
  houses: Array<HouseBrief & {
    latest: { match_id: string; match_title: string; version_no: number; score: number; rank: number; is_tie: boolean; participants: number; published_at: string; is_demo: boolean } | null;
    trend: Array<{ score: number }>;
    championships: number;
  }>;
}

export interface HouseDetail extends HouseBrief {
  history: Array<{ match_id: string; title: string; published_at: string; score: number; rank: number; is_tie: boolean; participants: number; version_no: number; is_official: boolean; is_demo: boolean }>;
  championships: number;
  avg_score: number | null;
  matches_count: number;
  note: string;
  demo: boolean;
}

export interface UserOut {
  id: string;
  username: string;
  /** 角色只加能力、不拦参赛：student 绑定学院后即可代表本院上场。
   *  `entrant` 已并入 `student`（后端仍有历史别名兼容，但界面不再出现该值）。 */
  role: "admin" | "advisor" | "student";
  display_name: string | null;
  must_change_password: boolean;
  house: HouseBrief | null;
}

export interface Me extends UserOut {
  session_generation: number;
  active_participation?: { match_id: string; title: string; status: string; is_captain: boolean; display_alias: string };
}

export interface RoundPublic {
  round_instance_id: string;
  attempt_no: number;
  position: number | null;
  total: number;
  phase: "display" | "open";
  display_at: string | null;
  open_at: string | null;
  deadline_at: string | null;
  server_time: string;
  prompt: string;
  options: Array<{ option_id: string; text: string }>;
  aid_window?: { enabled: boolean; window_end: string };
}

export interface Snapshot {
  role: string;
  match: {
    id: string; title: string; status: string; mode: string; state_version: number;
    aid_enabled: boolean; buzz?: boolean;
    /** 投屏大屏（/board）额外带上的赛制信息，供大屏说清"这场在比什么"。 */
    competition_mode?: string; planned_questions?: number;
    questions_per_house?: number | null; answer_seconds?: number;
    display_seconds?: number; scoring_mode?: string; scoring_note?: string | null;
  };
  server_time: string;
  round: RoundPublic | null;
  progress: {
    houses: Array<{ key: string; name: string; color_light: string; color_deep: string; total: number; submitted: number }>;
    total_participants: number;
    total_submitted: number;
    rounds_done: number;
    aid: Record<string, { remaining: number; quota: number }>;
    connections: number | null;
  };
  /** 逐题节奏（大屏用；聚合量，不含题面与标准答案）。 */
  timeline?: { items: BoardTimelineItem[]; total: number };
  /** 最近提交流水（大屏用；匿名：只有学院、题号、时间）。 */
  recent?: Array<{ at: string | null; house_key: string; house_name: string; position: number | null }>;
  /** 提交矩阵：回合 × 学院的已交比例（大屏用；聚合量，不含题面与个人身份）。 */
  matrix?: {
    rounds: Array<{ index: number; status: string; total_submitted: number }>;
    houses: Array<{
      key: string; name: string; color_light: string; color_deep: string; total: number;
      cells: Array<{ round_index: number; submitted: number; total: number; ratio: number }>;
    }>;
    max_rounds: number;
    planned_questions: number;
  };
  reveal?: { ceremony_start_at: string; reveal_at: string };
  result?: ResultsData;
  you?: {
    user_id: string;
    role: string;
    is_admin?: boolean;
    entry?: { entry_id: string; alias: string; house_key: string; house_name: string; is_captain: boolean; checked_in: boolean };
    /** 选手流程分流（服务端判定，前端据此决定进哪一屏）：
     *  checking  尚未签到且处于签到阶段 → 签到页
     *  waiting   已签到但未开赛（lobby/paused）→ 等待室
     *  answering 比赛进行中 → 答题界面
     *  done      本场已锁定及以后 → 等待揭晓
     *  spectate  不在本场名单 → 旁观提示
     *  用服务端判定而不是前端自己拼条件，是为了让"什么状态进哪一屏"只有一个权威来源。 */
    flow?: "checking" | "waiting" | "answering" | "done" | "spectate" | "missed_checkin";
    my_answer?: { receipt_id: string; option_id: string; accepted_at: string } | null;
    aid?: { remaining: number; quota: number };
    aid_request?: { id: string; status: string; advice_option_id: string | null };
  };
}

/** 大屏"逐题节奏"的一行（服务端聚合，不含题面/标准答案）。
 *  `correct` 只在该回合**已关闭**时才有值：进行中的回合不给正确份数，
 *  否则场下可以直接喊出答案。 */
export interface BoardTimelineItem {
  round_instance_id: string;
  position: number | null;
  status: string;
  house_scope: string[] | null;
  submitted: number;
  correct: number | null;
  closed: boolean;
  open_at: string | null;
  closed_at: string | null;
  duration_ms: number | null;
}

export interface RelayTiming {
  total_ms: number | null; total_s: number | null;
  per_question_ms: number | null; per_question_s: number | null;
  answered_rounds: number; expected_rounds: number;
  time_factor: number; completed: boolean;
}

export interface ResultsData {
  match: {
    id: string; title: string; status: string;
    /** shared_buzz 抢答 / relay 接力 */
    competition_mode?: string;
    questions_per_house?: number | null;
    scoring_mode?: string;
    weight_accuracy?: number;
    weight_speed?: number;
    weight_time?: number;
    /** 计分口径的中文说明（原样展示给学生，保证每一分可追溯） */
    scoring_note?: string;
  };
  result_version: number;
  published_at: string;
  is_official: boolean;
  is_demo: boolean;
  valid_question_count: number;
  notes: string | null;
  houses: Array<{
    key: string; name: string; color_light: string; color_deep: string;
    score: number; rank: number; is_tie: boolean; participants: number;
    correct_total: number; meets_minimum: boolean; aid_used: number;
    distribution: number[];
    /** 仅接力赛：该院的总用时 / 每题用时等（抢答赛为 null） */
    timing?: RelayTiming | null;
  }>;
  speed: { median_s: number; sample_size: number; note: string } | null;
  entry_point?: string;
  published?: boolean;
}

export interface WsMessage {
  protocol_version: string;
  type: string;
  match_id: string | null;
  server_time: string;
  state_version?: number;
  progress_seq?: number;
  payload: Record<string, unknown>;
}

export interface PracticeQuestion {
  question_version_id: string;
  prompt: string;
  options: Array<{ option_id: string; text: string }>;
}
