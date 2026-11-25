// 后端接口的数据形状（与 FastAPI 的响应模型对应，只列前端用到的字段）。

export interface User {
  user_id: string
  email: string
}

// ---------- 事实库 ----------

export type FactKind = 'action' | 'technology' | 'result' | 'role'
export type Responsibility = 'lead' | 'own' | 'participate' | 'na'
export type ItemStatus = 'pending' | 'confirmed' | 'deleted'
export type SectionKind = 'education' | 'project' | 'internship' | 'skill' | 'award' | 'other'

export interface DraftItem {
  lineage_id: string
  status: ItemStatus
  experience: string
  section: SectionKind | null
  kind: FactKind
  responsibility: Responsibility
  text: string
  source: 'resume' | 'user'
  source_quote: string | null
  quote_verified: boolean
  review_reasons: string[]
}

export interface PublishedFact {
  fact_id: string
  lineage_id: string
  experience: string
  kind: FactKind
  responsibility: Responsibility
  text: string
  source: string
  source_quote: string | null
}

export interface Library {
  fact_set: { fact_set_id: string; version: number } | null
  facts: PublishedFact[]
  draft: { base_fact_set_id: string | null; items: DraftItem[]; revision: number }
}

export interface UploadedResume {
  doc_id: string
  run_id: string
  items: DraftItem[]
  revision: number
}

// ---------- 岗位 ----------

export type JobStage = 'saved' | 'applied' | 'interviewing' | 'offer' | 'rejected' | 'archived'
export type RequirementType = 'hard' | 'preferred'

export interface Requirement {
  requirement_id: string
  type: RequirementType
  text: string
}

export interface BasicFields {
  company: string | null
  title: string | null
  location: string | null
  salary: string | null
  onsite_days_per_week: string | null
  internship_duration: string | null
  education: string | null
  graduation_window: string | null
  application_deadline: string | null
}

export interface Job {
  job_id: string
  stage: JobStage
  raw_text: string
  parsed: {
    basic_fields: BasicFields
    requirements: Requirement[]
    duties: { text: string }[]
    keywords: string[]
  }
  injection_flags: unknown[]
  created_at: string
}

export interface JobSummary {
  job_id: string
  title: string | null
  company: string | null
  stage: JobStage
  keywords: string[]
  injection_count: number
  updated_at: string
}

// ---------- 匹配 ----------

export type MatchVerdict = 'covered' | 'expression_gap' | 'partial' | 'missing'

export interface Coverage {
  total: number
  covered: number
  expression_gap: number
  partial: number
  missing: number
}

export interface MatchReport {
  report_id: string
  fact_set_version: number
  hard_summary: Coverage
  preferred_summary: Coverage
  main_gaps: string[]
  items: {
    requirement_id: string
    requirement_type: RequirementType
    requirement_text: string
    verdict: MatchVerdict
    facts: { fact_id: string; experience: string; text: string }[]
    reason: string
    suggestion: string
  }[]
  created_at: string
}

export type MatchSummary = Omit<MatchReport, 'items'>

// ---------- 改写 ----------

export type RunStatus =
  | 'running'
  | 'waiting_confirmation'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'budget_exhausted'

export interface TailorRunSummary {
  run_id: string
  status: RunStatus
  stop_reason: string | null
  started_at: string
  finished_at: string | null
  has_resume: boolean
}

export interface PlanBlock {
  key: string
  experience: string | null
  status: 'done' | 'interrupted' | 'failed' | 'not_started'
  focus: { text: string; type: RequirementType; base_status: 'written' | 'suggest_add' | 'to_check' }[]
}

export interface TailorRun {
  run_id: string
  status: RunStatus
  stop_reason: string | null
  blocks: PlanBlock[]
  bullets: { bullet_id: string; experience: string | null; text: string; check_status: string }[]
}
