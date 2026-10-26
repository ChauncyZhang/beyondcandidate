import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
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

const now = Date.parse("2026-10-26T02:00:00Z");
const harness = `
import React from '/node_modules/.vite/deps/react.js';
import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
import { useInterviewAutoRefresh } from '/src/useInterviewAutoRefresh.js';
import { InterviewCalendar } from '/src/InterviewCalendar.jsx';
const initial = window.initial;
const { useCallback, useState } = React;
const controller = {
  get: async (id, {signal}) => { const response = await fetch('/refresh/' + id, {signal}); if (!response.ok) throw Object.assign(Error('unavailable'), {status: response.status}); return response.json(); },
  listMyTasks: async ({signal}) => { const response = await fetch('/refresh/tasks', {signal}); if (!response.ok) throw Error('unavailable'); return response.json(); },
};
function Harness() {
  const [state, setState] = useState(initial.state);
  const [candidateRecords, setCandidateRecords] = useState(initial.candidateRecords || []);
  const [active, setActive] = useState(true);
  window.mutate = (record) => {
    setState(current => ({...current, records: current.records.map(item => item.id === record.id ? record : item)}));
    setCandidateRecords(current => current.map(item => item.id === record.id ? record : item));
  };
  window.navigateAway = () => setActive(false);
  window.resurrect = record => {
    setState(current => ({...current, records: [...current.records, record], upcomingRecords: [...current.upcomingRecords, record]}));
    setCandidateRecords(current => [...current, record]);
  };
  useInterviewAutoRefresh({ enabled: active && !initial.calendar && initial.enabled !== false, navigationKey: active, records: state.records, upcomingRecords: state.upcomingRecords, tasks: state.tasks, candidateRecords, setCandidateRecords, setInterviewState: setState, controller: initial.missingMethods ? {} : controller });
  const loadRange = useCallback(async () => initial.state.records, []);
  return React.createElement(React.Fragment, null,
    React.createElement('pre', {id: 'state'}, JSON.stringify({...state, candidateRecords})),
    React.createElement('input', {'aria-label': 'Schedule draft', defaultValue: 'draft schedule'}),
    React.createElement('textarea', {'aria-label': 'Feedback draft', defaultValue: 'draft feedback'}),
    initial.calendar && active && React.createElement(InterviewCalendar, {controller, query: '', status: '全部状态', mineOnly: false, canSchedule: true, onLoadRange: loadRange, onOpen: () => {}}));
}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(Harness));
`;

function record(id, offset = 1000, status = "已安排") {
  return { id, serverBacked: true, version: 1, status, feedbackStatus: "未开始", startsAt: new Date(now - 1800000).toISOString(), endsAt: new Date(now + offset).toISOString(), candidate: id, position: "Engineer", round: "一面", date: "2026-10-26", time: "09:30", duration: 30, interviewers: [], interviewerIds: [] };
}
function state(records) {
  return { status: "ready", records, upcomingRecords: records.slice(0, 2), tasks: [], nextCursor: "page-3", loadingMore: false, upcomingStatus: "ready", error: "", candidateOptions: ["keep"] };
}
async function openHarness(initial, respond) {
  const context = await browser.newContext();
  const requests = [];
  await context.route("**/refresh/**", async route => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1);
    requests.push({ id, method: route.request().method() });
    const data = await respond(id, route);
    if (data !== undefined) await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
  });
  const html = await vite.transformIndexHtml("/", '<div id="root"></div><script type="module" src="/auto-refresh-harness.js"></script>');
  await context.route("**/auto-refresh-harness", route => route.fulfill({ contentType: "text/html", body: html }));
  await context.route("**/auto-refresh-harness.js", route => route.fulfill({ contentType: "application/javascript", body: harness }));
  const page = await context.newPage();
  page.setDefaultTimeout(3000);
  await page.clock.install({ time: now });
  await page.clock.pauseAt(now);
  await page.addInitScript(data => { window.initial = data; }, initial);
  await page.goto(`${baseUrl}auto-refresh-harness`);
  await page.locator("#state").waitFor();
  return { context, page, requests };
}
async function readState(page) { return JSON.parse(await page.locator("#state").textContent()); }
async function waitState(page, predicate) { await page.waitForFunction(predicate); }

