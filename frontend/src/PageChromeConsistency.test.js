import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const source = (name) => readFileSync(new URL(`./${name}`, import.meta.url), "utf8");
const app = source("App.jsx");
const theme = source("product-theme.css");

test("workbench board keeps real candidate cards visible and exposes explicit view choices", () => {
  assert.doesNotMatch(theme, /\.stage-list\s*\{[^}]*display:\s*none/);
  assert.match(theme, /\.stage-list\s*\{[^}]*display:\s*grid/);
  assert.match(app, /className="segmented-control pipeline-view-toggle"/);
  assert.match(app, /aria-pressed=\{view === "board"\}/);
  assert.match(app, /aria-pressed=\{view === "list"\}/);
  assert.match(app, />看板<\/button>/);
  assert.match(app, />列表<\/button>/);
});

test("list page primary actions share the topbar action host", () => {
  assert.equal(existsSync(new URL("./PagePrimaryAction.jsx", import.meta.url)), true, "missing shared PagePrimaryAction portal");
  assert.match(app, /className="page-primary-action-host"/);

  for (const file of ["JobViews.jsx", "ScreeningViews.jsx", "CandidateViews.jsx", "InterviewViews.jsx", "TalentPoolViews.jsx", "ReportViews.jsx", "SettingsViews.jsx"]) {
    const content = source(file);
    assert.match(content, /PagePrimaryAction/, `${file} does not use the shared primary action host`);
    assert.match(content, /pageActionHost/, `${file} does not accept the shared primary action host`);
  }
});

test("page bodies use specific section headings instead of repeating the shell title", () => {
  assert.match(source("CandidateViews.jsx"), /<h2>全部候选人<\/h2>/);
  assert.match(source("InterviewViews.jsx"), /<h2>面试安排<\/h2>/);
  assert.match(source("TalentPoolViews.jsx"), /<h2>人才库管理<\/h2>/);
  assert.doesNotMatch(source("SettingsViews.jsx"), /className="settings-heading"/);
});
