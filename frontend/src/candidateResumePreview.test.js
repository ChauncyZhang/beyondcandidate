import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./CandidateViews.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

test("candidate resume preview opens as a centered modal instead of a side drawer", () => {
  assert.match(source, /className="resume-preview-backdrop"/);
  assert.match(source, /className="resume-preview-modal"/);
  assert.doesNotMatch(source, /className="resume-preview-drawer"/);
  assert.match(styles, /\.resume-preview-backdrop\s*\{[^}]*place-items:\s*center/);
  assert.match(styles, /\.resume-preview-modal\s*\{[^}]*width:\s*min\(/);
});

test("candidate resume preview supports expected modal close interactions", () => {
  assert.match(source, /event\.key === "Escape"/);
  assert.match(source, /onMouseDown=\{onClose\}/);
  assert.match(source, /event\.stopPropagation\(\)/);
  assert.match(source, /aria-modal="true"/);
});

test("candidate resume modal renders the authorized original file with the PDF reader", () => {
  assert.match(source, /<PdfResumeViewer/);
  assert.match(source, /controller\.getResumeFile\(candidate\.resume\.id/);
  assert.match(source, /URL\.createObjectURL\(result\.blob\)/);
  assert.match(source, /URL\.revokeObjectURL/);
  assert.doesNotMatch(source, /preview\?\.text/);
});
