// 后端枚举值对应的界面文字，集中放一处，各页面共用。
import type { ItemStatus, MatchVerdict, Responsibility, RunStatus, SectionKind } from './api/types'

export const RESPONSIBILITY: Record<Responsibility, string> = {
  lead: '主导',
  own: '负责',
  participate: '参与',
  na: '不适用',
}

export const ITEM_STATUS: Record<ItemStatus, string> = {
  pending: '待确认',
  confirmed: '已确认',
  deleted: '待删除',
}

export const REVIEW_REASON: Record<string, string> = {
  quote_not_found: '原文里找不到这段，请修改或删除',
  experience_uncertain: '不确定属于哪段经历',
  quote_ambiguous: '原文里出现了不止一处',
  responsibility_unstated: '原文没写明职责程度',
  responsibility_unsupported: '职责程度和原文的写法对不上',
  number_not_in_quote: '数字和原文对不上',
}

export const SECTION: Record<SectionKind, string> = {
  internship: '实习',
  project: '项目',
  other: '其他经历',
  education: '教育',
  skill: '技能',
  award: '获奖',
}

export const VERDICT: Record<MatchVerdict, { text: string; color: string }> = {
  covered: { text: '已覆盖', color: 'success' },
  expression_gap: { text: '表达缺口', color: 'processing' },
  partial: { text: '部分覆盖', color: 'warning' },
  missing: { text: '未覆盖', color: 'default' },
}

export const RUN_STATUS: Record<RunStatus, string> = {
  running: '生成中',
  waiting_confirmation: '等待确认',
  succeeded: '已生成',
  failed: '生成失败',
  cancelled: '已取消',
  budget_exhausted: '额度用完，部分完成',
}

export const STOP_REASON: Record<string, string> = {
  budget_exhausted: '这次的模型额度用完了，已写好的部分保留',
  match_failed: '匹配分析没有完成',
  rewrite_failed: '有一段经历改写失败',
  service_restarted: '服务重启，这次运行被中断',
}
