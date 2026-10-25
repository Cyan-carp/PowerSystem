import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  applyRunEvent,
  cancelRun,
  consumeRun,
  createRun,
  getRun,
  RunHttpError,
  RunProtocolError,
  type RunConnectionState,
  type RunEvent,
  type RunSnapshot,
} from './agent-runs'
import { token } from './auth'

const encoder = new TextEncoder()

function snapshot(overrides: Partial<RunSnapshot> = {}): RunSnapshot {
  return {
    protocol: 'assistant/v4-draft1',
    run_id: 'run-1',
    session_id: 'session-1',
    turn_id: 7,
    status: 'running',
    phase: 'starting',
    cancel_requested: false,
    requires_manual_reconciliation: false,
    final_answer: '',
    answer_version: 1,
    result_refs: [],
    actions: [],
    artifacts: [],
    terminal_event: null,
    last_seq: 0,
    error: null,
    ...overrides,
  }
}

function event(overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    protocol: 'assistant/v4-draft1',
    run_id: 'run-1',
    turn_id: 7,
    seq: 1,
    type: 'progress',
    at: '2026-10-23T00:00:00Z',
    data: {},
    ...overrides,
  }
}

/** The run routes relay the upstream body verbatim: raw JSON, no envelope. */
function raw(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  const bodyText = typeof body === 'string' ? body : JSON.stringify(body)
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(headers),
    text: async () => bodyText,
  } as unknown as Response
}

function sseResponse(chunks: (string | Uint8Array)[], status = 200, contentType = 'text/event-stream; charset=utf-8'): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk)
      controller.close()
    },
  })
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'Content-Type': contentType }),
    body,
  } as unknown as Response
}

function rawFrame(seq: string, type: string, data: Record<string, unknown>, runId = 'run-1', turnId: string | number = 7): string {
  const turn = typeof turnId === 'number' ? String(turnId) : `"${turnId}"`
  return `id: ${seq}\nevent: ${type}\ndata: {"protocol":"assistant/v4-draft1","run_id":"${runId}","turn_id":${turn},"seq":${seq},"type":"${type}","at":"t","data":${JSON.stringify(data)}}\n\n`
}

const completedData = {
  status: 'succeeded',
  answer_version: 1,
  result_refs: [],
  error: null,
  requires_manual_reconciliation: false,
}

describe('S04 integration regressions found during browser review', () => {
  it('moves a queued receipt to running on the actual starting event', () => {
    const next = applyRunEvent(snapshot({ status: 'queued' }), event({ data: { phase: 'starting', message: '任务开始处理' } }))
    expect(next.status).toBe('running')
    expect(next.progress_message).toBe('任务开始处理')
  })
  it('shows only sanitized tool progress and keeps the answer in its original run', () => {
    const next = applyRunEvent(snapshot(), event({ type: 'tool_started', data: { capability: 'get_device', summary: '只读查询', reasoning: 'private', raw: { token: 'private' } } }))
    expect(next.phase).toBe('tool')
    expect(next.progress_message).toBe('get_device · 只读查询')
    expect(JSON.stringify(next)).not.toContain('private')
    expect(next.run_id).toBe('run-1')
  })
  it('preserves a full int64 receipt and quoted JSON-like answer content', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(raw('{"run_id":"run-1","session_id":"session-1","turn_id":9007199254740993,"status":"queued"}')).mockResolvedValueOnce(raw(snapshot({ final_answer: '原文 "seq":9007199254740993 保留' }))))
    expect((await createRun({ message: 'hello' }, 'same-key')).turn_id).toBe('9007199254740993')
    expect((await getRun('run-1')).final_answer).toBe('原文 "seq":9007199254740993 保留')
  })
  it('preserves actual expired artifact status without inventing a download URL', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw(snapshot({ artifacts: [{ artifact_id: 'a', status: 'expired', name: 'old.csv', storage_key: 'private', download_url: 'private' }] }))))
    const value = await getRun('run-1')
    expect(value.artifacts).toEqual([{ artifact_id: 'a', status: 'expired', name: 'old.csv' }])
  })
  it('keeps machine error code when the human message differs', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw({ code: 'event_cursor_expired', message: 'expired cursor' }, 410)))
    await expect(getRun('run-1')).rejects.toMatchObject({ code: 'event_cursor_expired' })
  })
  it('rejects negative offsets and unsafe or overflowing sequence values', async () => {
    expect(() => applyRunEvent(snapshot(), event({ type: 'text_delta', data: { answer_version: 1, offset: -1, text: 'x' } }))).toThrow('invalid_text_delta')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw(snapshot({ last_seq: '9223372036854775808' }))))
    await expect(getRun('run-1')).rejects.toBeInstanceOf(RunProtocolError)
  })
})

