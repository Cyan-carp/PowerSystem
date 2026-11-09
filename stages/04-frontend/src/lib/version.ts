/**
 * 版本号的「显示代号」：只压缩界面上看到的那串字。
 *
 * 接口参数、台账数据、按钮操作值、复制文本与操作回执一律仍用完整版本号；
 * 显示处把完整串放在 `title` 与 `data-version-full` 上，鼠标悬停/长按可核对。
 *
 * 现行命名规律（服务端与假后端一致）：
 * | 类型 | 完整版本号 | 结构 | 代号 |
 * | --- | --- | --- | --- |
 * | 模型 | `stage3-xgb-89e6530c9e29` | 阶段 / 算法 / 12 位十六进制 | `XGB·89E6` |
 * | 预测 | `lgbm-pv-2026.09` | 算法 / 用途 / 年.月 | `PV·26.09` |
 * | 规则 | `rules-v6`、`v6` | 本来就很短 | 原样 |
 *
 * 代号从完整串机械派生，不维护手写映射表；新增版本自动有代号，也不会和底层值脱钩。
 */
export function shortVersion(raw?: string | null): string {
  const value = (raw ?? '').trim()
  if (!value) return '—'
  if (value.length <= 14) return value

  const parts = value.split(/[-_]/).filter(Boolean)
  const last = parts[parts.length - 1] || value
  const family =
    [...parts.slice(0, -1)].reverse().find((part) => !/^stage\d+$/i.test(part) && !/^v\d+$/i.test(part)) ||
    parts[0] ||
    ''

  let code: string
  if (/^[0-9a-f]{8,}$/i.test(last)) {
    code = last.slice(0, 4).toUpperCase()
  } else if (/^20\d{2}[.\-]\d{1,2}$/.test(last)) {
    code = last.slice(2)
  } else {
    code = last.length > 8 ? last.slice(0, 8) : last
  }

  const head = family ? family.toUpperCase().slice(0, 6) : 'V'
  return `${head}·${code}`
}
