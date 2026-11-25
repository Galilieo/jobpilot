// 简历编辑器前端行为测试：验证保存串行、导出等保存、保存失败中止导出、改用事实原文等保存与字体代码发送。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import editorScript from '../../../jobpilot/templates/resume_editor.js?raw'

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

interface RecordedFetch {
  url: string
  init: RequestInit
  body: Record<string, unknown>
  gate: Deferred<Response>
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const BULLET_ID = '11111111-1111-4111-8111-111111111111'
const FONTS = [
  { code: 'default', label: '默认字体', css: '' },
  { code: 'songti', label: '宋体', css: '"Songti SC", SimSun, serif' },
  { code: 'kaiti', label: '楷体', css: '"Kaiti SC", KaiTi, serif' },
]

function makeContent(bulletText: string, font = 'default') {
  return {
    header: ['测试同学'],
    sections: [
      {
        title: '实习经历',
        kind: 'internship',
        entries: [
          {
            entry_id: 'e1',
            experience: '示例实习',
            heading: '示例实习',
            title: '示例实习',
            time: null,
            lines: [],
            bullets: [
              {
                bullet_id: BULLET_ID,
                text: bulletText,
                fact_ids: ['22222222-2222-4222-8222-222222222222'],
                origin: 'passed',
                edited: false,
                check: { text: bulletText, layers: 'full', issues: [] },
              },
            ],
          },
        ],
      },
    ],
    style: { font, size_pt: 10.5, leading: 1.5 },
  }
}

function mountEditor(initialText = '模型改写 30%') {
  const initialContent = makeContent(initialText)
  const embedded = {
    content: initialContent,
    version: 1,
    version_id: 'v1-uuid',
    storage_key: 'resume-test',
    save_url: '/api/tailor-runs/run-1/resume',
    export_url: '/api/tailor-runs/run-1/export?format=html',
    reset_url: '/api/tailor-runs/run-1/resume/reset',
    use_facts_url: '/api/tailor-runs/run-1/resume/bullets/{bullet_id}/use-facts',
    fonts: FONTS,
  }

  document.body.innerHTML = `
    <main class="page" data-resume-version="1" data-font-code="default" style="--size: 10.5pt; --leading: 1.5;">
      <header><h1 data-part="header" data-index="0">测试同学</h1></header>
      <section data-kind="internship">
        <div class="entry" data-entry-id="e1">
          <div class="entry-head"><span class="entry-title" data-part="title">示例实习</span></div>
          <ul>
            <li data-bullet-id="${BULLET_ID}"><span data-part="text">${initialText}</span></li>
          </ul>
        </div>
      </section>
    </main>
    <script type="application/json" id="jobpilot-resume">${JSON.stringify(embedded)}</script>
  `

  const requests: RecordedFetch[] = []
  const fetchMock = vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
    const gate = deferred<Response>()
    requests.push({
      url: String(input),
      init,
      body: init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {},
      gate,
    })
    return gate.promise
  })
  vi.stubGlobal('fetch', fetchMock)

  const downloads: string[] = []
  const origClick = HTMLAnchorElement.prototype.click
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.getAttribute('href') ?? '')
    return origClick.call(this)
  })

  new Function(editorScript)()
  return { requests, downloads }
}

