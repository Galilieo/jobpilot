// 助手回复的轻量排版：块的切分在 messageBlocks，这里处理加粗和行内代码并生成 React 元素。
// 不插入 HTML，回复里的任何标签都按文字显示。
import type { ReactElement, ReactNode } from 'react'
import { parseBlocks, type Block } from './messageBlocks'

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={index}>{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={index}>{part.slice(1, -1)}</code>
    }
    return part
  })
}

function MessageTable({ block }: { block: Extract<Block, { type: 'table' }> }): ReactElement {
  const align = (index: number) => block.align[index] ?? undefined
  // 外层可横向滚动：右侧面板和手机上列多时不撑破气泡。
  return (
    <div className="message-table-wrap">
      <table className="message-table">
        <thead>
          <tr>
            {block.header.map((cell, index) => (
              <th key={index} style={{ textAlign: align(index) }}>
                {inline(cell)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, index) => (
                <td key={index} style={{ textAlign: align(index) }}>
                  {inline(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function MessageText({ text }: { text: string }): ReactElement {
  return (
    <div className="message-text">
      {parseBlocks(text).map((block, index) => {
        if (block.type === 'heading') return <h4 key={index}>{inline(block.text)}</h4>
        if (block.type === 'table') return <MessageTable key={index} block={block} />
        if (block.type === 'list') {
          const List = block.ordered ? 'ol' : 'ul'
          return (
            <List key={index}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{inline(item)}</li>
              ))}
            </List>
          )
        }
        return (
          <p key={index}>
            {block.lines.map((line, lineIndex) => (
              <span key={lineIndex}>
                {lineIndex > 0 && <br />}
                {inline(line)}
              </span>
            ))}
          </p>
        )
      })}
    </div>
  )
}
