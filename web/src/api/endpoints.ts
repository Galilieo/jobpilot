// 前端用到的全部接口，一个函数对应一个后端接口；路径与 02 第 6 节一致。
import { postForStream, request } from './http'
import type {
  AgentDecision,
  AgentMessageInput,
  AgentUndoResult,
  ApplicationChanges,
  ApplicationDetail,
  ApplicationDiff,
  ApplicationInput,
  ApplicationListItem,
  ApplicationRecord,
  CompactResult,
  ContextUsage,
  Conversation,
  ConversationDetail,
  CriterionField,
  DegreeLevel,
  DraftCriterion,
  DraftEducation,
  DraftItem,
  EducationStatus,
  FactKind,
  Job,
  JobChanges,
  JobSummary,
  Library,
  MatchReport,
  MatchSummary,
  MemoryCollection,
  MemoryInput,
  MemoryWriteResult,
  WritingField,
  WritingPreference,
  Overview,
  Responsibility,
  SectionKind,
  TailorRun,
  TailorRunSummary,
  TailoredResumeData,
  TailoredResumeSummary,
  UploadedResume,
  User,
} from './types'

// ---------- 账号与工作台 ----------

export const auth = {
  me: (signal?: AbortSignal) => request<User>('/api/me', { signal }),
  register: (email: string, password: string) =>
    request<Pick<User, 'user_id' | 'email'>>('/api/auth/register', { json: { email, password } }),
  login: (email: string, password: string) =>
    request<void>('/api/auth/login', { json: { email, password } }),
  logout: () => request<void>('/api/auth/logout', { method: 'POST' }),
}

export const account = {
  requestDeletion: () => request<void>('/api/account', { method: 'DELETE' }),
  restore: () => request<void>('/api/account/restore', { method: 'POST' }),
  exportUrl: '/api/account/export',
}

export const overview = {
  get: (signal?: AbortSignal) => request<Overview>('/api/overview', { signal }),
}

// ---------- 简历上传与事实库 ----------

function fileForm(field: string, files: File[]): FormData {
  const form = new FormData()
  for (const file of files) form.append(field, file)
  return form
}

export const textImport = {
  // 文件只提取文字、不保存；截图走本机文字识别。两者都要用户核对后再提交。
  extract: (file: File) =>
    request<{ text: string }>('/api/extract-text', { form: fileForm('file', [file]) }),
  ocr: (files: File[]) => request<{ text: string }>('/api/ocr', { form: fileForm('files', files) }),
}

export const resumes = {
  upload: (rawText: string, filename: string | null, idempotencyKey: string) =>
    request<UploadedResume>('/api/resumes', {
      json: { raw_text: rawText, filename },
      idempotencyKey,
    }),
}