test("end deadline refreshes only affected IDs, including tasks outside paginated records", async () => {
  const records = Array.from({ length: 65 }, (_, i) => record(`row-${i}`, i === 0 ? 1000 : 3600000));
  records.push(record("cancelled", 500, "已取消"), record("completed", 500, "已完成"), record("no-show", 500, "未到场"), record("pending", 500, "待评价"));
  const initial = state(records);
  initial.tasks = [{ id: "task-only", interviewId: "outside-page", type: "interview", status: "confirmed", endsAt: new Date(now + 1000).toISOString() }];
  const tasks = [{ ...initial.tasks[0], type: "interview_feedback", status: "pending_feedback" }];
  const { context, page, requests } = await openHarness({ state: initial, candidateRecords: [record("scoped")] }, id => id === "tasks" ? tasks : { ...record(id), status: "待评价", feedbackStatus: "待反馈" });
  try {
    await page.getByLabel("Schedule draft").fill("dirty schedule");
    await page.getByLabel("Feedback draft").fill("dirty feedback");
    await page.clock.runFor(999);
    assert.equal(requests.length, 0);
    assert.equal((await readState(page)).records[0].status, "已安排");
    await page.clock.runFor(1);
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).records[0].status === '待评价');
    const updated = await readState(page);
    assert.equal(updated.records.length, records.length);
    assert.equal(updated.nextCursor, "page-3");
    assert.deepEqual(updated.candidateOptions, ["keep"]);
    assert.deepEqual(updated.records.slice(1), initial.records.slice(1));
    assert.equal(updated.candidateRecords[0].status, "待评价");
    assert.equal(updated.upcomingRecords.length, 1);
    assert.equal(updated.upcomingRecords[0].id, "row-1");
    assert.deepEqual(updated.tasks, tasks);
    assert.deepEqual(requests.map(item => item.id).sort(), ["outside-page", "row-0", "scoped", "tasks"]);
    assert.equal(requests.some(item => item.method !== "GET"), false);
    assert.equal(await page.getByLabel("Schedule draft").inputValue(), "dirty schedule");
    assert.equal(await page.getByLabel("Feedback draft").inputValue(), "dirty feedback");
  } finally { await context.close(); }
});

test("task feedback classification is visible only for feedback workbench tasks", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  assert.match(source, /task\.type === "interview_feedback" && <span className="interview-status warning">待评价<\/span>/);
  assert.match(source, /enabled:.*!\(route\.kind === "interviews" && interviewMode !== "list"\)/);
});

test("an in-flight end read cannot overwrite a newer reschedule or scoped candidate source", async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const old = record("rescheduled");
  const initial = state([old]);
  initial.tasks = [{ id: "task", interviewId: old.id, type: "interview", status: "scheduled", endsAt: old.endsAt }];
  const { context, page, requests } = await openHarness({ state: initial, candidateRecords: [old] }, async id => {
    await gate;
    return id === "tasks" ? [{ ...initial.tasks[0], type: "interview_feedback" }] : { ...old, status: "待评价" };
  });
  try {
    const request = page.waitForRequest("**/refresh/rescheduled");
    await page.clock.runFor(1000);
    await request;
    const future = { ...record(old.id, 3600000), version: 2 };
    await page.evaluate(next => window.mutate(next), future);
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).records[0].version === 2);
    release();
    await page.clock.runFor(30000);
    const current = await readState(page);
    assert.deepEqual(current.records, [future]);
    assert.deepEqual(current.candidateRecords, [future]);
    assert.deepEqual(current.tasks, initial.tasks);
    assert.equal(requests.filter(item => item.id === old.id).length, 1);
  } finally { release(); await context.close(); }
});

