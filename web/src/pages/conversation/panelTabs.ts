// 右侧产物面板显示哪些标签（07 第 5.2 节）。没有当前岗位时不显示面板。
// 「事实库变化」「用到的事实」两个标签还没做，见任务 P 回报。
export type PanelTab = 'resume' | 'match' | 'job'

export const PANEL_TAB_LABEL: Record<PanelTab, string> = {
  resume: '简历',
  match: '匹配报告',
  job: '岗位',
}

export function panelTabs(state: { hasMatch: boolean; hasResume: boolean }): PanelTab[] {
  // 已匹配未生成时也显示简历标签，标签里是「生成定制简历」的空状态。
  if (state.hasResume || state.hasMatch) return ['resume', 'match', 'job']
  return ['job']
}
