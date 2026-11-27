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

  it('表头加分隔行的竖线写法切成表格，对齐方式按分隔行', () => {
    expect(
      parseBlocks(
        '对照如下：\n| 要求 | 你的情况 | 结论 |\n| --- | :---: | ---: |\n| Python | 有 `FastAPI` 项目 | 符合 |\n| 到岗 | 每周 3 天 |\n\n总结',
      ),
    ).toEqual([
      { type: 'paragraph', lines: ['对照如下：'] },
      {
        type: 'table',
        header: ['要求', '你的情况', '结论'],
        align: [null, 'center', 'right'],
        rows: [
          ['Python', '有 `FastAPI` 项目', '符合'],
          ['到岗', '每周 3 天', ''],
        ],
      },
      { type: 'paragraph', lines: ['总结'] },
    ])
  })

  it('没有分隔行的竖线行仍当普通段落，转义的竖线留在格内', () => {
    expect(parseBlocks('| 甲 | 乙 |\n| 丙 | 丁 |')).toEqual([
      { type: 'paragraph', lines: ['| 甲 | 乙 |', '| 丙 | 丁 |'] },
    ])
    expect(parseBlocks('| 写法 |\n| --- |\n| a \\| b |')).toEqual([
      { type: 'table', header: ['写法'], align: [null], rows: [['a | b']] },
    ])
  })

  it('表格渲染成带表头的 table，格内照样处理加粗', () => {
    render(<MessageText text={'| 要求 | 结论 |\n| --- | --- |\n| Python | **符合** |'} />)
    expect(screen.getByRole('columnheader', { name: '要求' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '符合' })).toBeInTheDocument()
    expect(screen.getByText('符合').tagName).toBe('STRONG')
  })

  it('加粗和代码变成元素，HTML 标签按文字显示', () => {
    const { container } = render(<MessageText text={'**专业技能** 用 `FastAPI` <b>x</b>'} />)
    expect(screen.getByText('专业技能').tagName).toBe('STRONG')
    expect(screen.getByText('FastAPI').tagName).toBe('CODE')
    expect(container.querySelector('b')).toBeNull()
    expect(container.textContent).toContain('<b>x</b>')
  })
})
