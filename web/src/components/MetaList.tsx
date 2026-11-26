// 分开排成小块的元信息，替代「A · B · C」中点串。
import type { ReactElement } from 'react'

export function MetaList({
  items,
  className,
}: {
  items: string[]
  className?: string
}): ReactElement {
  return (
    <div className={className ? `meta-list ${className}` : 'meta-list'}>
      {items.map((item, index) => (
        <span key={`${index}-${item}`}>{item}</span>
      ))}
    </div>
  )
}