test("returning to a hidden tab catches overdue interviews without background reads", async () => {
  const old = record("hidden");
  const { context, page, requests } = await openHarness({ state: state([old]) }, id => id === "tasks" ? [] : { ...old, status: "待评价" });
  try {
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.clock.runFor(60000);
    assert.equal(requests.length, 0);
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).records[0].status === '待评价');
    assert.deepEqual(requests.map(item => item.id).sort(), ["hidden", "tasks"]);
  } finally { await context.close(); }
});

test("failed reads retry only overdue IDs and navigation removes timers and visibility listeners", async () => {
  const old = record("retry");
  let fail = true;
  const { context, page, requests } = await openHarness({ state: state([old, record("future", 3600000)]) }, async (id, route) => {
    if (fail) { await route.fulfill({ status: 503, body: "unavailable" }); return undefined; }
    return id === "tasks" ? [] : { ...old, status: "待评价" };
  });
  try {
    const first = page.waitForResponse("**/refresh/retry");
    await page.clock.runFor(1000);
    await first;
    await page.clock.runFor(29999);
    assert.equal(requests.filter(item => item.id === old.id).length, 1);
    fail = false;
    await page.clock.runFor(1);
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).records[0].status === '待评价');
    assert.equal(requests.filter(item => item.id === old.id).length, 2);
    assert.equal(requests.some(item => item.id === "future"), false);
    await page.evaluate(() => window.navigateAway());
    await page.clock.runFor(7200000);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    assert.equal(requests.length, 4);
  } finally { await context.close(); }
});

test("optional fixture methods are not required", async () => {
  const { context, page, requests } = await openHarness({ state: state([record("fixture")]), missingMethods: true }, () => []);
  try {
    await page.clock.runFor(60000);
    assert.equal(requests.length, 0);
    assert.equal((await readState(page)).records[0].status, "已安排");
  } finally { await context.close(); }
});

test("disabled refresh protects open form drafts from record changes", async () => {
  const { context, page, requests } = await openHarness({ state: state([record("editing")]), enabled: false }, () => []);
  try {
    await page.getByLabel("Feedback draft").fill("not yet autosaved");
    await page.clock.runFor(60000);
    assert.equal(requests.length, 0);
    assert.equal((await readState(page)).records[0].status, "已安排");
    assert.equal(await page.getByLabel("Feedback draft").inputValue(), "not yet autosaved");
  } finally { await context.close(); }
});

test("an already-open week calendar changes at the exact end without reloading its range", async () => {
  const old = record("calendar-candidate");
  const { context, page, requests } = await openHarness({ state: state([old]), calendar: true }, id => ({ ...old, status: "待评价", feedbackStatus: "待反馈" }));
  try {
    const card = page.getByRole("button", { name: /calendar-candidate/ });
    await card.waitFor();
    const selectedDay = await page.locator('input[type="date"]').inputValue();
    assert.match(await card.textContent(), /已安排/);
    await page.clock.runFor(999);
    assert.equal(requests.length, 0);
    assert.match(await card.textContent(), /已安排/);
    await page.clock.runFor(1);
    await card.getByText("待评价", { exact: true }).waitFor();
    assert.equal(await page.locator('input[type="date"]').inputValue(), selectedDay);
    assert.deepEqual(requests.map(item => item.id), [old.id]);
    assert.equal(requests.some(item => item.method !== "GET"), false);
  } finally { await context.close(); }
});

