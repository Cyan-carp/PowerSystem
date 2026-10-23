import { describe, it, expect } from 'vitest'
import { allDeletableSelected, canDeleteTask, pollDelay, retainedSelection, terminalTask, taskLabels, toggleSelection } from './ops-task'
import type { OpsTask } from '../types'
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
describe('MA-M1 task record batch deletion', () => {
  const task = (task_id: string, status: OpsTask['status']) => ({ task_id, status } as OpsTask)
  it('only finished tasks can be deleted', () => {
    for (const status of ['succeeded', 'failed', 'cancelled', 'timeout'] as const) expect(canDeleteTask(status)).toBe(true)
    for (const status of ['queued', 'planning', 'executing', 'awaiting_approval'] as const) expect(canDeleteTask(status)).toBe(false)
  })
  it('toggles a selection without duplicating ids', () => {
    expect(toggleSelection([], 'a')).toEqual(['a'])
    expect(toggleSelection(['a'], 'a')).toEqual([])
    expect(toggleSelection(['a', 'b'], 'a')).toEqual(['b'])
    expect(toggleSelection(['a'], 'b')).toEqual(['a', 'b'])
  })
  it('drops vanished or no longer deletable selections and keeps order', () => {
    const rows = [task('a', 'succeeded'), task('b', 'executing'), task('c', 'failed')]
    expect(retainedSelection(['c', 'b', 'a', 'gone'], rows)).toEqual(['c', 'a'])
    expect(retainedSelection(['a'], [])).toEqual([])
  })
  it('select-all covers only deletable rows and never counts an empty list', () => {
    const rows = [task('a', 'succeeded'), task('b', 'executing')]
    expect(allDeletableSelected(['a'], rows)).toBe(true)
    expect(allDeletableSelected(['a', 'b'], rows)).toBe(true)
    expect(allDeletableSelected([], rows)).toBe(false)
    expect(allDeletableSelected(['a'], [task('b', 'executing')])).toBe(false)
  })
})
