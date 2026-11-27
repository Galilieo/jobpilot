// 把助手回复切成标题、列表、表格和段落。只认模型常用的几种写法，链接、代码块原样当段落。

export type CellAlign = 'left' | 'center' | 'right' | null

export type Block =
  | { type: 'heading'; text: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'table'; header: string[]; align: CellAlign[]; rows: string[][] }
  | { type: 'paragraph'; lines: string[] }

const HEADING = /^#{1,6}\s+(.*)$/
const BULLET = /^[-*]\s+(.*)$/
const NUMBERED = /^\d+[.、]\s*(.*)$/
const TABLE_ROW = /^\|.*\|$/
const DELIMITER_CELL = /^:?-+:?$/

function listItem(line: string): { ordered: boolean; text: string } | null {
  const bullet = BULLET.exec(line)
  if (bullet) return { ordered: false, text: bullet[1] }
  const numbered = NUMBERED.exec(line)
  return numbered ? { ordered: true, text: numbered[1] } : null
}

function cells(row: string): string[] {
  return row
    .slice(1, -1)
    .split(/(?<!\\)\|/)
    .map((cell) => cell.trim().replaceAll('\\|', '|'))
}

function alignOf(cell: string): CellAlign {
  const left = cell.startsWith(':')
  const right = cell.endsWith(':')
  if (left && right) return 'center'
  if (right) return 'right'
  return left ? 'left' : null
}

// 按 GFM：第二行是和表头同样多格的分隔行才算表格；否则这些竖线行就是普通文字。
// 流式输出时分隔行还没到，先按文字显示，到了再变成表格。
function tableOf(rows: string[]): Block | null {
  const header = cells(rows[0])
  const delimiter = rows.length >= 2 ? cells(rows[1]) : []
  const isTable =
    delimiter.length === header.length && delimiter.every((cell) => DELIMITER_CELL.test(cell))
  if (!isTable) return null
  return {
    type: 'table',
    header,
    align: delimiter.map(alignOf),
    rows: rows.slice(2).map((row) => header.map((_, index) => cells(row)[index] ?? '')),
  }
}

function pushRows(blocks: Block[], rows: string[]): void {
  if (rows.length === 0) return
  const table = tableOf(rows)
  const last = blocks.at(-1)
  if (table) blocks.push(table)
  else if (last?.type === 'paragraph') last.lines.push(...rows)
  else blocks.push({ type: 'paragraph', lines: [...rows] })
}

function pushLine(blocks: Block[], line: string): void {
  const last = blocks.at(-1)
  const heading = HEADING.exec(line)
  const item = heading ? null : listItem(line)
  if (heading) blocks.push({ type: 'heading', text: heading[1] })
  else if (item && last?.type === 'list' && last.ordered === item.ordered)
    last.items.push(item.text)
  else if (item) blocks.push({ type: 'list', ordered: item.ordered, items: [item.text] })
  else if (last?.type === 'paragraph') last.lines.push(line)
  else blocks.push({ type: 'paragraph', lines: [line] })
}

export function parseBlocks(source: string): Block[] {
  const blocks: Block[] = []
  let rows: string[] = []
  for (const raw of source.split('\n')) {
    const line = raw.trim()
    if (TABLE_ROW.test(line)) {
      rows.push(line)
      continue
    }
    pushRows(blocks, rows)
    rows = []
    if (line) pushLine(blocks, line)
    else blocks.push({ type: 'paragraph', lines: [] }) // 空行结束上一块
  }
  pushRows(blocks, rows)
  return blocks.filter((block) => block.type !== 'paragraph' || block.lines.length > 0)
}
