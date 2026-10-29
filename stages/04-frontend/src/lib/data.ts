/**
 * 组合查询：告警接口只给 device_id，设备编码与名称需要在前端按台账补齐。
 * 真实后端 /devices 的 page_size 上限是 100，这里按页取全量并缓存 60 秒。
 */
import { api } from './api'
import type { Alarm, Device } from './api'

export interface AlarmView extends Alarm {
  device_code: string
  device_name: string
  station_code: string
}

let cache: { at: number; rows: Device[] } | null = null

export async function deviceIndex(): Promise<Map<number, Device>> {
  if (cache && Date.now() - cache.at < 60_000) return new Map(cache.rows.map((item) => [item.id, item]))
  const rows: Device[] = []
  for (let page = 1; page <= 20; page += 1) {
    const payload = await api.devices({ page, pageSize: 100 })
    rows.push(...payload.list)
    if (rows.length >= payload.total || !payload.list.length) break
  }
  cache = { at: Date.now(), rows }
  return new Map(rows.map((item) => [item.id, item]))
}

export async function fetchAlarmViews(limit = 100): Promise<{ list: AlarmView[]; total: number }> {
  const [page, index] = await Promise.all([api.alarms({ page: 1, pageSize: Math.min(100, limit) }), deviceIndex()])
  return {
    total: page.total,
    list: page.list.map((alarm) => {
      const device = index.get(alarm.device_id)
      return {
        ...alarm,
        device_code: device?.device_code || `#${alarm.device_id}`,
        device_name: device?.name || '',
        station_code: device?.station_code || '',
      }
    }),
  }
}

/** 预测因子名 → 中文，未收录的保留原名，避免编造语义。 */
export const FEATURE_CN: Record<string, string> = {
  temperature_mean_1h: '近 1 小时平均温度',
  power_drop_ratio: '功率跌落比例',
  voltage_std_1h: '近 1 小时电压标准差',
  current_mean_1h: '近 1 小时平均电流',
  fault_code_24h: '24 小时故障码次数',
}

export const FEATURE_UNIT: Record<string, string> = {
  temperature_mean_1h: '℃',
  power_drop_ratio: '',
  voltage_std_1h: 'V',
  current_mean_1h: 'A',
  fault_code_24h: '次',
}