test("a permanent missing detail cannot discard another elapsed result or resurrect through stale lists/tasks", async () => {
  const missing = record("missing");
  const healthy = record("healthy");
  const initial = state([missing, healthy]);
  initial.tasks = [{ id: "missing-task", interviewId: missing.id, type: "interview", status: "scheduled", endsAt: missing.endsAt }];
  const { context, page, requests } = await openHarness({ state: initial, candidateRecords: [missing, healthy] }, async (id, route) => {
    if (id === missing.id) { await route.fulfill({ status: 404, body: "not found" }); return undefined; }
    return id === "tasks" ? [...initial.tasks, { id: "healthy-task", interviewId: healthy.id, type: "interview_feedback", status: "pending_feedback" }] : { ...healthy, status: "待评价" };
  });
  try {
    await page.clock.runFor(1000);
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).records.some(item => item.id === 'healthy' && item.status === '待评价'));
    let current = await readState(page);
    assert.deepEqual(current.records.map(item => item.id), [healthy.id]);
    assert.deepEqual(current.candidateRecords.map(item => item.id), [healthy.id]);
    assert.deepEqual(current.tasks.map(item => item.id), ["healthy-task"]);
    assert.equal(current.upcomingRecords.length, 0);
    assert.equal(current.nextCursor, initial.nextCursor);
    await page.evaluate(row => window.resurrect(row), missing);
    await waitState(page, () => !JSON.parse(document.querySelector('#state').textContent).records.some(item => item.id === 'missing'));
    await page.clock.runFor(60000);
    current = await readState(page);
    assert.equal(current.candidateRecords.some(item => item.id === missing.id), false);
    assert.equal(current.upcomingRecords.some(item => item.id === missing.id), false);
    assert.deepEqual(requests.map(item => item.id).sort(), [healthy.id, missing.id, "tasks"]);
  } finally { await context.close(); }
});

test("a transient detail failure retains its rows and retries without refetching successful details", async () => {
  const flaky = record("flaky");
  const healthy = record("healthy");
  let failing = true;
  const { context, page, requests } = await openHarness({ state: state([flaky, healthy]), candidateRecords: [flaky] }, async (id, route) => {
    if (id === flaky.id && failing) { await route.fulfill({ status: 503, body: "unavailable" }); return undefined; }
    return id === "tasks" ? [{ id: "feedback-task", interviewId: healthy.id, type: "interview_feedback" }] : { ...record(id), status: "待评价" };
  });
  try {
    await page.clock.runFor(1000);
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).records[1].status === '待评价');
    const current = await readState(page);
    assert.deepEqual(current.records[0], flaky);
    assert.deepEqual(current.candidateRecords, [flaky]);
    assert.deepEqual(current.upcomingRecords, [flaky]);
    assert.equal(current.tasks[0].type, "interview_feedback");
    failing = false;
    await page.clock.runFor(29999);
    assert.equal(requests.filter(item => item.id === flaky.id).length, 1);
    await page.clock.runFor(1);
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).records[0].status === '待评价');
    assert.equal(requests.filter(item => item.id === flaky.id).length, 2);
    assert.equal(requests.filter(item => item.id === healthy.id).length, 1);
  } finally { await context.close(); }
});

test("failed task reads do not block details and retry even after all detail transitions succeed", async () => {
  const healthy = record("healthy");
  let failing = true;
  const initial = state([healthy]);
  initial.tasks = [{ id: "old-task", interviewId: healthy.id, type: "interview", status: "scheduled", endsAt: healthy.endsAt }];
  const { context, page, requests } = await openHarness({ state: initial }, async (id, route) => {
    if (id === "tasks" && failing) { await route.fulfill({ status: 503, body: "unavailable" }); return undefined; }
    return id === "tasks" ? [{ ...initial.tasks[0], type: "interview_feedback", status: "pending_feedback" }] : { ...healthy, status: "待评价" };
  });
  try {
    await page.clock.runFor(1000);
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).records[0].status === '待评价');
    assert.deepEqual((await readState(page)).tasks, initial.tasks);
    failing = false;
    await page.clock.runFor(30000);
    await waitState(page, () => JSON.parse(document.querySelector('#state').textContent).tasks[0].type === 'interview_feedback');
    assert.equal(requests.filter(item => item.id === healthy.id).length, 1);
    assert.equal(requests.filter(item => item.id === "tasks").length, 2);
  } finally { await context.close(); }
});
