# WebSocket 协议（/ws）— v1

- 入口：`ws(s)://<host>/ws?match=<match_id>`；同源 Cookie 鉴权 + Origin 校验（§14.2）。
- 匿名连接只收公开事件（进度/阶段/揭晓）；参赛账号额外收个人状态；智囊收本 House 援助。
- **WS 只负责通知，不承载任何写操作**；提交答案与管理命令一律走 HTTP。

## 消息信封

```json
{
  "protocol_version": "1",
  "type": "progress.snapshot",
  "match_id": "…",
  "server_time": "2026-09-25T12:00:00Z",
  "state_version": 7,
  "progress_seq": 42,
  "payload": { }
}
```

- `state_version`：本场持久化的比赛状态版本，随状态事务递增；客户端用于比对快照新旧。
- `progress_seq`：进度快照流内单调序号；连接重连后**允许重新起序**，客户端以 HTTP 快照对齐（§14.3）。

## 事件类型

| type | 触发 | payload 关键字段 | 订阅范围 |
| --- | --- | --- | --- |
| `match.state` | 状态转换/接管 | `status, action?` | 全部 |
| `round.opened` | 回合展示/开放 | `round_id, phase: display\|open` | 全部 |
| `round.closed` | 回合关闭 | `round_id` | 全部 |
| `progress.snapshot` | 每秒批量广播 | 四 House 已交人数/比例、总参与、援助剩余、`you.submitted`（仅本人帧） | 全部（个人字段仅本人帧） |
| `aid.updated` | 求援/建议/退回 | `house, phase, advice?` | 本 House + 管理员 |
| `session.revoked` | 预留（会话吊销经 HTTP 401 生效） | — | — |
| `result.scheduled` | 安排揭晓 | `ceremony_start_at, reveal_at`（不含成绩） | 全部 |
| `result.published` | 到点发布 | `result_version_id, version_no, correction?` | 全部 |

## 客户端恢复顺序（§14.3）

1. 建立 WS 订阅（暂存其后到达的消息）；
2. `GET /api/v1/matches/{id}/snapshot` 应用权威快照；
3. 只应用 `progress_seq` 大于已应用值的消息；
4. WS 不可用但 HTTP 可用时：以 2–3 秒（带抖动）轮询快照；恢复后停止轮询。

## 时序约定

- 常规进度：每 1 秒一次批量快照（服务端每场只计算一次，逐连接合并个性化字段，§10.5）。
- 阶段切换（开题/开放/关闭/揭晓）：即时推送 + 客户端触发快照重同步。
- 慢客户端：服务端发送队列有界（64 帧），队满丢帧不阻塞；丢帧客户端靠下一帧全量快照自愈。
