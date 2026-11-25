// 改写计划里「底稿没写到」的要求：哪些算、怎么称呼。和组件分开放，组件文件只导出组件。
import type { PlanBlock } from '../../api/types'

type FocusItem = PlanBlock['focus'][number]

// 底稿没写到的两种情况。「已写到」不列；部分覆盖、未覆盖的要求没有事实支撑，不在计划里。
export const UNWRITTEN_LABEL: Partial<Record<FocusItem['base_status'], string>> = {
  suggest_add: '别的简历写过',
  to_check: '你补充的，待核对',
}

export function isUnwritten(item: FocusItem): boolean {
  return item.base_status in UNWRITTEN_LABEL
}

export function countUnwritten(blocks: PlanBlock[]): number {
  return blocks.reduce((sum, block) => sum + block.focus.filter(isUnwritten).length, 0)
}
