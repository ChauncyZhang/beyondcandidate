import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = (name) => readFile(new URL(`./${name}`, import.meta.url), "utf8");

test("job create form exposes session draft change save and discard boundaries", async () => {
  const contents = await source("JobViews.jsx");
  assert.match(contents, /function JobForm\(\{[^\n]*initialDraft[^\n]*onDiscard[^\n]*onDraftChange/);
  assert.match(contents, /onDraftChange\(next\)/);
  assert.match(contents, /onDraftClear\(\)/);
  assert.match(contents, /onDiscard=/);
});

test("candidate list filters and detail tab are controlled by route props", async () => {
  const contents = await source("CandidateViews.jsx");
  assert.match(contents, /function CandidateList\(\{[^\n]*filters[^\n]*onFiltersChange/);
  assert.match(contents, /function CandidateDetail\(\{[^\n]*activeTab[^\n]*onTabChange/);
  assert.match(contents, /onFiltersChange\(/);
  assert.match(contents, /onTabChange\(item\)/);
});

test("settings section and nested tabs are controlled by route props", async () => {
  const contents = await source("SettingsViews.jsx");
  assert.match(contents, /function OrganizationSettings\(\{[\s\S]{0,300}\bactiveTab\b[\s\S]{0,150}\bonTabChange\b/);
  assert.match(contents, /function TemplateSettings\(\{[^\n]*activeTab[^\n]*onTabChange/);
  assert.match(contents, /export function SettingsWorkspace\(\{[^\n]*section[^\n]*templateTab[^\n]*onRouteChange/);
});