export const facts = {
  library: (signal?: AbortSignal) => request<Library>('/api/facts', { signal }),
  add: (body: {
    experience: string
    kind: FactKind
    responsibility: Responsibility
    text: string
  }) => request<DraftItem & { revision: number }>('/api/facts', { json: body }),
  update: (
    lineageId: string,
    changes: { text?: string; responsibility?: Responsibility; confirmed?: boolean },
    expectedRevision: number,
  ) =>
    request<DraftItem & { revision: number }>(`/api/facts/${lineageId}`, {
      method: 'PUT',
      json: { ...changes, expected_revision: expectedRevision },
    }),
  remove: (lineageId: string, expectedRevision: number) =>
    request<void>(`/api/facts/${lineageId}?expected_revision=${expectedRevision}`, {
      method: 'DELETE',
    }),
  confirmClean: (expectedRevision: number) =>
    request<{ changed: number; revision: number }>('/api/facts/batch', {
      json: { action: 'confirm_clean', expected_revision: expectedRevision },
    }),
  setResponsibility: (
    experience: string,
    responsibility: Exclude<Responsibility, 'na'>,
    expectedRevision: number,
  ) =>
    request<{ changed: number; revision: number }>('/api/facts/batch', {
      json: {
        action: 'set_responsibility',
        experience,
        responsibility,
        expected_revision: expectedRevision,
      },
    }),
  commit: (baseFactSetId: string | null, expectedRevision: number) =>
    request<{ fact_set_id: string; version: number; revision: number }>('/api/facts/commit', {
      json: { base_fact_set_id: baseFactSetId, expected_revision: expectedRevision },
    }),
  updateCriterion: (body: {
    field: CriterionField
    int_value?: number | null
    text_value?: string | null
    list_value?: string[] | null
    confirmed?: boolean
    delete?: boolean
    expected_revision: number
  }) =>
    request<{ criterion: DraftCriterion | null; revision: number }>('/api/facts/criteria', {
      method: 'PUT',
      json: body,
    }),
  addEducation: (body: {
    school: string
    major?: string | null
    degree: DegreeLevel
    status: EducationStatus
    start_text?: string | null
    end_text?: string | null
    expected_revision: number
  }) => request<DraftEducation & { revision: number }>('/api/facts/education', { json: body }),
  updateEducation: (
    educationId: string,
    body: {
      school?: string
      major?: string | null
      degree?: DegreeLevel
      status?: EducationStatus
      start_text?: string | null
      end_text?: string | null
      confirmed?: boolean
      expected_revision: number
    },
  ) =>
    request<DraftEducation & { revision: number }>(`/api/facts/education/${educationId}`, {
      method: 'PUT',
      json: body,
    }),
  removeEducation: (educationId: string, expectedRevision: number) =>
    request<void>(`/api/facts/education/${educationId}?expected_revision=${expectedRevision}`, {
      method: 'DELETE',
    }),
}

// ---------- 岗位与匹配 ----------

export const jobs = {
  list: (q?: string, signal?: AbortSignal) => {
    const trimmed = q?.trim()
    const query = trimmed ? `?q=${encodeURIComponent(trimmed)}` : ''
    return request<JobSummary[]>(`/api/jobs${query}`, { signal })
  },
  get: (jobId: string, signal?: AbortSignal) => request<Job>(`/api/jobs/${jobId}`, { signal }),
  create: (rawText: string, idempotencyKey: string) =>
    request<Job>('/api/jobs', { json: { raw_text: rawText }, idempotencyKey }),
  update: (jobId: string, changes: JobChanges) =>
    request<Job>(`/api/jobs/${jobId}`, { method: 'PUT', json: changes }),
  remove: (jobId: string) => request<void>(`/api/jobs/${jobId}`, { method: 'DELETE' }),
  matches: (jobId: string, signal?: AbortSignal) =>
    request<MatchSummary[]>(`/api/jobs/${jobId}/matches`, { signal }),
  match: (jobId: string, idempotencyKey: string) =>
    request<MatchReport>(`/api/jobs/${jobId}/match`, { method: 'POST', idempotencyKey }),
  report: (reportId: string, signal?: AbortSignal) =>
    request<MatchReport>(`/api/match-reports/${reportId}`, { signal }),
}

// ---------- 改写与完整简历 ----------

export const tailoring = {
  listAll: (signal?: AbortSignal) =>
    request<TailoredResumeSummary[]>('/api/tailored-resumes', { signal }),
  runs: (jobId: string, signal?: AbortSignal) =>
    request<TailorRunSummary[]>(`/api/jobs/${jobId}/tailor-runs`, { signal }),
  start: (jobId: string, sections: Record<string, SectionKind>, idempotencyKey: string) =>
    request<{ run_id: string; base_doc_id: string }>(`/api/jobs/${jobId}/tailor`, {
      json: { sections },
      idempotencyKey,
    }),
  run: (runId: string, signal?: AbortSignal) =>
    request<TailorRun>(`/api/tailor-runs/${runId}`, { signal }),
  resume: (runId: string, version?: number, signal?: AbortSignal) => {
    const query = version !== undefined ? `?version=${version}` : ''
    return request<TailoredResumeData>(`/api/tailor-runs/${runId}/resume${query}`, { signal })
  },
  previewUrl: (runId: string, version?: number) =>
    version !== undefined
      ? `/api/tailor-runs/${runId}/preview?version=${version}`
      : `/api/tailor-runs/${runId}/preview`,
  exportUrl: (runId: string, format: 'html' | 'md' | 'docx', version?: number) => {
    const ver = version !== undefined ? `&version=${version}` : ''
    return `/api/tailor-runs/${runId}/export?format=${format}${ver}`
  },
}

