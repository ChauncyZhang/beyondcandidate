import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const workspace = readFileSync(new URL("./InterviewFeedbackWorkspace.jsx", import.meta.url), "utf8");
const viewer = readFileSync(new URL("./PdfResumeViewer.jsx", import.meta.url), "utf8");
const interviewViews = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
const viteConfig = readFileSync(new URL("../vite.config.js", import.meta.url), "utf8");

test("feedback uses an isolated resume and evaluation workspace", () => {
  assert.match(interviewViews, /<InterviewFeedbackWorkspace/);
  assert.match(workspace, /role="tablist"/);
  assert.match(workspace, /aria-label="简历与评价"/);
  assert.match(workspace, />简历</);
  assert.match(workspace, />评价</);
  assert.match(workspace, /<PdfResumeViewer/);
  assert.match(workspace, /lazy\(\(\) => import\("\.\/PdfResumeViewer\.jsx"\)/);
  assert.match(workspace, /<Suspense/);
  assert.match(workspace, /previewUrl/);
  assert.match(styles, /grid-template-columns:\s*minmax\(0,\s*56fr\)\s+minmax\(0,\s*44fr\)/);
  assert.match(styles, /@media \(max-width:\s*1179px\)/);
});

test("PDF viewer uses react-pdf and exposes complete keyboard-accessible controls", () => {
  assert.match(viewer, /from "react-pdf"/);
  assert.match(viewer, /pdfjs\.GlobalWorkerOptions\.workerSrc/);
  assert.match(viewer, /cMapUrl/);
  assert.match(viewer, /standardFontDataUrl/);
  assert.match(viewer, /wasmUrl/);
  assert.match(viteConfig, /viteStaticCopy/);
  for (const label of ["上一页", "下一页", "缩小", "放大", "适合宽度", "下载原始文件"]) {
    assert.match(viewer, new RegExp(`aria-label="${label}"`));
  }
  assert.match(viewer, /<Document/);
  assert.match(viewer, /file=\{file\.url\}/);
  assert.match(viewer, /<Page/);
  assert.match(viewer, /textContent/);
  assert.match(viewer, /aria-live="polite"/);
});
