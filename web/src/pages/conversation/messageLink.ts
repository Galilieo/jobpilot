// 搜索结果与已渲染的消息用相同的序号定位。
export function messageAnchor(seq: number): string {
  return `message-${seq}`
}
