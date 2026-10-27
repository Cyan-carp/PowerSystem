export interface AlarmNotice { alarmId: number; level: 'urgent' | 'major' | 'minor'; title: string; body: string }
export function validateAlarm(value: unknown): AlarmNotice {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('DESKTOP_ARGUMENTS_DENIED');
  const data = value as Record<string, unknown>;
  if (![Object.prototype, null].includes(Object.getPrototypeOf(data)) || Reflect.ownKeys(data).length !== Object.keys(data).length) throw new Error('DESKTOP_ARGUMENTS_DENIED');
  if (Object.keys(data).length !== 4 || Object.keys(data).some(k => !['alarmId', 'level', 'title', 'body'].includes(k)) ||
    !Number.isSafeInteger(data.alarmId) || (data.alarmId as number) <= 0 || !['urgent', 'major', 'minor'].includes(data.level as string) ||
    typeof data.title !== 'string' || !data.title.trim() || data.title.length > 100 || typeof data.body !== 'string' || data.body.length > 300 ||
    /[\u0000-\u001f\u007f<>]/.test(data.title + data.body)) throw new Error('DESKTOP_ARGUMENTS_DENIED');
  return data as unknown as AlarmNotice;
}

// No identity, alarm ID or notification text is persisted.
export class NotificationSession {
  generation = 0;
  private seen = new Set<number>();
  reset(): void { this.generation++; this.seen.clear(); }
  current(epoch: number): boolean { return epoch === this.generation; }
  claim(id: number): boolean {
    if (this.seen.has(id)) return false;
    // Keep dedup exact. Fail closed rather than evicting IDs and notifying twice.
    if (this.seen.size >= 100000) return false;
    this.seen.add(id); return true;
  }
}
