/**
 * 待审批动作的自动弹窗闸门。
 *
 * 审批弹窗必须在待批准动作出现时自己弹出来，而不是等人翻到折叠区再点按钮。
 * 但自动弹出需要两个约束，否则会变成骚扰：
 * 1. 同一动作只自动弹一次 —— 用户关掉后不再反复弹，人工入口仍然保留；
 * 2. 同一时刻只弹一个 —— 多个待审批动作同时到达时，其余保留手动按钮，
 *    等前一个决定完再依次自动弹出。
 *
 * 闸门放在模块作用域，跨组件实例共享；实例自己的 open 状态不参与判定。
 */
const shown = new Set<string>()
let owner = ''

/** 认领一次自动弹出。返回 true 表示调用方应当立即打开弹窗。 */
export function claimAutoApproval(key: string): boolean {
  if (!key || owner || shown.has(key)) return false
  shown.add(key)
  owner = key
  return true
}

/** 弹窗关闭、动作不再是待批准或组件卸载时释放占用。 */
export function releaseAutoApproval(key: string): void {
  if (owner === key) owner = ''
}

/** 只读视图，便于测试与排查；不暴露内部集合。 */
export function autoApprovalState(): { owner: string; shown: string[] } {
  return { owner, shown: [...shown] }
}

/** 仅测试使用：重置闸门。 */
export function resetAutoApproval(): void {
  shown.clear()
  owner = ''
}
