import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import path from 'node:path';

export interface DesktopPreferences {
  notificationsEnabled: boolean;
  launchAtLogin: boolean;
}

export type PreferencesPatch = Partial<DesktopPreferences>;

export type PreferencesErrorCode =
  | 'PREFERENCES_PATH_INVALID'
  | 'PREFERENCES_PATCH_INVALID'
  | 'PREFERENCES_PATCH_EMPTY'
  | 'PREFERENCES_PATCH_KEY_DENIED'
  | 'PREFERENCES_PATCH_VALUE_INVALID'
  | 'PREFERENCES_WRITE_FAILED';

/** Fixed-code error so callers can report a result without echoing payloads or paths. */
export class PreferencesError extends Error {
  readonly code: PreferencesErrorCode;

  constructor(code: PreferencesErrorCode, message: string) {
    super(message);
    this.name = 'PreferencesError';
    this.code = code;
  }
}

const DEFAULTS: DesktopPreferences = { notificationsEnabled: true, launchAtLogin: false };
const DENIED_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const FILE_NAME = 'preferences.json';

interface StoredPreferences extends DesktopPreferences {
  trayHintShown: boolean;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function isAllowedKey(key: string): key is keyof DesktopPreferences {
  return key === 'notificationsEnabled' || key === 'launchAtLogin';
}

function asBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

/**
 * Accepts only a non-empty plain object whose own enumerable string keys are the
 * two boolean preferences. Unknown keys, prototype keys, symbols, accessors and
 * non-boolean values are rejected instead of being coerced or dropped.
 */
export function validatePreferencesPatch(value: unknown): PreferencesPatch {
  if (!isPlainRecord(value)) throw new PreferencesError('PREFERENCES_PATCH_INVALID', 'preferences patch must be a plain object');
  const keys = Object.keys(value);
  if (Reflect.ownKeys(value).length !== keys.length) throw new PreferencesError('PREFERENCES_PATCH_INVALID', 'preferences patch must not contain symbol or hidden keys');
  if (keys.length === 0) throw new PreferencesError('PREFERENCES_PATCH_EMPTY', 'preferences patch must not be empty');
  const patch: PreferencesPatch = {};
  for (const key of keys) {
    if (DENIED_KEYS.has(key) || !isAllowedKey(key)) throw new PreferencesError('PREFERENCES_PATCH_KEY_DENIED', 'preferences patch contains an unsupported key');
    if (!('value' in Object.getOwnPropertyDescriptor(value, key)!)) throw new PreferencesError('PREFERENCES_PATCH_INVALID', 'preferences patch must not contain accessors');
    const next = value[key];
    if (typeof next !== 'boolean') throw new PreferencesError('PREFERENCES_PATCH_VALUE_INVALID', 'preferences patch values must be booleans');
    patch[key] = next;
  }
  return patch;
}

/**
 * Local, non-sensitive desktop preferences. This module never touches Electron APIs:
 * the main process decides whether launch-at-login actually applies and rolls back
 * on failure. Only the two boolean preferences (plus the one-time tray hint flag)
 * are ever written.
 */
export class PreferencesStore {
  private readonly dir: string;
  private readonly file: string;
  private state: StoredPreferences;

  constructor(userDataDir: string) {
    if (typeof userDataDir !== 'string' || userDataDir.length === 0 || userDataDir.length > 4096 || userDataDir.includes('\0')) {
      throw new PreferencesError('PREFERENCES_PATH_INVALID', 'user data directory must be a non-empty path');
    }
    this.dir = path.resolve(userDataDir);
    this.file = path.join(this.dir, FILE_NAME);
    this.state = this.load();
  }

  get(): DesktopPreferences {
    return { notificationsEnabled: this.state.notificationsEnabled, launchAtLogin: this.state.launchAtLogin };
  }

  /**
   * Applies a validated patch and writes it atomically. The in-memory value only
   * changes after the file is replaced, so a failed write cannot look applied.
   */
  update(patch: unknown): DesktopPreferences {
    const valid = validatePreferencesPatch(patch);
    const next: StoredPreferences = { ...this.state, ...valid };
    if (next.notificationsEnabled === this.state.notificationsEnabled && next.launchAtLogin === this.state.launchAtLogin) return this.get();
    const previous = this.state;
    this.state = next;
    try { this.persist(); } catch (error) { this.state = previous; throw error; }
    return this.get();
  }

  getTrayHintShown(): boolean {
    return this.state.trayHintShown;
  }

  /** Records that the first-close tray explanation was shown; idempotent. */
  markTrayHintShown(): void {
    if (this.state.trayHintShown) return;
    const previous = this.state;
    this.state = { ...this.state, trayHintShown: true };
    try { this.persist(); } catch (error) { this.state = previous; throw error; }
  }

  private load(): StoredPreferences {
    let raw: string;
    try { raw = readFileSync(this.file, 'utf8'); } catch { return { ...DEFAULTS, trayHintShown: false }; }
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { return { ...DEFAULTS, trayHintShown: false }; }
    if (!isPlainRecord(parsed)) return { ...DEFAULTS, trayHintShown: false };
    return {
      notificationsEnabled: asBoolean(parsed.notificationsEnabled, DEFAULTS.notificationsEnabled),
      launchAtLogin: asBoolean(parsed.launchAtLogin, DEFAULTS.launchAtLogin),
      trayHintShown: parsed.trayHintShown === true,
    };
  }

  private persist(): void {
    const payload = JSON.stringify({
      notificationsEnabled: this.state.notificationsEnabled,
      launchAtLogin: this.state.launchAtLogin,
      trayHintShown: this.state.trayHintShown,
    }) + '\n';
    const tmp = this.file + '.tmp';
    try {
      mkdirSync(this.dir, { recursive: true });
      writeFileSync(tmp, payload, 'utf8');
      renameSync(tmp, this.file);
    } catch {
      try { unlinkSync(tmp); } catch { /* Nothing to clean up. */ }
      throw new PreferencesError('PREFERENCES_WRITE_FAILED', 'preferences could not be saved');
    }
  }
}
