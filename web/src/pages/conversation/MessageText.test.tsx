import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { parseBlocks } from './messageBlocks'
import { MessageText } from './MessageText'

describe('助手回复排版', () => {
  it('分出标题、列表和段落，相邻同类列表项合在一起', () => {
    expect(parseBlocks('**结论**\n对照 JD：\n\n- 甲\n- 乙\n\n1. 一\n2. 二\n## 提醒')).toEqual([
      { type: 'paragraph', lines: ['**结论**', '对照 JD：'] },
      { type: 'list', ordered: false, items: ['甲', '乙'] },
      { type: 'list', ordered: true, items: ['一', '二'] },
      { type: 'heading', text: '提醒' },
    ])
  })

  it('加粗和代码变成元素，HTML 标签按文字显示', () => {
    const { container } = render(<MessageText text={'**专业技能** 用 `FastAPI` <b>x</b>'} />)
    expect(screen.getByText('专业技能').tagName).toBe('STRONG')
    expect(screen.getByText('FastAPI').tagName).toBe('CODE')
    expect(container.querySelector('b')).toBeNull()
    expect(container.textContent).toContain('<b>x</b>')
  })
})
