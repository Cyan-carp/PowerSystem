import { describe, it, expect } from 'vitest'
import { pollDelay, terminalTask, taskLabels } from './ops-task'
describe('MA-M1 task polling', () => {
  it('polls active tasks every 2 seconds and honours rate limits', () => {
    expect(pollDelay('queued', false)).toBe(2000)
    expect(pollDelay('executing', false, 7)).toBe(7000)
    expect(pollDelay('planning', true)).toBeNull()
  })
  it('stops on every terminal state without confusing acceptance and completion', () => {
    for (const status of ['succeeded', 'failed', 'cancelled', 'timeout'] as const) {
      expect(terminalTask(status)).toBe(true); expect(pollDelay(status, false)).toBeNull()
    }
    expect(terminalTask('queued')).toBe(false); expect(taskLabels.queued).toContain('已受理')
  })
})
