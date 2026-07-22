import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const source = readFileSync(new URL("./ReportViews.jsx", import.meta.url), "utf8");
let reportViews;
let vite;

before(async () => {
  vite = await createServer({
    root: process.cwd(),
    logLevel: "silent",
    server: { middlewareMode: true },
    appType: "custom",
  });
  reportViews = await vite.ssrLoadModule("/src/ReportViews.jsx");
});

after(async () => {
  await vite?.close();
});

test("active screening quality copy excludes legacy rule metrics", () => {
  assert.doesNotMatch(source, /规则通过率|解析、规则和 LLM 独立统计/);
  assert.match(source, /当前筛选质量基于简历解析与 LLM 自动评分/);
  assert.match(source, /历史规则字段即使由 API 返回，也不在当前面板展示/);
});

test("screening quality panel ignores a returned rulePassRate value", () => {
  assert.equal(typeof reportViews.ScreeningQualityPanel, "function");

  const html = renderToStaticMarkup(createElement(reportViews.ScreeningQualityPanel, {
    quality: {
      parseSuccessRate: 80,
      rulePassRate: 9137,
      llmSuccessRate: 75,
    },
  }));

  assert.match(html, /解析成功率/);
  assert.match(html, /LLM 成功率/);
  assert.doesNotMatch(html, /9137|规则通过率|解析、规则和 LLM 独立统计/);
});

test("report funnel drills into candidates with the stable job id including deferred stage", () => {
  assert.match(source, /onDrillDown\(\{ jobId: filters\.jobId \|\| "全部职位", stage: item\.stage \}\)/);
  assert.doesNotMatch(source, /onDrillDown\(\{ position:/);
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  assert.match(appSource, /function drillDownReport\(\{ jobId, stage \}\)/);
  assert.match(appSource, /candidateListPath\(\{ jobId, stage \}\)/);
});
