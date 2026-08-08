(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LumeriWorkspaceLayout = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const DEFAULT_SIZES = Object.freeze({
    preview: Object.freeze({ width: 62, height: 64 }),
    timeline: Object.freeze({ width: 100, height: 36 }),
    outline: Object.freeze({ width: 19, height: 64 }),
    tasks: Object.freeze({ width: 19, height: 64 }),
    files: Object.freeze({ width: 50, height: 38 }),
    library: Object.freeze({ width: 38, height: 64 }),
    history: Object.freeze({ width: 50, height: 38 }),
  });
  const LIMITS = Object.freeze({
    preview: Object.freeze({ minWidth: 30, maxWidth: 100, minHeight: 30, maxHeight: 100 }),
    timeline: Object.freeze({ minWidth: 45, maxWidth: 100, minHeight: 18, maxHeight: 100 }),
    panel: Object.freeze({ minWidth: 16, maxWidth: 100, minHeight: 18, maxHeight: 100 }),
  });
  const LEGACY_COLUMNS = 12;
  const LEGACY_ROWS = 10;
  const ROW_FILL_LIMIT = 136;
  const FULL_WIDTH_THRESHOLD = 78;
  const TIMELINE_REFERENCE = Object.freeze({ width: 784, height: 184 });
  const TIMELINE_MIN_SCALE = 0.5;

  const number = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const clean = (value) => Math.round(value * 1000) / 1000;

  function limitsFor(id) {
    if (id === "preview") return LIMITS.preview;
    if (id === "timeline") return LIMITS.timeline;
    return LIMITS.panel;
  }

  function clampSize(id, size) {
    const fallback = DEFAULT_SIZES[id] || DEFAULT_SIZES.outline;
    const limits = limitsFor(id);
    // Old releases persisted integer cols/rows. Read them once as percentages so
    // existing workspaces migrate without snapping back to a default arrangement.
    const rawWidth = Number.isFinite(Number(size?.width))
      ? Number(size.width)
      : Number.isFinite(Number(size?.cols)) ? Number(size.cols) / LEGACY_COLUMNS * 100 : fallback.width;
    const rawHeight = Number.isFinite(Number(size?.height))
      ? Number(size.height)
      : Number.isFinite(Number(size?.rows)) ? Number(size.rows) / LEGACY_ROWS * 100 : fallback.height;
    return {
      width: clean(clamp(rawWidth, limits.minWidth, limits.maxWidth)),
      height: clean(clamp(rawHeight, limits.minHeight, limits.maxHeight)),
    };
  }

  function fullRowSize(id, size) {
    const clamped = clampSize(id, size);
    return clampSize(id, { ...clamped, width: Math.max(clamped.width, FULL_WIDTH_THRESHOLD) });
  }

  function minimumWidth(id, containerWidth) {
    const ideal = id === "preview" ? 280 : id === "timeline" ? 360 : 176;
    return Math.min(ideal, Math.max(72, containerWidth * 0.46));
  }

  // Timeline chrome used to keep its desktop pixel sizes while the workspace
  // module shrank, eventually collapsing the actual lanes to zero height.
  // Use one shared factor for both axes so the timeline can only scale, never
  // be cropped by its own fixed-size toolbar.
  function timelineScale(width, height) {
    const raw = Math.min(
      Math.max(1, number(width, TIMELINE_REFERENCE.width)) / TIMELINE_REFERENCE.width,
      Math.max(1, number(height, TIMELINE_REFERENCE.height)) / TIMELINE_REFERENCE.height,
    );
    return clean(clamp(raw, TIMELINE_MIN_SCALE, 1));
  }

  function groupRows(items) {
    const rows = [];
    let row = [];
    let requested = 0;
    const flush = () => {
      if (row.length) rows.push(row);
      row = [];
      requested = 0;
    };

    for (const item of items) {
      const fullWidth = item.width >= FULL_WIDTH_THRESHOLD;
      if (fullWidth && row.length) flush();
      if (row.length && requested + item.width > ROW_FILL_LIMIT) flush();
      row.push(item);
      requested += item.width;
      if (fullWidth) flush();
    }
    flush();
    return rows;
  }

  // Allocate a line exactly: minimums protect usability, while the remaining
  // space follows the user's continuous size weights. The last item absorbs
  // floating-point residue, so a row can never leave a visible sliver empty.
  function allocate(total, desired, minimums) {
    if (!desired.length) return [];
    if (desired.length === 1) return [Math.max(0, total)];
    const safeTotal = Math.max(0, total);
    const minSum = minimums.reduce((sum, value) => sum + Math.max(0, value), 0);
    if (minSum >= safeTotal) {
      const scale = minSum ? safeTotal / minSum : 0;
      const result = minimums.map((value) => Math.max(0, value) * scale);
      result[result.length - 1] += safeTotal - result.reduce((sum, value) => sum + value, 0);
      return result;
    }

    const result = minimums.map((value) => Math.max(0, value));
    let remaining = safeTotal - minSum;
    const extraWeights = desired.map((value, index) => Math.max(1, value - result[index]));
    const weightSum = extraWeights.reduce((sum, value) => sum + value, 0);
    result.forEach((_, index) => { result[index] += remaining * extraWeights[index] / weightSum; });
    result[result.length - 1] += safeTotal - result.reduce((sum, value) => sum + value, 0);
    return result;
  }

  // Continuous justified-flow layout. Modules form natural rows, then every
  // row is justified edge-to-edge and every row height is jointly normalized
  // to the board. Deliberate gutters are the only uncovered pixels.
  function flowModules(rawItems, bounds = {}) {
    const width = Math.max(1, number(bounds.width, 1200));
    const height = Math.max(1, number(bounds.height, 760));
    const gap = clamp(number(bounds.gap, 8), 0, 32);
    const seen = new Set();
    const items = [];
    for (const raw of Array.isArray(rawItems) ? rawItems : []) {
      const id = String(raw?.id || "").trim();
      if (!id || seen.has(id)) continue;
      seen.add(id);
      items.push({ id, ...clampSize(id, raw) });
    }
    if (!items.length) return { width, height, gap, rows: [], placements: {} };

    const rowItems = groupRows(items);
    const verticalSpace = Math.max(1, height - gap * Math.max(0, rowItems.length - 1));
    const desiredHeights = rowItems.map((row) => Math.max(...row.map((item) => item.height)) / 100 * height);
    const minimumHeights = rowItems.map(() => Math.min(96, verticalSpace / rowItems.length));
    const rowHeights = allocate(verticalSpace, desiredHeights, minimumHeights);
    const placements = {};
    const rows = [];
    let y = 0;

    rowItems.forEach((row, rowIndex) => {
      const horizontalSpace = Math.max(1, width - gap * Math.max(0, row.length - 1));
      const desiredWidths = row.map((item) => item.width / 100 * width);
      const minimumWidths = row.map((item) => minimumWidth(item.id, width));
      const widths = allocate(horizontalSpace, desiredWidths, minimumWidths);
      const rowHeight = rowHeights[rowIndex];
      let x = 0;
      row.forEach((item, itemIndex) => {
        const itemWidth = widths[itemIndex];
        placements[item.id] = {
          x: clean(x), y: clean(y), width: clean(itemWidth), height: clean(rowHeight), row: rowIndex,
        };
        x += itemWidth + gap;
      });
      rows.push({
        ids: row.map((item) => item.id),
        y: clean(y),
        height: clean(rowHeight),
      });
      y += rowHeight + gap;
    });

    return { width, height, gap, rows, placements };
  }

  function hasOverlap(placements, tolerance = 0.01) {
    const entries = Object.values(placements || {});
    for (let i = 0; i < entries.length; i += 1) {
      for (let j = i + 1; j < entries.length; j += 1) {
        const a = entries[i];
        const b = entries[j];
        const overlapX = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
        const overlapY = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
        if (overlapX > tolerance && overlapY > tolerance) return true;
      }
    }
    return false;
  }

  return {
    DEFAULT_SIZES,
    LIMITS,
    ROW_FILL_LIMIT,
    FULL_WIDTH_THRESHOLD,
    TIMELINE_REFERENCE,
    TIMELINE_MIN_SCALE,
    clampSize,
    fullRowSize,
    timelineScale,
    flowModules,
    hasOverlap,
  };
});
