import { describe, expect, it } from 'vitest'
import { panelTabs } from './panelTabs'

describe('面板标签', () => {
  it('按是否匹配、是否生成简历决定标签', () => {
    expect(panelTabs({ hasMatch: false, hasResume: false })).toEqual(['job'])
    expect(panelTabs({ hasMatch: true, hasResume: false })).toEqual(['resume', 'match', 'job'])
    expect(panelTabs({ hasMatch: true, hasResume: true })).toEqual(['resume', 'match', 'job'])
  })
})
