// 后端接口的数据形状（与 FastAPI 的响应模型对应）。

export interface User {
  user_id: string
  email: string
  deletion_requested_at: string | null
  deletion_deadline: string | null
}

export interface AgentTextAttachment {
  kind?: 'text' | 'jd' | 'ocr'
  name?: string | null
  text: string
}

export interface AgentMessageInput {
  client_message_id: string
  content?: string
  attachments?: AgentTextAttachment[]
}

export interface AgentActionCard {
  action_id: string
  run_id: string
  tool: string
  kind: 'question' | 'confirmation'
  status: 'pending' | 'confirmed' | 'rejected' | 'executed' | 'failed' | 'undone'
  args: Record<string, unknown>
}

export interface AgentMessageConflict {
  detail: {
    message: string
    run_id: string
    pending_action: AgentActionCard | null
  }
}

// ---------- 工作台汇总 ----------

export interface FactsOverview {
  published_count: number
  version: number | null
  pending_count: number
}

export interface GroundingOverview {
  total: number
  passed: number
  fallback: number
  pending: number
  resume_count: number
}

export interface JobMatchSummary {
  hard_covered: number
  hard_total: number
  expression_gap: number
}

export interface BoardJobCard {
  job_id: string
  title: string | null
  company: string | null
  salary: string | null
  location: string | null
  onsite_days_per_week: string | null
  education: string | null
  stage: JobStage
  match_summary: JobMatchSummary | null
  updated_at: string
  resume_stale: boolean
}

export interface JobBoard {
  unmatched: BoardJobCard[]
  matched: BoardJobCard[]
  tailored: BoardJobCard[]
  applied: BoardJobCard[]
}

export interface Overview {
  facts: FactsOverview
  grounding: GroundingOverview
  board: JobBoard
}

// ---------- 事实库 ----------

export type FactKind = 'action' | 'technology' | 'result' | 'role'
export type Responsibility = 'lead' | 'own' | 'participate' | 'na'
export type ItemStatus = 'pending' | 'confirmed' | 'deleted'
export type SectionKind = 'education' | 'project' | 'internship' | 'skill' | 'award' | 'other'
export type CriterionField = 'onsite_days_per_week' | 'internship_months' | 'earliest_start'
export type DegreeLevel = 'associate' | 'bachelor' | 'master' | 'phd'
export type EducationStatus = 'enrolled' | 'completed'

export interface PublishedCriterion {
  criterion_id: string
  field: CriterionField
  int_value: number | null
  text_value: string | null
  source: 'resume' | 'user'
  source_doc_id: string | null
  source_quote: string | null
  span_start: number | null
  span_end: number | null
}

export interface DraftCriterion {
  field: CriterionField
  status: ItemStatus
  int_value: number | null
  text_value: string | null
  source: 'resume' | 'user'
  source_doc_id: string | null
  source_quote: string | null
  span_start: number | null
  span_end: number | null
  quote_verified: boolean
  review_reasons: string[]
}

export interface PublishedEducation {
  record_id: string
  education_id: string
  school: string
  major: string | null
  degree: DegreeLevel
  status: EducationStatus
  start_text: string | null
  end_text: string | null
  source: 'resume' | 'user'
  source_doc_id: string | null
  source_quote: string | null
  span_start: number | null
  span_end: number | null
}

export interface DraftEducation {
  education_id: string
  draft_status: ItemStatus
  school: string
  major: string | null
  degree: DegreeLevel
  status: EducationStatus | null
  start_text: string | null
  end_text: string | null
  source: 'resume' | 'user'
  source_doc_id: string | null
  source_quote: string | null
  span_start: number | null
  span_end: number | null
  quote_verified: boolean
  review_reasons: string[]
}

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
  criteria: PublishedCriterion[]
  education: PublishedEducation[]
  draft: {
    base_fact_set_id: string | null
    items: DraftItem[]
    criteria: DraftCriterion[]
    education: DraftEducation[]
    revision: number
  }
}

export interface UploadedResume {
  doc_id: string
  run_id: string
  items: DraftItem[]
  criteria?: DraftCriterion[]
  education?: DraftEducation[]
  revision: number
}

// ---------- 岗位 ----------

