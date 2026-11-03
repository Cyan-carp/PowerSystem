/**
 * 「模型与额度」按**功能**拆分的注册表。
 *
 * 为什么展示单位不是通道：同一个供应商账户下挂着多个功能，它们各自在自己的服务里
 * 读自己的 Key（07 解读、08 主助手、09 运维、10 安全审查、02 后端质量解读），
 * 但只有 07/08 开了余额端点。按「通道」合并展示时，后三项在页面上完全不可见，
 * 余额见底也判断不出会影响谁。
 *
 * 这张表是**能力声明**，不是配置副本：模型、地址、余额一律取后端返回值，
 * 这里只写「谁在用它」和「平台能不能查它」。新增功能要同时改这里和后端。
 */
export interface ModelFeature {
  key: string
  name: string
  purpose: string
  /** 真正发起模型调用的服务，出问题时按这个名字去查 */
  service: string
  /** 对应 /agent/model-balance 返回里的通道名；空串表示这条功能没有余额端点 */
  channel: string
  /** 谁持有这把 Key —— 用来区分「查不到余额」和「没有 Key」 */
  keyOwner: string
  /** 平台是否有独立探测接口 */
  probe: boolean
  /** 没有独立探测时的原因，直接显示给管理员 */
  probeNote: string
}

export const MODEL_FEATURES: ModelFeature[] = [
  {
    key: 'interpret',
    name: '告警解读',
    purpose: '逐条告警生成结论、依据与处置建议',
    service: '07-agent',
    channel: 'interpret',
    keyOwner: '解读专用 Key（07 服务自带）',
    probe: true,
    probeNote: '',
  },
  {
    key: 'assistant',
    name: '主助手问答',
    purpose: '平台问答、证据检索与任务派发',
    service: '08-agent-rag',
    channel: 'assistant',
    keyOwner: '主助手 Key（08 服务自带）',
    probe: false,
    probeNote: '对话探测会新建会话并留下记录，不拿它当探活手段',
  },
  {
    key: 'ops',
    name: '运维诊断与任务规划',
    purpose: '拆解运维任务、生成执行计划并判断风险',
    service: '09-ops-agent',
    channel: '',
    keyOwner: '运维 Key（09 服务自带）',
    probe: false,
    probeNote: '走兼容网关 hgapi.dieqiyun.top，该网关没有余额查询端点',
  },
  {
    key: 'security',
    name: '安全审查',
    purpose: '对高风险动作做独立复核与门禁判断',
    service: '10-security-agent',
    channel: '',
    keyOwner: '审查 Key（10 服务自带）',
    probe: false,
    probeNote: '走兼容网关 hgapi.dieqiyun.top，该网关没有余额查询端点',
  },
  {
    key: 'quality',
    name: '模型质量评估',
    purpose: '把预测退化与试用对比写成结论',
    service: '02-backend',
    // 复用解读通道的 Key，余额就是同一笔钱；分开列是为了让用途可见，不是两份额度
    channel: 'interpret',
    keyOwner: '与告警解读共用同一把 Key（后端读取）',
    probe: false,
    probeNote: '没有独立探针，状态跟随告警解读通道',
  },
]
