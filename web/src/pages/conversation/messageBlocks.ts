// 把助手回复切成标题、列表和段落。只认模型常用的几种写法，表格、链接、代码块原样当段落。

export type Block =
  | { type: 'heading'; text: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'paragraph'; lines: string[] }

const HEADING = /^#{1,6}\s+(.*)$/
const BULLET = /^[-*]\s+(.*)$/
const NUMBERED = /^\d+[.、]\s*(.*)$/

function listItem(line: string): { ordered: boolean; text: string } | null {
  const bullet = BULLET.exec(line)
  if (bullet) return { ordered: false, text: bullet[1] }
  const numbered = NUMBERED.exec(line)
  return numbered ? { ordered: true, text: numbered[1] } : null
}

export function parseBlocks(source: string): Block[] {
  const blocks: Block[] = []
  for (const raw of source.split('\n')) {
    const line = raw.trim()
    const last = blocks.at(-1)
    if (!line) {
      blocks.push({ type: 'paragraph', lines: [] }) // 空行结束上一块
      continue
    }
    const heading = HEADING.exec(line)
    const item = heading ? null : listItem(line)
    if (heading) blocks.push({ type: 'heading', text: heading[1] })
    else if (item && last?.type === 'list' && last.ordered === item.ordered)
      last.items.push(item.text)
    else if (item) blocks.push({ type: 'list', ordered: item.ordered, items: [item.text] })
    else if (last?.type === 'paragraph') last.lines.push(line)
    else blocks.push({ type: 'paragraph', lines: [line] })
  }
  return blocks.filter((block) => block.type !== 'paragraph' || block.lines.length > 0)
}
