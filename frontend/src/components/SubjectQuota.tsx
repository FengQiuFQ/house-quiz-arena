import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

/**
 * 建赛表单里的「小类（主题）与题量」编辑器。
 *
 * 为什么要把原来的自由文本框换掉：
 *   原来是 `主题蓝图` 一个输入框，要管理员手打「科学常识:2,生活与安全:1」。
 *   现场实测的问题有三个：
 *     ① 主题名要一字不差地打对（库里认的是字符串，错一个字就是"可用题不足 0"）；
 *     ② 打错了要到"点建赛"才知道，因为输入框旁边没有任何可用量提示；
 *     ③ 合计与全场题数不符时，只有服务端报一句"配额合计 3 题，与计划 4 题不符"。
 *   换成勾选 + 每题量之后，这三件事都变成"点之前就能看见"。
 *
 * 交互约定（与后端 _resolve_questions 的三种题源一一对应）：
 *   一个主题都不勾  → 不传 blueprint，后端按随机（或难度配比）抽题；
 *   勾了主题        → 传 blueprint，合计必须等于全场题数，否则不允许建赛。
 * 这两条必须**显式**呈现给管理员，否则"什么都没勾"会被误读成"抽不到题"。
 */

export interface SubjectQuotaValue {
  [subject: string]: number;
}

interface Props {
  /** 题库大类：换题库要重新拉可用量（两个池的家底完全不同） */
  pool: "official" | "practice";
  /** 全场题数（抢答＝全场题数；接力＝每院题量 × 4 院） */
  planned: number;
  value: SubjectQuotaValue;
  onChange: (v: SubjectQuotaValue) => void;
}

export default function SubjectQuota({ pool, planned, value, onChange }: Props) {
  const [bySubject, setBySubject] = useState<Array<{ subject: string; count: number }>>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [note, setNote] = useState<string | null>(null);

  /* 可用量从后端的配额预览接口取 —— 与真正抽题用的是同一段 SQL。
     为什么不在这儿自己按题算：两处各算一份必然走样，而"看着够、抽的时候不够"
     正是这个界面要消灭的问题。 */
  useEffect(() => {
    let alive = true;
    api.post<{ available_total: number; by_subject: Array<{ subject: string; count: number }> }>(
      "/api/v1/admin/matches/quota-preview",
      { planned_questions: Math.max(1, planned), question_pool: pool },
    ).then((r) => {
      if (!alive) return;
      setBySubject(r.by_subject ?? []);
      setTotal(r.available_total ?? null);
      setNote(null);
    }).catch((e) => {
      if (!alive) return;
      setBySubject([]); setTotal(null);
      setNote(e instanceof Error ? e.message : "读不到题库家底。");
    });
    return () => { alive = false; };
  }, [pool, planned]);

  const picked = Object.keys(value);
  const sum = useMemo(() => picked.reduce((s, k) => s + (Number(value[k]) || 0), 0), [value, picked]);
  const diff = planned - sum;
  const avail = useMemo(() => Object.fromEntries(bySubject.map((s) => [s.subject, s.count])), [bySubject]);
  const over = picked.filter((k) => (Number(value[k]) || 0) > (avail[k] ?? 0));

  const toggle = (subject: string) => {
    const next = { ...value };
    if (subject in next) {
      delete next[subject];
    } else {
      // 新勾上的主题先给 1 题：让"合计"立刻反映出来，管理员再按需调。
      next[subject] = 1;
    }
    onChange(next);
  };

  const setCount = (subject: string, n: number) => {
    onChange({ ...value, [subject]: Math.max(0, Math.floor(n) || 0) });
  };

  /** 把全场题数平均分给已勾选的主题，余数从前面的主题依次补 1。 */
  const spread = () => {
    if (!picked.length) return;
    const base = Math.floor(planned / picked.length);
    let rem = planned - base * picked.length;
    const next: SubjectQuotaValue = {};
    for (const k of picked) { next[k] = base + (rem > 0 ? 1 : 0); if (rem > 0) rem--; }
    onChange(next);
  };

  return (
    <div className="sq">
      <div className="sq-head">
        <span className="sq-title">小类（主题）与题量</span>
        <span className="sq-stat">
          本场共 <b className="num">{planned}</b> 题 · 已分配 <b className="num">{sum}</b> 题
          {picked.length === 0 && <em className="sq-rand">未选主题 → 按随机抽题</em>}
          {picked.length > 0 && diff !== 0 && (
            <em className="sq-bad">{diff > 0 ? `还差 ${diff} 题` : `多了 ${-diff} 题`}</em>
          )}
          {picked.length > 0 && diff === 0 && <em className="sq-good">已配平</em>}
        </span>
        <span className="sq-tools">
          <button type="button" className="btn btn-sm" onClick={spread} disabled={!picked.length}>
            平均分配
          </button>
          <button type="button" className="btn btn-sm" onClick={() => onChange({})} disabled={!picked.length}>
            全部不选
          </button>
        </span>
      </div>

      {note && <p className="mk-hint">{note}</p>}
      {total !== null && (
        <p className="mk-hint">
          这个题库里可抽的已审核题共 <b className="num">{total}</b> 题（含 20% 封存备用题由系统自动预留）。
        </p>
      )}
      {over.length > 0 && (
        <p className="sq-warn" role="alert">
          {over.map((k) => `「${k}」只有 ${avail[k] ?? 0} 题，却分配了 ${value[k]} 题`).join("；")}。
        </p>
      )}

      {/* 真勾选框，不是自绘的可点区块：
          仓库里踩过"给容器加按压动画 transform: scale(0.97) 把 20px 勾选框挪出指针范围、
          点不动"的坑（见 docs 与既有注释）。所以这里**不加任何缩放动画**，
          命中区域靠 padding 撑大，而不是靠变形。 */}
      <div className="sq-grid" role="group" aria-label="小类（主题）与题量">
        {bySubject.length === 0 && <p className="mk-hint">这个题库暂时没有可抽的题。</p>}
        {bySubject.map((s) => {
          const on = s.subject in value;
          return (
            <label key={s.subject} className={`sq-item${on ? " on" : ""}`} data-subject={s.subject}>
              <input
                type="checkbox"
                data-quota-subject={s.subject}
                checked={on}
                onChange={() => toggle(s.subject)}
              />
              <span className="sq-name">{s.subject}</span>
              <span className="sq-avail num">{s.count} 题</span>
              {on && (
                <input
                  className="sq-num"
                  type="number"
                  min={0}
                  max={s.count}
                  aria-label={`${s.subject} 题量`}
                  value={value[s.subject]}
                  onChange={(e) => setCount(s.subject, Number(e.target.value))}
                />
              )}
            </label>
          );
        })}
      </div>
    </div>
  );
}