// ---------- 对话 ----------

export const conversations = {
  list: (signal?: AbortSignal) => request<Conversation[]>('/api/conversations', { signal }),
  get: (conversationId: string, signal?: AbortSignal) =>
    request<ConversationDetail>(`/api/conversations/${conversationId}`, { signal }),
  create: (title: string) => request<Conversation>('/api/conversations', { json: { title } }),
  update: (conversationId: string, changes: { title?: string; current_job_id?: string | null }) =>
    request<Conversation>(`/api/conversations/${conversationId}`, {
      method: 'PUT',
      json: changes,
    }),
  remove: (conversationId: string) =>
    request<void>(`/api/conversations/${conversationId}`, { method: 'DELETE' }),
  // 下面两个返回 SSE 响应，由 readAgentStream 读。
  sendMessage: (conversationId: string, body: AgentMessageInput, signal?: AbortSignal) =>
    postForStream(`/api/conversations/${conversationId}/messages`, body, signal),
  decide: (
    conversationId: string,
    actionId: string,
    decision: AgentDecision,
    signal?: AbortSignal,
  ) => postForStream(`/api/conversations/${conversationId}/actions/${actionId}`, decision, signal),
  undo: (actionId: string) =>
    request<AgentUndoResult>(`/api/actions/${actionId}/undo`, { method: 'POST' }),
  context: (conversationId: string, signal?: AbortSignal) =>
    request<ContextUsage>(`/api/conversations/${conversationId}/context`, { signal }),
  compact: (conversationId: string, focus?: string) =>
    request<CompactResult>(`/api/conversations/${conversationId}/compact`, {
      json: focus ? { focus } : {},
    }),
}

// ---------- 长期记忆 ----------

export const memories = {
  list: (signal?: AbortSignal) => request<MemoryCollection>('/api/memories', { signal }),
  add: (body: MemoryInput) =>
    request<MemoryWriteResult>('/api/memories', { method: 'POST', json: body }),
  update: (id: string, body: MemoryInput & { expected_revision: number }) =>
    request<MemoryWriteResult>(`/api/memories/${id}`, { method: 'PUT', json: body }),
  remove: (id: string, body: { expected_revision: number }) =>
    request<void>(`/api/memories/${id}`, { method: 'DELETE', json: body }),
}

export const writingPreference = {
  get: (signal?: AbortSignal) => request<WritingPreference>('/api/writing-preference', { signal }),
  update: (body: { expected_revision: number } & Partial<Record<WritingField, string | null>>) =>
    request<WritingPreference>('/api/writing-preference', { method: 'PUT', json: body }),
}

export const applications = {
  list: (jobId?: string, signal?: AbortSignal) => {
    const filter = jobId ? `?job_id=${encodeURIComponent(jobId)}` : ''
    return request<ApplicationListItem[]>(`/api/applications${filter}`, { signal })
  },
  detail: (id: string, signal?: AbortSignal) =>
    request<ApplicationDetail>(`/api/applications/${id}`, { signal }),
  create: (body: ApplicationInput, idempotencyKey: string) =>
    request<ApplicationRecord>('/api/applications', { json: body, idempotencyKey }),
  update: (id: string, body: ApplicationChanges) =>
    request<ApplicationRecord>(`/api/applications/${id}`, { method: 'PUT', json: body }),
  remove: (id: string, revision: number) =>
    request<void>(`/api/applications/${id}`, {
      method: 'DELETE',
      json: { expected_revision: revision },
    }),
  snapshotUrl: (id: string) => `/api/applications/${id}/snapshot.html`,
  diff: (id: string, signal?: AbortSignal) =>
    request<ApplicationDiff>(`/api/applications/${id}/diff`, { signal }),
}
