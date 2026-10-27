import { BUSINESS_ORIGIN, isBusinessUrl } from './policy';
export function safeFilename(value: string): string {
  let name = value.replace(/[\\/:*?"<>|\u0000-\u001f\u007f]/g, '_').replace(/[. ]+$/g, '').slice(0, 160);
  if (!name || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name)) name = 'report.csv';
  return name;
}
export function allowedDownload(initiator: string, target: string, mime: string): boolean {
  if (initiator !== BUSINESS_ORIGIN) return false;
  try {
    const url = new URL(target);
    if (!(isBusinessUrl(target) || (url.protocol === 'blob:' && url.origin === BUSINESS_ORIGIN))) return false;
    return ['text/csv', 'application/json', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'].includes(mime.toLowerCase().split(';')[0].trim());
  } catch { return false; }
}
export function completeSize(received: number, total: number, disk: number): boolean {
  return Number.isSafeInteger(received) && received >= 0 && disk === received && (total <= 0 || received === total);
}
