/**
 * 运行产物下载（迁自覆盖前的同名模块）。
 *
 * 服务端把「取元数据」与「取文件」拆成两个接口：先读元数据拿到 size / sha256 /
 * run_id，再取文件流，落地前逐项核对。这样即使 CDN 或网关返回了别人的文件，
 * 也会在落盘前被拦下，不会把错文件存成用户的 CSV。
 *
 * 元数据接口是裸 JSON（网关 `proxyAgentRunJSON` 原样透传），不是 code/data 信封。
 */
import { authToken, notifyUnauthorized } from './auth'

interface ArtifactMeta {
  run_id?: string
  artifact_id?: string
  status?: string
  name?: string
  size?: number
  sha256?: string
  mime_type?: string
}

const ACCEPTED = ['text/csv', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/json']

interface LoadedArtifact {
  meta: ArtifactMeta
  blob: Blob
}

/** 归属与完整性都过关的产物字节；下载与就地读取共用这一道闸门 */
async function loadArtifact(id: string, expectedRun: string): Promise<LoadedArtifact> {
  if (!/^[0-9a-f-]{36}$/i.test(id)) throw new Error('产物标识无效')
  const identity = authToken.value
  const headers = { Authorization: `Bearer ${identity}` }
  const base = `/api/v1/agent/artifacts/${encodeURIComponent(id)}`

  async function checked(path: string): Promise<Response> {
    const response = await fetch(path, { headers, cache: 'no-store' })
    if (identity !== authToken.value) throw new Error('登录身份已变化，请重新下载')
    if (response.status === 401) {
      notifyUnauthorized()
      throw new Error('登录已失效，请重新登录')
    }
    if (!response.ok) {
      throw new Error(
        response.status === 410 ? '文件已过期或不可用' : response.status === 404 ? '文件不存在或无权访问' : `下载失败（${response.status}）`,
      )
    }
    return response
  }

  const meta = (await (await checked(base)).json()) as ArtifactMeta
  if (meta.run_id !== expectedRun || meta.artifact_id !== id || meta.status !== 'ready') {
    throw new Error('文件归属不匹配或已不可用')
  }

  const response = await checked(`${base}/download`)
  const type = response.headers.get('Content-Type')?.toLowerCase() ?? ''
  if (!ACCEPTED.some((item) => type.startsWith(item))) throw new Error('服务器未返回文件')

  const blob = await response.blob()
  if (identity !== authToken.value) throw new Error('登录身份已变化，请重新下载')
  if (typeof meta.size === 'number' && blob.size !== meta.size) throw new Error('文件不完整，请重试')

  if (meta.sha256) {
    const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
    const hash = [...new Uint8Array(digest)].map((x) => x.toString(16).padStart(2, '0')).join('')
    if (hash !== meta.sha256) throw new Error('文件校验失败，请重试')
  }

  return { meta, blob }
}

export async function downloadArtifact(id: string, expectedRun: string): Promise<void> {
  const { meta, blob } = await loadArtifact(id, expectedRun)
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = String(meta.name || id).replace(/[\\/:*?"<>|\r\n]/g, '_')
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

/**
 * 就地读取 JSON 产物（不落盘）：预演结果这种结构化结果要直接渲染，
 * 但仍按 size / sha256 核对，避免把错文件当成结论显示出来。
 */
export async function readArtifactJson<T>(id: string, expectedRun: string): Promise<T> {
  const { meta, blob } = await loadArtifact(id, expectedRun)
  if (!String(meta.mime_type || '').toLowerCase().includes('json')) throw new Error('该产物不是结构化结果')
  const text = await blob.text()
  try {
    return JSON.parse(text) as T
  } catch {
    throw new Error('产物内容无法解析')
  }
}
