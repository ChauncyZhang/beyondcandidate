import test from "node:test";
import assert from "node:assert/strict";
import { clampPdfPage, nextPdfZoom } from "./pdfResumeViewerState.js";

test("PDF page navigation stays inside the loaded document", () => {
  assert.equal(clampPdfPage(0, 6), 1);
  assert.equal(clampPdfPage(4, 6), 4);
  assert.equal(clampPdfPage(7, 6), 6);
  assert.equal(clampPdfPage(3, 0), 1);
});

test("PDF zoom uses bounded ten-percent steps", () => {
  assert.equal(nextPdfZoom(1, 1), 1.1);
  assert.equal(nextPdfZoom(1, -1), 0.9);
  assert.equal(nextPdfZoom(0.5, -1), 0.5);
  assert.equal(nextPdfZoom(2, 1), 2);
});