export type JobStage = 'saved' | 'applied' | 'interviewing' | 'offer' | 'rejected' | 'archived'
export type RequirementType = 'hard' | 'preferred'

export interface Requirement {
  requirement_id: string
  type: RequirementType
  text: string
  source_quote?: string | null
  quote_verified?: boolean
}

export interface RequirementChange {
  requirement_id?: string | null
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

export interface InjectionFlag {
  rule: string
  match: string
  context: string
  start: number
  end: number
}

export interface Job {
  job_id: string
  stage: JobStage
  note: string | null
  raw_text: string
  parsed: {
    basic_fields: BasicFields
    requirements: Requirement[]
    duties: { text: string }[]
    keywords: string[]
  }
  overridden: string[]
  overridden_basic_fields: string[]
  injection_flags: InjectionFlag[]
  created_at: string
  updated_at: string
}

export interface JobChanges {
  stage?: JobStage
  note?: string | null
  basic_fields?: Partial<BasicFields>
  requirements?: RequirementChange[]
  keywords?: string[]
}

export interface JobSummary {
  job_id: string
  title: string | null
  company: string | null
  salary: string | null
  location: string | null
  onsite_days_per_week: string | null
  education: string | null
  stage: JobStage
  keywords: string[]
  injection_count: number
  match_summary: JobMatchSummary | null
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

export interface CitedCriterion {
  ref_id: string
  kind: 'criterion' | 'education'
  label: string
  text: string
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
    criteria?: CitedCriterion[]
    decision_source?: 'model' | 'program'
    review_note?: string | null
    reason: string
    suggestion: string
  }[]
  created_at: string
}

export type MatchSummary = Omit<MatchReport, 'items'>

// ---------- 改写与定制简历 ----------

export type RunStatus =
  'running' | 'waiting_confirmation' | 'succeeded' | 'failed' | 'cancelled' | 'budget_exhausted'

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
  focus: {
    text: string
    type: RequirementType
    base_status: 'written' | 'suggest_add' | 'to_check'
  }[]
}

export interface RunBullet {
  bullet_id: string
  section: string
  experience: string | null
  order_no: number
  text: string
  check_status: string
  revision_count: number
  issues: { kind: string; message: string; fragment?: string | null }[]
  fact_ids: string[]
}

export interface TailorRun {
  run_id: string
  job_id: string | null
  base_doc_id: string | null
  fact_set_id: string | null
  status: RunStatus
  stop_reason: string | null
  started_at: string
  finished_at: string | null
  blocks: PlanBlock[]
  bullets: RunBullet[]
}

export type CheckLayers = 'full' | 'program'
export type CheckStatus = 'passed' | 'fallback' | 'pending'

export interface GroundingIssue {
  kind: string
  message: string
  fragment: string | null
}

export interface BulletCheck {
  text: string
  layers: CheckLayers
  issues: GroundingIssue[]
}

export interface ResumeBullet {
  bullet_id: string
  text: string
  fact_ids: string[]
  origin: CheckStatus
  user_choice?: 'use_facts' | null
  edited: boolean
  check: BulletCheck
}

export interface ResumeEntry {
  entry_id: string
  experience: string | null
  heading: string
  title: string
  time: string | null
  lines: string[]
  bullets: ResumeBullet[]
}

export interface ResumeSection {
  title: string | null
  kind: SectionKind
  entries: ResumeEntry[]
}

export interface CitedFact {
  experience: string
  text: string
  source: 'resume' | 'user'
  source_quote: string | null
}

export interface VersionSummary {
  version_id: string
  version: number
  created_at: string
}

export interface TailoredResumeData {
  resume_id: string
  run_id: string
  version_id: string
  version: number
  current_version_id: string
  versions: VersionSummary[]
  content: {
    header: string[]
    sections: ResumeSection[]
    style: { font: string; size_pt: number; leading: number }
  }
  facts: Record<string, CitedFact>
}

export type TailoredResumeStatus = 'generated' | 'edited' | 'needs_attention'

export interface TailoredResumeSummary {
  resume_id: string
  run_id: string
  job_id: string | null
  job_title: string | null
  job_company: string | null
  fact_set_version: number | null
  latest_fact_set_version: number | null
  current_version: number
  status: TailoredResumeStatus
  updated_at: string
}
