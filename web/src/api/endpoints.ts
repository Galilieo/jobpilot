// 前端用到的全部接口，一个函数对应一个后端接口；路径与 02 第 6 节一致。
import { request } from './http'
import type {
  DraftItem,
  Job,
  JobSummary,
  Library,
  MatchReport,
  MatchSummary,
  Responsibility,
  SectionKind,
  TailorRun,
  TailorRunSummary,
  UploadedResume,
  User,
} from './types'

// ---------- 账号 ----------

export const auth = {
  me: (signal?: AbortSignal) => request<User>('/api/me', { signal }),
  register: (email: string, password: string) =>
    request<User>('/api/auth/register', { json: { email, password } }),
  login: (email: string, password: string) =>
    request<void>('/api/auth/login', { json: { email, password } }),
  logout: () => request<void>('/api/auth/logout', { method: 'POST' }),
}

// ---------- 简历上传与事实库 ----------

function fileForm(field: string, files: File[]): FormData {
  const form = new FormData()
  for (const file of files) form.append(field, file)
  return form
}

export const resumes = {
  // 文件只提取文字、不保存；截图走本机文字识别。两者都要用户核对后再提交。
  extract: (file: File) =>
    request<{ text: string }>('/api/resumes/extract', { form: fileForm('file', [file]) }),
  ocr: (files: File[]) =>
    request<{ text: string }>('/api/ocr', { form: fileForm('files', files) }),
  upload: (rawText: string, filename: string | null, idempotencyKey: string) =>
    request<UploadedResume>('/api/resumes', {
      json: { raw_text: rawText, filename },
      idempotencyKey,
    }),
}

export const facts = {
  library: (signal?: AbortSignal) => request<Library>('/api/facts', { signal }),
  update: (
    lineageId: string,
    changes: { text?: string; responsibility?: Responsibility; confirmed?: boolean },
    expectedRevision: number,
  ) =>
    request<DraftItem & { revision: number }>(`/api/facts/${lineageId}`, {
      method: 'PUT',
      json: { ...changes, expected_revision: expectedRevision },
    }),
  remove: (lineageId: string) => request<void>(`/api/facts/${lineageId}`, { method: 'DELETE' }),
  confirmClean: (expectedRevision: number) =>
    request<{ changed: number; revision: number }>('/api/facts/batch', {
      json: { action: 'confirm_clean', expected_revision: expectedRevision },
    }),
  commit: (baseFactSetId: string | null, expectedRevision: number) =>
    request<{ fact_set_id: string; version: number; revision: number }>('/api/facts/commit', {
      json: { base_fact_set_id: baseFactSetId, expected_revision: expectedRevision },
    }),
}

// ---------- 岗位与匹配 ----------

export const jobs = {
  list: (signal?: AbortSignal) => request<JobSummary[]>('/api/jobs', { signal }),
  get: (jobId: string, signal?: AbortSignal) => request<Job>(`/api/jobs/${jobId}`, { signal }),
  create: (rawText: string, idempotencyKey: string) =>
    request<Job>('/api/jobs', { json: { raw_text: rawText }, idempotencyKey }),
  matches: (jobId: string, signal?: AbortSignal) =>
    request<MatchSummary[]>(`/api/jobs/${jobId}/matches`, { signal }),
  match: (jobId: string, idempotencyKey: string) =>
    request<MatchReport>(`/api/jobs/${jobId}/match`, { method: 'POST', idempotencyKey }),
  report: (reportId: string, signal?: AbortSignal) =>
    request<MatchReport>(`/api/match-reports/${reportId}`, { signal }),
}

// ---------- 改写与完整简历 ----------

export const tailoring = {
  runs: (jobId: string, signal?: AbortSignal) =>
    request<TailorRunSummary[]>(`/api/jobs/${jobId}/tailor-runs`, { signal }),
  start: (jobId: string, sections: Record<string, SectionKind>, idempotencyKey: string) =>
    request<{ run_id: string; base_doc_id: string }>(`/api/jobs/${jobId}/tailor`, {
      json: { sections },
      idempotencyKey,
    }),
  run: (runId: string, signal?: AbortSignal) =>
    request<TailorRun>(`/api/tailor-runs/${runId}`, { signal }),
  previewUrl: (runId: string) => `/api/tailor-runs/${runId}/preview`,
  exportUrl: (runId: string, format: 'html' | 'md' | 'docx') =>
    `/api/tailor-runs/${runId}/export?format=${format}`,
}
