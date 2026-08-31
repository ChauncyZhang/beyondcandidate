import test, { after, before } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

const workspace = readFileSync(new URL("./InterviewFeedbackWorkspace.jsx", import.meta.url), "utf8");
const viewer = readFileSync(new URL("./PdfResumeViewer.jsx", import.meta.url), "utf8");
const interviewViews = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
const viteConfig = readFileSync(new URL("../vite.config.js", import.meta.url), "utf8");
const prototypeRoot = fileURLToPath(new URL("../", import.meta.url));
let browser;
let vite;
let baseUrl;

before(async () => {
  vite = await createServer({ root: prototypeRoot, logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});

after(async () => {
  await browser?.close();
  await vite?.close();
});

async function openWorkspace(width) {
  const context = await browser.newContext({ viewport: { width, height: 800 } });
  const page = await context.newPage();
  await page.goto(baseUrl);
  await page.evaluate(async () => {
    document.body.innerHTML = '<div id="workspace-root"></div>';
    const React = (await import("/node_modules/.vite/deps/react.js")).default;
    const { createRoot } = (await import("/node_modules/.vite/deps/react-dom_client.js")).default;
    const { InterviewFeedbackWorkspace } = await import("/src/InterviewFeedbackWorkspace.jsx");
    window.__resumeCalls = 0;
    const controller = {
      getResumeFile() {
        window.__resumeCalls += 1;
        return new Promise(() => {});
      },
      downloadResumeFile() {
        return new Promise(() => {});
      },
    };
    createRoot(document.getElementById("workspace-root")).render(
      React.createElement(
        InterviewFeedbackWorkspace,
        { record: { id: "interview-1" }, controller },
        React.createElement("div", { id: "evaluation-harness" }, "评价内容"),
      ),
    );
  });
  await page.locator("#evaluation-harness").waitFor();
  return { context, page };
}

test("feedback uses an isolated resume and evaluation workspace", () => {
  assert.match(interviewViews, /<InterviewFeedbackWorkspace/);
  assert.match(workspace, /role="tablist"/);
  assert.match(workspace, /aria-label="简历与评价"/);
  assert.match(workspace, />简历</);
  assert.match(workspace, />评价</);
  assert.match(workspace, /<PdfResumeViewer/);
  assert.match(workspace, /lazy\(\(\) => import\("\.\/PdfResumeViewer\.jsx"\)/);
  assert.match(workspace, /<Suspense/);
  assert.match(workspace, /useState\("evaluation"\)/);
  assert.match(workspace, /matchMedia\("\(min-width: 1180px\)"\)/);
  assert.match(workspace, /const showResume = wideLayout \|\| activePane === "resume"/);
  assert.match(workspace, /if \(!showResume\) return undefined/);
  assert.match(workspace, /showResume && <Suspense/);
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
  assert.match(viewer, /className="resume-image-preview"/);
  assert.match(styles, /\.resume-image-preview/);
});

test("feedback defers resume work only in the tabbed layout boundary", async () => {
  const narrow = await openWorkspace(1179);
  try {
    assert.equal(await narrow.page.evaluate(() => window.__resumeCalls), 0);
    assert.equal(await narrow.page.locator("#evaluation-panel").evaluate((element) => getComputedStyle(element).display), "block");
    assert.notEqual(await narrow.page.locator(".interview-workspace-tabs").evaluate((element) => getComputedStyle(element).display), "none");
    await narrow.page.getByRole("tab", { name: "简历" }).click();
    await narrow.page.waitForFunction(() => window.__resumeCalls === 1);
  } finally {
    await narrow.context.close();
  }

  const wide = await openWorkspace(1180);
  try {
    await wide.page.waitForFunction(() => window.__resumeCalls === 1);
    assert.equal(await wide.page.locator(".interview-workspace-tabs").evaluate((element) => getComputedStyle(element).display), "none");
    assert.notEqual(await wide.page.locator("#resume-panel").evaluate((element) => getComputedStyle(element).display), "none");
    assert.notEqual(await wide.page.locator("#evaluation-panel").evaluate((element) => getComputedStyle(element).display), "none");
  } finally {
    await wide.context.close();
  }
});
