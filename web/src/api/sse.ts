// 读对话接口返回的 SSE：把字节流切成事件，只产出前端认识的事件。
// 浏览器的 EventSource 只支持 GET，发消息和确认都是 POST，所以自己读 ReadableStream。
// 不负责重试和界面状态，那些在对话页的运行控制里。
import type { AgentStreamEvent } from './types'

const KNOWN_EVENTS = new Set<AgentStreamEvent['event']>([
  'text',
  'tool_start',
  'tool_end',
  'confirm',
  'done',
  'error',
])

function parseBlock(block: string): AgentStreamEvent | null {
  let name = 'message'
  const data: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith(':')) continue // 保活注释
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') name = value
    else if (field === 'data') data.push(value)
  }
  if (!KNOWN_EVENTS.has(name as AgentStreamEvent['event']) || data.length === 0) return null
  try {
    return { event: name, data: JSON.parse(data.join('\n')) } as AgentStreamEvent
  } catch {
    return null
  }
}

export function createSseParser(): { push: (chunk: string) => AgentStreamEvent[] } {
  let buffer = ''
  return {
    push(chunk) {
      buffer = (buffer + chunk).replace(/\r\n?/g, '\n')
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() ?? '' // 最后一段还没等到空行，留到下一块
      return blocks.map(parseBlock).filter((item): item is AgentStreamEvent => item !== null)
    },
  }
}

/** 读完整个流，返回是否收到了 done。没收到说明流中途断了，调用方应重读详情。 */
export async function readAgentStream(
  response: Response,
  onEvent: (event: AgentStreamEvent) => void,
): Promise<boolean> {
  if (!response.body) return false
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  const parser = createSseParser()
  let finished = false
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    for (const event of parser.push(value)) {
      if (event.event === 'done') finished = true
      onEvent(event)
    }
  }
  return finished
}
