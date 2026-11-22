import { computed, ref } from 'vue'
import { agentAttachments } from './api'
import type { AgentAttachment, AgentTurn } from './api'

const maxFiles = 3
const maxBytes = 5 * 1024 * 1024

export function useAssistantAttachments() {
  const files = ref<AgentAttachment[]>([])
  const selected = ref<string[]>([])
  const parsing = ref('')
  const error = ref('')
  const searching = ref(false)
  const draftIds = new Set<string>()
  let upload: AbortController | undefined
  let searchRequest: AbortController | undefined
  let retry: { key: string; signature: string } | undefined
  const active = computed(() => selected.value.length > 0)

  function reset() {
    upload?.abort()
    searchRequest?.abort()
    upload = searchRequest = undefined
    files.value = []
    selected.value = []
    parsing.value = error.value = ''
    searching.value = false
    retry = undefined
    // Drafts left behind by navigation expire on the server. An in-flight search
    // may already have consumed a draft, so reset never deletes bound files.
    draftIds.clear()
  }

  function restore(items: AgentAttachment[], history: AgentTurn[]) {
    files.value = items
    const last = [...history].reverse().find((turn) => turn.attachment_result)
    selected.value = (last?.attachment_result?.attachment_ids ?? [])
      .filter((id) => items.some((file) => file.id === id)).slice(0, maxFiles)
  }

  async function choose(event: Event) {
    const input = event.target as HTMLInputElement
    const chosen = Array.from(input.files ?? [])
    input.value = ''
    if (upload || searching.value || !chosen.length) return
    error.value = ''
    if (chosen.length + selected.value.length > maxFiles) {
      error.value = '每次最多选择 3 个附件。'
      return
    }
    const controller = new AbortController()
    upload = controller
    const failures: string[] = []
    try {
      for (const file of chosen) {
        if (controller.signal.aborted) return
        if (!file.size || file.size > maxBytes) {
          failures.push(`${file.name} 为空或超过 5 MiB。`)
          continue
        }
        parsing.value = file.name
        try {
          const item = await agentAttachments.upload(file, controller.signal)
          if (controller.signal.aborted) return
          draftIds.add(item.id)
          files.value.push(item)
          selected.value = [...selected.value, item.id]
        } catch (cause) {
          if (controller.signal.aborted) return
          failures.push(`${file.name}：${(cause as Error).message || '附件解析失败'}`)
        }
      }
      error.value = failures.join('\n')
    } finally {
      if (upload === controller) {
        upload = undefined
        parsing.value = ''
      }
    }
  }

  function toggle(id: string) {
    if (searching.value || upload) return
    error.value = ''
    if (selected.value.includes(id)) {
      selected.value = selected.value.filter((value) => value !== id)
      if (draftIds.delete(id)) {
        files.value = files.value.filter((item) => item.id !== id)
        void agentAttachments.discard(id).catch(() => { /* drafts have a server-side expiry */ })
      }
    } else if (selected.value.length < maxFiles) {
      selected.value = [...selected.value, id]
    } else {
      error.value = '每次最多选择 3 个附件。'
    }
  }

  async function search(question: string, sessionId: string | null) {
    if (!active.value || searching.value || upload) return null
    const payload = { question, session_id: sessionId, attachment_ids: [...selected.value] }
    const signature = JSON.stringify(payload)
    if (retry?.signature !== signature) retry = { signature, key: crypto.randomUUID() }
    const controller = new AbortController()
    searchRequest = controller
    searching.value = true
    error.value = ''
    try {
      const result = await agentAttachments.search({ ...payload, request_id: retry.key }, controller.signal)
      if (controller.signal.aborted) return null
      for (const id of payload.attachment_ids) draftIds.delete(id)
      retry = undefined
      return result
    } catch (cause) {
      if (!controller.signal.aborted) error.value = (cause as Error).message || '附件检索失败，请重试。'
      return null
    } finally {
      if (searchRequest === controller) {
        searchRequest = undefined
        searching.value = false
      }
    }
  }

  function cancel() {
    upload?.abort()
    searchRequest?.abort()
  }

  return { files, selected, parsing, error, searching, active, reset, restore, choose, toggle, search, cancel }
}
