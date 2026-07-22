import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const settingsSource = readFileSync(new URL("./SettingsViews.jsx", import.meta.url), "utf8");
const jobsSource = readFileSync(new URL("./JobViews.jsx", import.meta.url), "utf8");
const scheduleSource = readFileSync(new URL("./ScheduleWorkspace.jsx", import.meta.url), "utf8");
const jobTheme = readFileSync(new URL("./product-theme-jobs-screening.css", import.meta.url), "utf8");

test("workflow template UI supports creating, adding, ordering, and deleting interview rounds", () => {
  assert.match(settingsSource, /新建流程模板/);
  assert.match(settingsSource, /添加面试轮次/);
  assert.match(settingsSource, /moveRound\(index, -1\)/);
  assert.match(settingsSource, /moveRound\(index, 1\)/);
  assert.match(settingsSource, /rounds\.filter/);
  assert.match(settingsSource, /完成后等待安排/);
});

test("job AI evaluation uses a compact switch isolated from generic input sizing", () => {
  assert.match(jobsSource, /className="compact-switch"/);
  assert.match(jobTheme, /\.compact-switch input\[type="checkbox"\]/);
  assert.match(jobTheme, /width: 46px/);
  assert.match(jobTheme, /input:checked \+ span/);
});

test("scheduling uses the next template round instead of resetting to first round", () => {
  assert.match(scheduleSource, /round: record\?\.round \|\| recommendedInterviewRound\(fallback\)/);
  assert.match(scheduleSource, /round: recommendedInterviewRound\(loadedCandidate, current\.round\)/);
  assert.match(scheduleSource, /已按职位流程预选/);
});
