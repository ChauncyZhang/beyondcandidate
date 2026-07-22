const MIN_PDF_ZOOM = 0.5;
const MAX_PDF_ZOOM = 2;

export function clampPdfPage(page, pageCount) {
  const upper = Number.isInteger(pageCount) && pageCount > 0 ? pageCount : 1;
  const value = Number.isInteger(page) ? page : 1;
  return Math.min(Math.max(value, 1), upper);
}

export function nextPdfZoom(zoom, direction) {
  const current = Number.isFinite(zoom) ? zoom : 1;
  const step = direction < 0 ? -0.1 : 0.1;
  return Math.min(MAX_PDF_ZOOM, Math.max(MIN_PDF_ZOOM, Math.round((current + step) * 10) / 10));
}