beforeEach(() => {
  token.value = 'jwt-test'
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  token.value = ''
})

describe('run admission and snapshots', () => {
  it('posts the run with an Idempotency-Key and the JWT header, never the URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(raw({
      run_id: 'run-1', turn_id: 7, session_id: 'session-1', status: 'queued',
    }))
    vi.stubGlobal('fetch', fetchMock)

    const receipt = await createRun({ message: '检查告警', session_id: 'session-1', page_context: { route: '/alarms' } }, 'key-1')

    expect(receipt).toEqual({ run_id: 'run-1', turn_id: 7, session_id: 'session-1', status: 'queued' })
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/agent/runs')
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('key-1')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt-test')
    expect(new Headers(init.headers).get('Content-Type')).toBe('application/json')
    expect(JSON.parse(String(init.body))).toEqual({ message: '检查告警', session_id: 'session-1', page_context: { route: '/alarms' } })
    expect(path).not.toContain('jwt-test')
  })

  it('reads a raw (non-enveloped) snapshot and cancels through the run routes', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(raw(snapshot({ last_seq: 3 })))
      .mockResolvedValueOnce(raw(snapshot({ status: 'cancelled', cancel_requested: true, last_seq: 4 })))
    vi.stubGlobal('fetch', fetchMock)

    const current = await getRun('run-1')
    expect(current).toMatchObject({ run_id: 'run-1', status: 'running', last_seq: 3 })
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/v1/agent/runs/run-1')

    const cancelled = await cancelRun('run-1')
    expect(cancelled.status).toBe('cancelled')
    expect(cancelled.cancel_requested).toBe(true)
    const [path, init] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(path).toBe('/api/v1/agent/runs/run-1/cancel')
    expect(init.method).toBe('POST')
    expect(init.body).toBeUndefined()
  })

  it('keeps 64-bit last_seq and turn_id lexemes when reading a raw snapshot', async () => {
    const body = '{"protocol":"assistant/v4-draft1","run_id":"run-1","session_id":"session-1","turn_id":9007199254740993,"status":"running","phase":"x","cancel_requested":false,"requires_manual_reconciliation":false,"final_answer":"","answer_version":1,"result_refs":[],"actions":[],"artifacts":[],"terminal_event":null,"last_seq":9007199254740993,"error":null}'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw(body)))

    const value = await getRun('run-1')
    expect(value.last_seq).toBe('9007199254740993')
    expect(value.turn_id).toBe('9007199254740993')
  })

  it('surfaces a raw upstream error body as RunHttpError instead of an envelope', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw({ code: 'invalid run request', message: 'invalid run request' }, 400)))

    await expect(getRun('run-1')).rejects.toBeInstanceOf(RunHttpError)
    await expect(getRun('run-1')).rejects.toMatchObject({ status: 400, code: 'invalid run request' })
  })

  it('clears the session and redirects when a snapshot read returns 401', async () => {
    const replace = vi.fn()
    vi.stubGlobal('window', { location: { replace } })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw({ code: 40101, message: 'unauthorized' }, 401)))

    await expect(getRun('run-1')).rejects.toMatchObject({ status: 401 })
    expect(token.value).toBe('')
    expect(replace).toHaveBeenCalledWith('/login')
  })

  it('rejects a snapshot whose run binding does not match the requested id', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw(snapshot({ run_id: 'run-2' }))))

    await expect(getRun('run-1')).rejects.toMatchObject({ name: 'RunProtocolError', code: 'run_binding_mismatch' })
  })

  it('rejects snapshots without the frozen protocol or with an unknown status', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw({ ...snapshot(), protocol: undefined })))
    await expect(getRun('run-1')).rejects.toThrow('missing_protocol')

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw({ ...snapshot(), protocol: 'assistant/v3' })))
    await expect(getRun('run-1')).rejects.toThrow('protocol_mismatch')

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw({ ...snapshot(), status: 'unknown' })))
    await expect(getRun('run-1')).rejects.toThrow('invalid_status')
  })

  it('drops non-whitelisted fields from actions, artifacts and errors in a snapshot', async () => {
    const payload: Record<string, unknown> = {
      ...snapshot(),
      actions: [{
        action_id: 'action-1', status: 'pending', approval_id: 'approval-1', plan_hash: 'hash',
        risk_class: 'R2', stale: true, observed_at: '2026-10-23T00:00:00Z', token: 'secret', raw: { secret: true },
      }],
      artifacts: [{ artifact_id: 'artifact-1', name: 'report.csv', size: 10, path: '/secret', sha256: 'abc' }],
      result_refs: [{ task_id: 'task-1', status: 'succeeded', raw: 'drop' }],
      error: { code: 'none', message: 'ok', stack: 'secret' },
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw(payload)))

    const value = await getRun('run-1')
    expect(value.actions).toEqual([{
      action_id: 'action-1', status: 'pending', approval_id: 'approval-1', plan_hash: 'hash',
      risk_class: 'R2', stale: true, observed_at: '2026-10-23T00:00:00Z',
    }])
    expect(value.artifacts).toEqual([{ artifact_id: 'artifact-1', name: 'report.csv', size: 10, sha256: 'abc' }])
    expect(value.result_refs).toEqual([{ task_id: 'task-1', status: 'succeeded' }])
    expect(value.error).toEqual({ code: 'none', message: 'ok' })
    expect(value.actions[0]).not.toHaveProperty('token')
    expect(value.artifacts[0]).not.toHaveProperty('path')
  })
})

