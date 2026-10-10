import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

let vite, browser, baseUrl;
before(async () => {
  vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await vite?.close(); });

test("job candidate pending onboarding opens Offer with application context and returns to the same job", async () => {
  const jobId = "10000000-0000-4000-8000-000000000001";
  const candidateId = "30000000-0000-4000-8000-000000000001";
  const applicationId = "20000000-0000-4000-8000-000000000001";
  const jobPath = `/jobs/${jobId}`;
  const job = { id: jobId, title: "Onboarding Engineer", status: "open", version: 1, priority: "normal", headcount: 1, funnel: { total: 1, stages: { hired: 1, pending_onboarding: 1 } } };
  const application = { id: applicationId, candidate_id: candidateId, job_id: jobId, job_title: job.title, job_status: "open", stage: "hired", pending_onboarding: true, version: 1 };
  const candidate = { id: candidateId, display_name: "Job pending candidate", current_title: "Engineer", application };
  const requests = [];
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  try {
    await context.route("**/api/v1/**", async (route) => {
      const url = new URL(route.request().url());
      requests.push({ path: url.pathname, query: Object.fromEntries(url.searchParams), method: route.request().method() });
      const path = url.pathname;
      const data = path === "/api/v1/me" ? { id: "user-1", display_name: "HR", roles: ["recruiting_admin"] }
        : path === "/api/v1/jobs" ? [job]
        : path === `/api/v1/job-definitions/${jobId}` ? { job, jd: {}, rules: {}, hiring_manager_ids: [] }
        : path === `/api/v1/jobs/${jobId}/funnel` ? job.funnel
        : path === "/api/v1/candidates" ? [candidate]
        : path === `/api/v1/candidates/${candidateId}` ? candidate
        : path === `/api/v1/candidates/${candidateId}/applications` ? [application]
        : new RegExp(`^/api/v1/candidates/${candidateId}/(resumes|timeline|notes)$`).test(path) ? []
        : path === "/api/v1/offers" ? [{ id: "offer-1", application_id: applicationId, job_id: jobId, candidate_name: candidate.display_name, job_title: job.title, status: "accepted", version: 1, content: {}, allowed_actions: {} }]
        : path === `/api/v1/applications/${applicationId}/onboarding` ? { id: "onboarding-1", application_id: applicationId, status: "incomplete", version: 0, complete: false, can_submit: false, can_update: true, blocking_reason: "onboarding_data_missing" }
        : null;
      await route.fulfill({ status: data !== null ? 200 : 503, contentType: "application/json", body: JSON.stringify(data !== null ? { data, meta: { departments: [], owners: [], status_counts: {}, next_cursor: null } } : { status: 503 }) });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    await page.goto(`${baseUrl}${jobPath.slice(1)}`);
    const row = page.locator(".detail-candidate-table").getByRole("button", { name: /Job pending candidate/ });
    await row.waitFor();
    assert.equal(await row.locator(".stage-pill").textContent(), "待入职");
    assert.ok(requests.some((request) => request.path === "/api/v1/candidates" && request.query.job_id === jobId));
    await row.click();
    await page.waitForURL((url) => url.pathname === `/candidates/${candidateId}`);
    const params = new URL(page.url()).searchParams;
    assert.equal(params.get("tab"), "offer");
    assert.equal(params.get("application"), applicationId);
    assert.equal(params.get("job"), jobId);
    assert.equal(params.get("return"), jobPath);
    await page.getByRole("heading", { name: "入职办理", exact: true }).waitFor();
    assert.ok(requests.some((request) => request.path === "/api/v1/offers" && request.query.application_id === applicationId));
    assert.ok(requests.some((request) => request.path === `/api/v1/applications/${applicationId}/onboarding`));
    await page.getByRole("button", { name: "返回职位详情", exact: true }).click();
    await page.waitForURL((url) => url.pathname === jobPath && !url.search);
    await page.locator(".detail-candidate-table").getByRole("button", { name: /Job pending candidate/ }).waitFor();
    assert.equal(requests.some((request) => request.method !== "GET"), false, "opening onboarding must not submit a mutation");
  } finally { await context.close(); }
});
