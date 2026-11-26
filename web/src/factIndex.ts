// 每份定制简历的事实编号纯函数：按首次引用顺序编为 F1、F2…，保证预览、依据追溯、匹配报告与确认卡编号一致。
import type { CitedFact, TailoredResumeData } from './api/types'

export interface IndexedFact {
  factId: string
  code: string
  fact: CitedFact
}

export interface FactIndex {
  byFactId: Map<string, IndexedFact>
  ordered: IndexedFact[]
  bulletFacts: (bulletId: string) => IndexedFact[]
  toEditorPayload: () => Record<string, { code: string; text: string }[]>
}

export function indexResumeFacts(resume: TailoredResumeData): FactIndex {
  const byFactId = new Map<string, IndexedFact>()
  const ordered: IndexedFact[] = []
  const byBulletId = new Map<string, IndexedFact[]>()

  for (const section of resume.content.sections) {
    for (const entry of section.entries) {
      for (const bullet of entry.bullets) {
        const list: IndexedFact[] = []
        const seenInBullet = new Set<string>()
        for (const factId of bullet.fact_ids) {
          if (seenInBullet.has(factId)) continue
          const fact = resume.facts[factId]
          if (!fact) continue
          seenInBullet.add(factId)
          let indexed = byFactId.get(factId)
          if (!indexed) {
            indexed = { factId, code: `F${ordered.length + 1}`, fact }
            byFactId.set(factId, indexed)
            ordered.push(indexed)
          }
          list.push(indexed)
        }
        byBulletId.set(bullet.bullet_id, list)
      }
    }
  }

  return {
    byFactId,
    ordered,
    bulletFacts: (bulletId) => byBulletId.get(bulletId) ?? [],
    toEditorPayload: () =>
      Object.fromEntries(
        [...byBulletId.entries()].map(([bulletId, items]) => [
          bulletId,
          items.map((item) => ({ code: item.code, text: item.fact.text })),
        ]),
      ),
  }
}
