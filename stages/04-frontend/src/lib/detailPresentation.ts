export function fixedNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const text = value.toFixed(digits)
  return Number(text) === 0 ? (0).toFixed(digits) : text
}

// Presentation only: never round the values used to place the threshold or
// compare the recorded numbers. Alarm state still comes from the server.
export function alarmComparison(value: number | null | undefined, threshold: number | null | undefined, probability = false) {
  if (value == null || threshold == null || !Number.isFinite(value) || !Number.isFinite(threshold)) return null
  const low = Math.min(0, value, threshold)
  const high = Math.max(0, value, threshold)
  const span = high - low || 1
  const minimum = low < 0 ? low - span * .12 : 0
  const maximum = probability && low >= 0 && high <= 1 ? 1 : high > 0 ? high + span * .12 : low < 0 ? 0 : 1
  const position = (number: number) => (number - minimum) / (maximum - minimum) * 100
  const actual = position(value)
  const limit = position(threshold)
  const zero = position(0)
  const difference = value - threshold
  const absolute = Math.abs(difference)
  const differenceText = difference === 0 ? '与阈值相同' : `${difference > 0 ? '高于' : '低于'}阈值${absolute < .01 ? '，差值小于 0.01' : ` ${fixedNumber(absolute)}`}`
  return {
    minimum, maximum, actual, limit,
    fillStart: Math.min(zero, actual), fillWidth: Math.abs(actual - zero),
    excessStart: limit, excessWidth: Math.max(0, actual - limit),
    above: difference > 0, differenceText,
  }
}

export const predictionFeatures: Record<string, { label: string; unit: string; description: string }> = {
  temperature_last: { unit: '°C', label: '最新温度', description: '预测窗口内最后一次记录的温度。' },
  temperature_mean: { unit: '°C', label: '平均温度', description: '预测窗口内温度的平均值。' },
  temperature_std: { unit: '°C', label: '温度波动', description: '温度的波动程度（标准差）；越大表示波动越明显。' },
  temperature_slope: { unit: '°C/min', label: '温度变化速度', description: '预测窗口首尾温度差除以间隔分钟数。' },
  temperature_residual_last: { unit: '°C', label: '功率修正后温度', description: '窗口末端按模型公式（温度 − 0.25 × 功率）修正后的温度指标。' },
  temperature_residual_slope: { unit: '°C/min', label: '修正后温度变化速度', description: '功率修正后温度在窗口首尾的变化量除以间隔分钟数。' },
  temperature_rising_minutes: { unit: '次', label: '修正后温度上升次数', description: '相邻有效采样中，功率修正后温度上升的次数。' },
  power_last: { unit: 'kW', label: '最新功率', description: '预测窗口内最后一次记录的功率。' },
  power_mean: { unit: 'kW', label: '平均功率', description: '预测窗口内功率的平均值。' },
  power_std: { unit: 'kW', label: '功率波动', description: '功率的波动程度（标准差）；越大表示波动越明显。' },
  power_slope: { unit: 'kW/min', label: '功率变化速度', description: '预测窗口首尾功率差除以间隔分钟数。' },
  voltage_mean: { unit: 'V', label: '平均电压', description: '预测窗口内电压的平均值。' },
  voltage_std: { unit: 'V', label: '电压波动', description: '电压的波动程度（标准差）；越大表示波动越明显。' },
  current_mean: { unit: 'A', label: '平均电流', description: '预测窗口内电流的平均值。' },
  current_std: { unit: 'A', label: '电流波动', description: '电流的波动程度（标准差）；越大表示波动越明显。' },
}

export function predictionFeature(feature: string) {
  return predictionFeatures[feature] ?? { label: '其他影响因素', unit: '', description: `暂未配置中文名称的特征：${feature}` }
}

export function contributionNumber(value: number): string {
  const text = fixedNumber(value, 3)
  return text !== '—' && Number(text) > 0 ? `+${text}` : text
}
