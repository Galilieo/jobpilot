// 助手回复的轻量排版：块的切分在 messageBlocks，这里处理加粗和行内代码并生成 React 元素。
// 不插入 HTML，回复里的任何标签都按文字显示。
import type { ReactElement, ReactNode } from 'react'
import { parseBlocks } from './messageBlocks'

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

export function MessageText({ text }: { text: string }): ReactElement {
  return (
    <div className="message-text">
      {parseBlocks(text).map((block, index) => {
        if (block.type === 'heading') return <h4 key={index}>{inline(block.text)}</h4>
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