function flushMicrotasks(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

describe('resume_editor.js 编辑器行为', () => {
  beforeEach(() => {
    if (!globalThis.CSS || typeof globalThis.CSS.escape !== 'function') {
      vi.stubGlobal('CSS', { escape: (value: string) => value })
    }
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
    document.head.innerHTML = ''
    document.body.innerHTML = ''
    localStorage.clear()
    delete (window as { jobpilotEditor?: unknown }).jobpilotEditor
  })

  it('同一时刻最多一个保存请求在飞，第二次以第一次返回的版本为基准', async () => {
    const { requests } = mountEditor('初稿内容')
    const api = (
      window as unknown as {
        jobpilotEditor: { setEditing: (on: boolean) => void; save: () => Promise<unknown> }
      }
    ).jobpilotEditor
    const textSpan = document.querySelector('[data-part="text"]')!

    api.setEditing(true)
    textSpan.textContent = '第一次修改'
    textSpan.dispatchEvent(new Event('input', { bubbles: true }))
    const firstSave = api.save()

    // 第一次保存还在飞：再改字并触发保存，此时不能发第二个请求
    textSpan.textContent = '第二次修改'
    textSpan.dispatchEvent(new Event('input', { bubbles: true }))
    const secondAttempt = api.save()
    expect(requests).toHaveLength(1)
    expect(requests[0].body.base_version_id).toBe('v1-uuid')

    // 放行第一次保存（返回 v2），随后防抖到点后才发第二次，且基准为 v2-uuid
    vi.useFakeTimers()
    requests[0].gate.resolve(
      jsonResponse(200, {
        version_id: 'v2-uuid',
        version: 2,
        content: makeContent('第一次修改'),
      }),
    )
    await Promise.all([firstSave, secondAttempt])
    expect(requests).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(1000)
    expect(requests).toHaveLength(2)
    expect(requests[1].body.base_version_id).toBe('v2-uuid')
    const sentContent = requests[1].body.content as ReturnType<typeof makeContent>
    expect(sentContent.sections[0].entries[0].bullets[0].text).toBe('第二次修改')
  })

  it('改字后立即导出：先保存，返回新版本后再按返回版本号导出', async () => {
    const { requests, downloads } = mountEditor('原句')
    const api = (window as unknown as { jobpilotEditor: { setEditing: (on: boolean) => void } })
      .jobpilotEditor
    const textSpan = document.querySelector('[data-part="text"]')!
    const exportBtn = [...document.querySelectorAll('.toolbar button')].find(
      (b) => b.textContent === '导出 HTML',
    ) as HTMLButtonElement

    api.setEditing(true)
    textSpan.textContent = '导出前刚改的新句子'
    textSpan.dispatchEvent(new Event('input', { bubbles: true }))
    exportBtn.click()

    // 保存尚未返回：已经发出 PUT，但还没触发下载
    expect(requests).toHaveLength(1)
    expect(requests[0].init.method).toBe('PUT')
    expect(downloads).toEqual([])

    requests[0].gate.resolve(
      jsonResponse(200, {
        version_id: 'v2-uuid',
        version: 2,
        content: makeContent('导出前刚改的新句子'),
      }),
    )
    await flushMicrotasks()

    expect(downloads).toEqual(['/api/tailor-runs/run-1/export?format=html&version=2'])
  })

  it('保存失败时不导出，并把修改留在本地草稿', async () => {
    const { requests, downloads } = mountEditor('原句')
    const api = (window as unknown as { jobpilotEditor: { setEditing: (on: boolean) => void } })
      .jobpilotEditor
    const textSpan = document.querySelector('[data-part="text"]')!
    const exportBtn = [...document.querySelectorAll('.toolbar button')].find(
      (b) => b.textContent === '导出 HTML',
    ) as HTMLButtonElement

    api.setEditing(true)
    textSpan.textContent = '保存会失败的修改'
    textSpan.dispatchEvent(new Event('input', { bubbles: true }))
    exportBtn.click()

    expect(requests).toHaveLength(1)
    requests[0].gate.resolve(jsonResponse(500, { detail: 'server error' }))
    await flushMicrotasks()

    expect(downloads).toEqual([])
    expect(document.querySelector('.toolbar-status')?.textContent).toContain('没有导出')
    expect(localStorage.getItem('jobpilot-draft:resume-test')).toContain('保存会失败的修改')
  })

  it('点击「改用事实原文」先等保存结束，再带最新 version_id 请求并更新页面', async () => {
    const { requests } = mountEditor('模型写的 30%')
    const api = (window as unknown as { jobpilotEditor: { setEditing: (on: boolean) => void } })
      .jobpilotEditor
    const textSpan = document.querySelector('[data-part="text"]')!
    const useFactsBtn = document.querySelector(
      `li[data-bullet-id="${BULLET_ID}"] button[data-action="use-facts"]`,
    ) as HTMLButtonElement

    api.setEditing(true)
    textSpan.textContent = '用户刚改了一半的句子'
    textSpan.dispatchEvent(new Event('input', { bubbles: true }))
    useFactsBtn.click()

    // 在保存结束之前，不能先发出 use-facts POST
    expect(requests).toHaveLength(1)
    expect(requests[0].init.method).toBe('PUT')

    requests[0].gate.resolve(
      jsonResponse(200, {
        version_id: 'v2-uuid',
        version: 2,
        content: makeContent('用户刚改了一半的句子'),
      }),
    )
    await flushMicrotasks()

    expect(requests).toHaveLength(2)
    expect(requests[1].init.method).toBe('POST')
    expect(requests[1].url).toBe(`/api/tailor-runs/run-1/resume/bullets/${BULLET_ID}/use-facts`)
    expect(requests[1].body.base_version_id).toBe('v2-uuid')

    requests[1].gate.resolve(
      jsonResponse(200, {
        version_id: 'v3-uuid',
        version: 3,
        content: makeContent('事实原文 **20%**'),
      }),
    )
    await flushMicrotasks()

    expect(textSpan.textContent).toBe('事实原文 20%')
    expect(textSpan.querySelector('strong')?.textContent).toBe('20%')
    expect(document.querySelector('main.page')?.getAttribute('data-resume-version')).toBe('3')
    expect(document.querySelector('.toolbar-status')?.textContent).toContain('第 3 版')
  })

  it('切换字体下拉发送固定字体代码而非 CSS 字体串', async () => {
    const { requests } = mountEditor('条目')
    const fontSelect = document.querySelector('select[aria-label="字体"]') as HTMLSelectElement
    const api = (window as unknown as { jobpilotEditor: { save: () => Promise<unknown> } })
      .jobpilotEditor

    fontSelect.value = 'songti'
    fontSelect.dispatchEvent(new Event('change', { bubbles: true }))
    const saving = api.save()

    expect(requests).toHaveLength(1)
    const sent = requests[0].body.content as ReturnType<typeof makeContent>
    expect(sent.style.font).toBe('songti')

    requests[0].gate.resolve(
      jsonResponse(200, {
        version_id: 'v2-uuid',
        version: 2,
        content: makeContent('条目', 'songti'),
      }),
    )
    await saving
  })
})
