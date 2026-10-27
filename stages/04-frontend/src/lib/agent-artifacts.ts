import { clearSession, token } from './auth'
import { sessionEpoch } from './desktop'

export async function downloadArtifact(id: string, expectedRun: string): Promise<void> {
  if (!/^[0-9a-f-]{36}$/i.test(id)) throw new Error('产物标识无效')
  const identity = token.value
  const epoch = sessionEpoch()
  const headers = { Authorization: `Bearer ${identity}` }
  const base = `/api/v1/agent/artifacts/${encodeURIComponent(id)}`
  async function checked(path: string): Promise<Response> {
    const response = await fetch(path, { headers, cache: 'no-store' })
    if (identity !== token.value || epoch !== sessionEpoch()) throw new Error('登录身份已变化，请重新下载')
    if (response.status === 401) { clearSession(); window.location.replace('/login') }
    if (!response.ok) throw new Error(response.status === 410 ? '文件已过期或不可用' : response.status === 404 ? '文件不存在或无权访问' : `下载失败（${response.status}）`)
    return response
  }
  const meta = await (await checked(base)).json()
  if (meta.run_id !== expectedRun || meta.artifact_id !== id || meta.status !== 'ready') throw new Error('文件归属不匹配或已不可用')
  const response = await checked(base + '/download')
  if (!['text/csv', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/json'].some(type => response.headers.get('Content-Type')?.startsWith(type))) throw new Error('服务器未返回文件')
  const blob = await response.blob()
  if (identity !== token.value || epoch !== sessionEpoch()) throw new Error('登录身份已变化，请重新下载')
  if (blob.size !== meta.size) throw new Error('文件不完整，请重试')
  const bytes = await blob.arrayBuffer()
  const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(x => x.toString(16).padStart(2, '0')).join('')
  if (identity !== token.value || epoch !== sessionEpoch()) throw new Error('登录身份已变化，请重新下载')
  if (hash !== meta.sha256) throw new Error('文件校验失败，请重试')
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url; link.download = String(meta.name).replace(/[\\/:*?"<>|\r\n]/g, '_')
    document.body.appendChild(link); link.click(); link.remove()
  } finally { URL.revokeObjectURL(url) }
}
