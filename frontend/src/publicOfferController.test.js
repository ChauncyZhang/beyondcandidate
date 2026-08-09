import assert from "node:assert/strict";
import test from "node:test";
import { createPublicOfferController } from "./publicOfferController.js";

test("public offer requests use the path token and omit credentials", async () => {
  const calls = [];
  const controller = createPublicOfferController({ fetchImpl: async (path, options) => {
    calls.push({ path, options });
    return new Response(JSON.stringify({ data: { status: "sent", job_title: "平台工程师" } }), { headers: { "Content-Type": "application/json" } });
  } });
  await controller.load("secret-token");
  await controller.respond("secret-token", { decision: "accepted", expected_start_date: "2026-09-01" });
  assert.deepEqual(calls.map(({ path, options }) => [path, options.credentials, options.method]), [
    ["/api/public/v1/offers/secret-token", "omit", "GET"],
    ["/api/public/v1/offers/secret-token/responses", "omit", "POST"],
  ]);
});

test("public projection normalizes safe offer fields and response state", () => {
  const controller = createPublicOfferController();
  assert.deepEqual(controller.normalize({ data: { status: "accepted", candidate_name: "林候选人", job_title: "平台工程师", company_name: "示例公司", candidate_response_deadline: "2026-09-01T00:00:00Z", pdf_available: false, content: { summary: "Offer 摘要" } } }), {
    status: "accepted", candidateName: "林候选人", jobTitle: "平台工程师", companyName: "示例公司", deadline: "2026-09-01T00:00:00Z", summary: "Offer 摘要", location: "", contact: "", pdfAvailable: false, response: null,
  });
});

test("public display_status is accepted for compatibility and an empty token stays local", async () => {
  let calls = 0;
  const controller = createPublicOfferController({ fetchImpl: async () => { calls += 1; throw new Error("unexpected request"); } });
  assert.equal(controller.normalize({ display_status: "expired" }).status, "expired");
  assert.equal((await controller.load("")).status, "invalid");
  assert.equal(calls, 0);
});

test("responses send accepted and declined JSON exactly once per in-flight token", async () => {
  const calls = []; let resolve;
  const controller = createPublicOfferController({ fetchImpl: (path, options) => { calls.push({ path, options }); return new Promise((done) => { resolve = () => done(new Response(JSON.stringify({ data: { status: "accepted" } }), { headers: { "Content-Type": "application/json" } })); }); } });
  const accepted = controller.respond("token", { decision: "accepted", expected_start_date: "2026-10-01" });
  const duplicate = controller.respond("token", { decision: "accepted", expected_start_date: "2026-10-01" });
  assert.equal(accepted, duplicate);
  assert.deepEqual(JSON.parse(calls[0].options.body), { decision: "accepted", expected_start_date: "2026-10-01" });
  resolve(); await accepted;
  const declined = controller.respond("token", { decision: "declined", reason_text: "时间不合适" });
  assert.deepEqual(JSON.parse(calls[1].options.body), { decision: "declined", reason_text: "时间不合适" });
});

test("PDF fetch omits credentials", async () => {
  const calls = []; const controller = createPublicOfferController({ fetchImpl: async (path, options) => { calls.push({ path, options }); return new Response(new Blob(["pdf"]), { headers: { "Content-Type": "application/pdf" } }); } });
  await controller.fetchPdf("token");
  assert.deepEqual(calls[0], { path: "/api/public/v1/offers/token/pdf", options: { method: "GET", credentials: "omit" } });
});
