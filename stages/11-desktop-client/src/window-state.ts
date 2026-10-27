export interface Bounds { x: number; y: number; width: number; height: number }
export interface WindowState { bounds: Bounds; maximized: boolean }

export function restoreState(value: unknown, workAreas: Bounds[]): WindowState {
  const primary = workAreas[0] ?? { x: 0, y: 0, width: 1280, height: 800 };
  const candidate = value as Partial<WindowState> | null;
  const b = candidate?.bounds;
  const valid = b && [b.x, b.y, b.width, b.height].every(Number.isSafeInteger)
    && b.width >= 400 && b.height >= 300 && b.width <= 16384 && b.height <= 16384;
  const area = valid ? workAreas.find(a => b.x >= a.x && b.y >= a.y && b.x + b.width <= a.x + a.width && b.y + b.height <= a.y + a.height) : undefined;
  if (valid && area) return { bounds: { x: b.x, y: b.y, width: b.width, height: b.height }, maximized: candidate?.maximized === true };
  const width = Math.min(valid ? b.width : 1280, primary.width);
  const height = Math.min(valid ? b.height : 840, primary.height);
  return { bounds: { width, height, x: primary.x + Math.floor((primary.width - width) / 2), y: primary.y + Math.floor((primary.height - height) / 2) }, maximized: candidate?.maximized === true };
}
