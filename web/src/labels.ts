// 后端枚举值对应的界面文字，集中放一处，各页面共用。
import type {
  ApplicationStage,
  CriterionField,
  DegreeLevel,
  EducationStatus,
  FactKind,
  ItemStatus,
  JobMatchSummary,
  JobStage,
  MatchVerdict,
  Responsibility,
  RunStatus,
  SectionKind,
  TailoredResumeStatus,
} from './api/types'

export const APPLICATION_STAGE: Record<ApplicationStage, string> = {
  applied: '已投递',
  written_test: '笔试',
  interviewing: '面试中',
  offer: '已拿 offer',
  rejected: '未通过',
}

export const JOB_STAGE: Record<JobStage, string> = {
  saved: '已收藏',
  ...APPLICATION_STAGE,
  archived: '已归档',
}

export const FACT_KIND: Record<FactKind, string> = {
  action: '做了什么',
  technology: '技术',
  result: '结果或数字',
  role: '角色',
}

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
  education_status_uncertain: '需要核对就读状态（在读或已取得）',
}

export const CRITERION_LABEL: Record<CriterionField, string> = {
  onsite_days_per_week: '每周可到岗天数',
  internship_months: '可实习月数',
  earliest_start: '最早到岗时间',
  target_direction: '目标方向',
  preferred_cities: '意向城市',
}

export const DEGREE_LABEL: Record<DegreeLevel, string> = {
  associate: '大专',
  bachelor: '本科',
  master: '硕士',
  phd: '博士',
}

export const EDUCATION_STATUS_LABEL: Record<EducationStatus, string> = {
  enrolled: '在读',
  completed: '已取得',
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

export const INJECTION_RULE: Record<string, string> = {
  ignore_instructions: '要求忽略原有规则或指令',
  role_play: '试图让模型扮演新角色',
  addressing_ai: '直接向 AI 或自动化工具发指令',
  system_prompt: '提及系统提示词或开发者模式',
  write_into_output: '试图在输出或简历中硬塞内容',
  data_tag: '试图伪造数据分隔标记',
  chat_role_marker: '包含伪造的对话角色前缀',
}

export const TAILORED_RESUME_STATUS: Record<TailoredResumeStatus, { text: string; color: string }> =
  {
    generated: { text: '已生成', color: 'default' },
    edited: { text: '已手动修改', color: 'warning' },
    needs_attention: { text: '有待处理条目', color: 'error' },
  }

export function formatDateTime(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(parsed)
}

export function formatMatchSummary(summary: JobMatchSummary | null): string {
  if (!summary) return '还没匹配'
  const base = `硬性 ${summary.hard_covered}/${summary.hard_total}`
  return summary.expression_gap > 0 ? `${base} · 表达缺口 ${summary.expression_gap}` : base
}

export function jobMetaParts(fields: {
  company: string | null
  location: string | null
  onsite_days_per_week?: string | null
  education?: string | null
  salary?: string | null
}): string[] {
  const parts = [
    fields.company,
    fields.location,
    fields.onsite_days_per_week,
    fields.education,
    fields.salary,
  ].filter((item): item is string => Boolean(item))
  return parts.length > 0 ? parts : ['公司未填写']
}

export function formatJobMeta(fields: {
  company: string | null
  location: string | null
  onsite_days_per_week: string | null
  education: string | null
}): string {
  return jobMetaParts(fields).join(' · ')
}

// 对话 Agent 的工具在进度条里的名字；没列出的工具直接显示工具名。
export const AGENT_TOOL: Record<string, string> = {
  list_jobs: '读取岗位库',
  read_job: '读取岗位详情',
  read_facts: '读取经历事实',
  read_match_report: '读取匹配报告',
  read_resume: '读取定制简历',
  parse_jd: '解析 JD',
  match_job: '匹配岗位',
  tailor_resume: '生成定制简历',
  set_current_job: '设置当前岗位',
  edit_resume_item: '修改简历条目',
  ask_user: '向你提问',
  record_application: '记录投递',
  remember: '记住偏好',
  set_writing_preference: '设置写作偏好',
  forget: '忘掉偏好',
  read_conversation_history: '查阅更早的对话',
}

export const toolLabel = (tool: string): string => AGENT_TOOL[tool] ?? tool