describe('applyRunEvent', () => {
  it('applies text deltas by Unicode code point, including emoji and overlaps', () => {
    const first = applyRunEvent(snapshot({ final_answer: '', answer_version: 1 }), event({
      seq: 1, type: 'text_delta', data: { answer_version: 1, offset: 0, text: '电池⚡' },
    }))
    expect(first.final_answer).toBe('电池⚡')

    const second = applyRunEvent(first, event({
      seq: 2, type: 'text_delta', data: { answer_version: 1, offset: 3, text: '告警🔋' },
    }))
    expect(second.final_answer).toBe('电池⚡告警🔋')
    expect(Array.from(second.final_answer)).toHaveLength(6)

    const overlap = applyRunEvent(snapshot({ final_answer: 'A⚡B' }), event({
      seq: 1, type: 'text_delta', data: { answer_version: 1, offset: 1, text: '⚡B C' },
    }))
    expect(overlap.final_answer).toBe('A⚡B C')

    const reset = applyRunEvent(second, event({
      seq: 3, type: 'text_delta', data: { answer_version: 2, offset: 0, text: '已恢复', reset: true },
    }))
    expect(reset.final_answer).toBe('已恢复')
    expect(reset.answer_version).toBe(2)
  })

  it('dedupes seq, rejects gaps and enforces run/turn/version binding', () => {
    const base = snapshot({ last_seq: 2, final_answer: 'AB', answer_version: 1 })
    expect(applyRunEvent(base, event({ seq: 2 }))).toBe(base)
    expect(() => applyRunEvent(snapshot({ last_seq: 1 }), event({ seq: 3 }))).toThrow('seq_gap')
    expect(() => applyRunEvent(base, event({ seq: 3, run_id: 'other' }))).toThrow('run_binding_mismatch')
    expect(() => applyRunEvent(base, event({ seq: 3, turn_id: 8 }))).toThrow('turn_binding_mismatch')
    expect(() => applyRunEvent(base, event({
      seq: 3, type: 'text_delta', data: { answer_version: 2, offset: 2, text: 'C' },
    }))).toThrow('answer_version_mismatch')
    expect(() => applyRunEvent(base, event({
      seq: 3, type: 'text_delta', data: { answer_version: 1, offset: 5, text: 'C' },
    }))).toThrow('answer_gap')
    expect(() => applyRunEvent(base, event({
      seq: 3, type: 'text_delta', data: { answer_version: 1, offset: 1, text: 'XY' },
    }))).toThrow('answer_offset_conflict')
  })

  it('sanitizes raw event payloads and keeps the snapshot complete and immutable', () => {
    const base = snapshot({ last_seq: 0 })
    const completed = event({
      seq: 1,
      type: 'completed',
      data: {
        ...completedData,
        answer_version: 3,
        result_refs: [{ task_id: 'task-1', status: 'succeeded', historical: false, raw: 'drop' }],
        error: { code: 'none', message: 'ok', retryable: false, stack: 'secret' },
        reasoning: 'internal chain of thought',
        raw: { secret: true },
      },
    })

    const next = applyRunEvent(base, completed)

    expect(next.status).toBe('succeeded')
    expect(next.answer_version).toBe(3)
    expect(next.terminal_event?.data).toEqual({
      status: 'succeeded',
      answer_version: 3,
      result_refs: [{ task_id: 'task-1', status: 'succeeded', historical: false }],
      error: { code: 'none', message: 'ok', retryable: false },
      requires_manual_reconciliation: false,
    })
    expect(next.terminal_event?.data).not.toHaveProperty('reasoning')
    expect(next.terminal_event?.data).not.toHaveProperty('raw')
    expect(base.status).toBe('running')
    expect(base.final_answer).toBe('')
  })

  it('merges approval and artifact metadata without inventing execution state', () => {
    const base = snapshot()
    const withAction = applyRunEvent(base, event({
      seq: 1,
      type: 'approval_required',
      data: {
        action_id: 'action-1', approval_id: 'approval-1', plan_hash: 'hash', expires_at: '2026-10-23T00:05:00Z',
        risk_class: 'R2', effect_summary: '重启服务', target_summary: 'ops-agent', authority_revision: 1, token: 'secret',
      },
    }))
    expect(withAction.actions).toEqual([{
      status: 'pending', action_id: 'action-1', approval_id: 'approval-1', plan_hash: 'hash',
      expires_at: '2026-10-23T00:05:00Z', risk_class: 'R2', effect_summary: '重启服务',
      target_summary: 'ops-agent', authority_revision: 1,
    }])

    const withArtifact = applyRunEvent(withAction, event({
      seq: 2,
      type: 'artifact_ready',
      data: { artifact_id: 'artifact-1', name: 'report.csv', mime_type: 'text/csv', size: 10, sha256: 'abc', expires_at: '2026-10-30T00:00:00Z', path: '/secret' },
    }))
    expect(withArtifact.artifacts).toEqual([{
      artifact_id: 'artifact-1', name: 'report.csv', mime_type: 'text/csv', size: 10,
      sha256: 'abc', expires_at: '2026-10-30T00:00:00Z',
    }])
    expect(base.actions).toEqual([])
    expect(base.artifacts).toEqual([])
  })
})

