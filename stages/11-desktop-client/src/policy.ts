export const BUSINESS_ORIGIN = 'https://www.cyancarp.cn';
export const PROTOCOL = 'powersystem-desktop/v1';
export const PARTITION = 'powersystem-desktop-memory-v1';

export function isBusinessUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.origin === BUSINESS_ORIGIN && !url.username && !url.password;
  } catch { return false; }
}

export function isExternalHttps(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 4096) return false;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !isBusinessUrl(value);
  } catch { return false; }
}

export interface Caller {
  owned: boolean;
  top: boolean;
  frameUrl: string;
  currentUrl: string;
}

export function isBusinessCaller(caller: Caller): boolean {
  return caller.owned && caller.top && isBusinessUrl(caller.frameUrl) && isBusinessUrl(caller.currentUrl);
}

export function isFallbackCaller(caller: Caller, fallbackUrl: string): boolean {
  return caller.owned && caller.top && caller.frameUrl === fallbackUrl && caller.currentUrl === fallbackUrl;
}
