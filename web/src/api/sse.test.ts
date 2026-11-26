import { describe, expect, it } from 'vitest'
import { createSseParser, readAgentStream } from './sse'

function streamOf(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('SSE 解析', () => {
  it('一个事件被拆成几段时，等到空行才产出', () => {
    const parser = createSseParser()
    expect(parser.push('event: te')).toEqual([])
    expect(parser.push('xt\ndata: {"text":')).toEqual([])
    expect(parser.push('"你好"}\n')).toEqual([])
    expect(parser.push('\n')).toEqual([{ event: 'text', data: { text: '你好' } }])
  })

  it('一个数据块里有多个事件和保活注释时全部按顺序产出', () => {
    const parser = createSseParser()
    const events = parser.push(
      ': keep-alive\n\n' +
        'event: tool_start\ndata: {"tool":"list_jobs","tool_call_id":"c1"}\n\n' +
        'event: tool_end\r\ndata: {"tool":"list_jobs","total":2}\r\n\r\n' +
        'event: done\ndata: {"run_id":"r","status":"succeeded","stop_reason":null}\n\n',
    )
    expect(events.map((item) => item.event)).toEqual(['tool_start', 'tool_end', 'done'])
    expect(events[1]).toEqual({ event: 'tool_end', data: { tool: 'list_jobs', total: 2 } })
  })

  it('不认识的事件和坏的 JSON 被忽略，不影响后面的事件', () => {
    const parser = createSseParser()
    const events = parser.push(
      'event: memory_hint\ndata: {"x":1}\n\n' +
        'event: text\ndata: {坏的\n\n' +
        'event: text\ndata: {"text":"好"}\n\n',
    )
    expect(events).toEqual([{ event: 'text', data: { text: '好' } }])
  })

  it('多字节字符跨数据块时不乱码，流结束返回是否收到 done', async () => {
    const encoded = new TextEncoder().encode('event: text\ndata: {"text":"简历"}\n\n')
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoded.slice(0, 26)) // 从「简」字中间切开
        controller.enqueue(encoded.slice(26))
        controller.close()
      },
    })
    const seen: unknown[] = []
    const finished = await readAgentStream(new Response(body), (item) => seen.push(item))
    expect(seen).toEqual([{ event: 'text', data: { text: '简历' } }])
    expect(finished).toBe(false)

    const done = await readAgentStream(
      streamOf(['event: done\ndata: {"run_id":"r","status":"succeeded","stop_reason":null}\n\n']),
      () => undefined,
    )
    expect(done).toBe(true)
  })
})