describe('consumeRun', () => {
  it('parses chunked CRLF, UTF-8 splits, heartbeats and multi-line data, then reads the final snapshot', async () => {
    const textJson = JSON.stringify({
      protocol: 'assistant/v4-draft1', run_id: 'run-1', turn_id: 7, seq: 1,
      type: 'text_delta', at: 't', data: { answer_version: 1, offset: 0, text: '电池⚡告警' },
    })
    const terminalJson = JSON.stringify({
      protocol: 'assistant/v4-draft1', run_id: 'run-1', turn_id: 7, seq: 2,
      type: 'completed', at: 't', data: completedData,
    })
    const terminalLines = terminalJson.match(/.{1,24}/g) ?? []
    const wire = `: heartbeat\r\n\r\nid: 1\r\nevent: text_delta\r\ndata: ${textJson}\r\n\r\n`
      + `id: 2\r\nevent: completed\r\ndata: ${terminalLines.join('\r\ndata: ')}\r\n\r\n`
    const bytes = encoder.encode(wire)
    const marker = encoder.encode('⚡')
    let cut = -1
    outer: for (let index = 0; index <= bytes.length - marker.length; index += 1) {
      for (let offset = 0; offset < marker.length; offset += 1) {
        if (bytes[index + offset] !== marker[offset]) continue outer
      }
      cut = index + 1
      break
    }
    expect(cut).toBeGreaterThan(0)

    const final = snapshot({ status: 'succeeded', phase: 'finished', final_answer: '电池⚡告警', last_seq: 2 })
    const fetchMock = vi.fn(async (input: string, _init?: RequestInit) => {
      if (input.includes('/events')) return sseResponse([bytes.slice(0, cut), bytes.slice(cut)])
      return raw(final)
    })
    vi.stubGlobal('fetch', fetchMock)

    const snapshots: RunSnapshot[] = []
    const states: RunConnectionState[] = []
    await consumeRun('run-1', snapshot(), value => snapshots.push(value), undefined, state => states.push(state))

    expect(states).toEqual(['connecting', 'live', 'stopped'])
    expect(snapshots.at(-1)).toMatchObject({ status: 'succeeded', final_answer: '电池⚡告警', last_seq: 2 })
    expect(snapshots.every(value => value.run_id === 'run-1' && value.session_id === 'session-1')).toBe(true)
    expect(snapshots.every(value => Object.hasOwn(value, 'final_answer') && Object.hasOwn(value, 'turn_id'))).toBe(true)

    const eventsCall = fetchMock.mock.calls.find(call => String(call[0]).includes('/events'))!
    expect(String(eventsCall[0])).toBe('/api/v1/agent/runs/run-1/events?after_seq=0')
    const headers = new Headers(eventsCall[1]?.headers)
    expect(headers.get('Authorization')).toBe('Bearer jwt-test')
    expect(String(eventsCall[0])).not.toContain('jwt-test')
    expect(fetchMock.mock.calls.some(call => String(call[0]).endsWith('/cancel'))).toBe(false)
  })

  it('resumes from last_seq and preserves a signed 64-bit seq lexeme', async () => {
    const fetchMock = vi.fn(async (input: string) => {
      if (input.includes('/events')) {
        return sseResponse([
          rawFrame('9007199254740992', 'text_delta', { answer_version: 1, offset: 1, text: 'X' }),
          rawFrame('9007199254740993', 'text_delta', { answer_version: 1, offset: 1, text: 'B' }),
          rawFrame('9007199254740994', 'completed', completedData),
        ])
      }
      return raw(snapshot({ status: 'succeeded', final_answer: 'AB', last_seq: '9007199254740994' }))
    })
    vi.stubGlobal('fetch', fetchMock)

    const snapshots: RunSnapshot[] = []
    await consumeRun('run-1', snapshot({ last_seq: '9007199254740992', final_answer: 'A' }), value => snapshots.push(value))

    const eventsCall = fetchMock.mock.calls.find(call => String(call[0]).includes('/events'))!
    expect(String(eventsCall[0])).toBe('/api/v1/agent/runs/run-1/events?after_seq=9007199254740992')
    expect(snapshots.some(value => value.last_seq === '9007199254740993')).toBe(true)
    expect(snapshots.some(value => value.final_answer === 'AB')).toBe(true)
    expect(snapshots.at(-1)?.last_seq).toBe('9007199254740994')
  })

  it.each([400, 410])('re-reads a consistent snapshot after HTTP %i and resumes from its cursor', async status => {
    let events = 0
    let reads = 0
    const fetchMock = vi.fn(async (input: string) => {
      if (input.includes('/events')) {
        events += 1
        if (events === 1) return raw({ code: 'event_cursor_expired', message: 'event cursor expired' }, status)
        return sseResponse([rawFrame('11', 'completed', completedData)])
      }
      reads += 1
      if (reads === 1) return raw(snapshot({ last_seq: 10, final_answer: 'A' }))
      return raw(snapshot({ status: 'succeeded', final_answer: 'A', last_seq: 11 }))
    })
    vi.stubGlobal('fetch', fetchMock)

    const snapshots: RunSnapshot[] = []
    const states: RunConnectionState[] = []
    await consumeRun('run-1', snapshot(), value => snapshots.push(value), undefined, state => states.push(state))

    const eventUrls = fetchMock.mock.calls.map(call => String(call[0])).filter(url => url.includes('/events'))
    expect(eventUrls).toEqual([
      '/api/v1/agent/runs/run-1/events?after_seq=0',
      '/api/v1/agent/runs/run-1/events?after_seq=10',
    ])
    expect(snapshots.some(value => value.last_seq === 10)).toBe(true)
    expect(snapshots.at(-1)?.status).toBe('succeeded')
    expect(states).toEqual(['connecting', 'reconnecting', 'live', 'stopped'])
  })

  it('re-reads the run when a stream violates strict run/turn binding', async () => {
    let events = 0
    let reads = 0
    const fetchMock = vi.fn(async (input: string) => {
      if (input.includes('/events')) {
        events += 1
        if (events === 1) return sseResponse([rawFrame('1', 'text_delta', { answer_version: 1, offset: 0, text: 'X' }, 'run-1', 999)])
        return sseResponse([rawFrame('6', 'completed', completedData)])
      }
      reads += 1
      if (reads === 1) return raw(snapshot({ last_seq: 5, final_answer: 'AB' }))
      return raw(snapshot({ status: 'succeeded', final_answer: 'AB', last_seq: 6 }))
    })
    vi.stubGlobal('fetch', fetchMock)

    const snapshots: RunSnapshot[] = []
    await consumeRun('run-1', snapshot(), value => snapshots.push(value))

    const eventUrls = fetchMock.mock.calls.map(call => String(call[0])).filter(url => url.includes('/events'))
    expect(eventUrls).toEqual([
      '/api/v1/agent/runs/run-1/events?after_seq=0',
      '/api/v1/agent/runs/run-1/events?after_seq=5',
    ])
    expect(snapshots.some(value => value.final_answer === 'AB' && value.last_seq === 5)).toBe(true)
    expect(snapshots.at(-1)?.status).toBe('succeeded')
  })

  it('rejects a stream that does not answer with text/event-stream and recovers from the snapshot', async () => {
    let events = 0
    let reads = 0
    const fetchMock = vi.fn(async (input: string) => {
      if (input.includes('/events')) {
        events += 1
        if (events === 1) return raw('<html>gateway</html>', 200, { 'Content-Type': 'text/html' })
        return sseResponse([rawFrame('2', 'completed', completedData)])
      }
      reads += 1
      return raw(reads === 1 ? snapshot({ last_seq: 1 }) : snapshot({ status: 'succeeded', last_seq: 2 }))
    })
    vi.stubGlobal('fetch', fetchMock)

    const snapshots: RunSnapshot[] = []
    await consumeRun('run-1', snapshot(), value => snapshots.push(value))

    expect(events).toBe(2)
    expect(snapshots.at(-1)?.status).toBe('succeeded')
  })

  it('aborts instead of emitting a recovery snapshot from another session or turn', async () => {
    const fetchMock = vi.fn(async (input: string) => {
      if (input.includes('/events')) return sseResponse([], 200)
      return raw(snapshot({ session_id: 'session-2' }))
    })
    vi.stubGlobal('fetch', fetchMock)

    const onSnapshot = vi.fn()
    await expect(consumeRun('run-1', snapshot(), onSnapshot)).rejects.toBeInstanceOf(RunProtocolError)
    expect(onSnapshot).not.toHaveBeenCalled()
  })

  it('rejects an initial snapshot that is not bound to the requested run', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(consumeRun('run-2', snapshot(), vi.fn())).rejects.toThrow('run_binding_mismatch')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('clears the session and redirects on SSE 401', async () => {
    const replace = vi.fn()
    vi.stubGlobal('window', { location: { replace } })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(raw({ code: 40101, message: 'unauthorized' }, 401)))

    const onSnapshot = vi.fn()
    await expect(consumeRun('run-1', snapshot(), onSnapshot)).rejects.toBeInstanceOf(RunHttpError)
    expect(token.value).toBe('')
    expect(replace).toHaveBeenCalledWith('/login')
    expect(onSnapshot).not.toHaveBeenCalled()
  })

  it.each([403, 404])('stops without emitting foreign state on HTTP %i', async status => {
    const fetchMock = vi.fn().mockResolvedValue(raw({ code: 'forbidden', message: 'forbidden' }, status))
    vi.stubGlobal('fetch', fetchMock)

    const onSnapshot = vi.fn()
    const states: RunConnectionState[] = []
    await expect(consumeRun('run-1', snapshot(), onSnapshot, undefined, state => states.push(state)))
      .rejects.toMatchObject({ status })
    expect(onSnapshot).not.toHaveBeenCalled()
    expect(states).toEqual(['connecting', 'stopped'])
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('backs off and reconnects after a network drop without cancelling the run', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(sseResponse([rawFrame('1', 'completed', completedData)]))
      .mockResolvedValueOnce(raw(snapshot({ status: 'succeeded', last_seq: 1 })))
    vi.stubGlobal('fetch', fetchMock)

    const states: RunConnectionState[] = []
    const done = consumeRun('run-1', snapshot(), () => {}, undefined, state => states.push(state))
    await Promise.resolve()
    await Promise.resolve()
    expect(states).toEqual(['connecting'])

    await vi.advanceTimersByTimeAsync(600)
    await done

    expect(states).toEqual(['connecting', 'reconnecting', 'live', 'stopped'])
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls.some(call => String(call[0]).endsWith('/cancel'))).toBe(false)
  })

  it('releases the reader and pending timer when aborted', async () => {
    let resolveRead: ((value: { done: boolean; value?: Uint8Array }) => void) | undefined
    let reads = 0
    const cancel = vi.fn(async () => {})
    const releaseLock = vi.fn()
    const reader = {
      read: vi.fn(() => {
        reads += 1
        if (reads === 1) return Promise.resolve({ done: false, value: encoder.encode(rawFrame('1', 'progress', { phase: 'working' })) })
        return new Promise<{ done: boolean; value?: Uint8Array }>(resolve => { resolveRead = resolve })
      }),
      cancel,
      releaseLock,
    }
    const response = {
      ok: true,
      status: 200,
      headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body: { getReader: () => reader },
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))

    const controller = new AbortController()
    const states: RunConnectionState[] = []
    const snapshots: RunSnapshot[] = []
    const done = consumeRun('run-1', snapshot(), value => snapshots.push(value), controller.signal, state => states.push(state))

    await vi.waitFor(() => expect(reads).toBe(2))
    expect(snapshots).toHaveLength(1)
    controller.abort()
    resolveRead!({ done: true })
    await done

    expect(cancel).toHaveBeenCalled()
    expect(releaseLock).toHaveBeenCalled()
    expect(states).toEqual(['connecting', 'live', 'stopped'])
  })

  it('does not open a stream for an already terminal snapshot', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const states: RunConnectionState[] = []
    await consumeRun('run-1', snapshot({ status: 'succeeded' }), vi.fn(), undefined, state => states.push(state))

    expect(states).toEqual(['stopped'])
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reports protocol errors as RunProtocolError instances', () => {
    expect(() => applyRunEvent(snapshot({ last_seq: 0 }), event({ seq: 2 }))).toThrow(RunProtocolError)
  })
})
