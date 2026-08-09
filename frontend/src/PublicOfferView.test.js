import assert from "node:assert/strict";
import test from "node:test";
import { publicOfferLoadState, publicOfferState, responseBody } from "./publicOfferViewState.js";

test("public Offer state handles active and all terminal states", () => {
  assert.equal(publicOfferState({ status: "sent" }), "active");
  for (const status of ["accepted", "declined", "expired", "withdrawn", "superseded", "invalid"]) assert.equal(publicOfferState({ status }), status);
  assert.equal(publicOfferState(null), "invalid");
});
test("accept validation requires expected start date", () => {
  assert.equal(publicOfferState({ status: "sent" }, "accepted", ""), "start-date-required");
  assert.equal(responseBody("accepted", "", ""), null);
  assert.deepEqual(responseBody("accepted", "2026-10-01", "ignored"), { decision: "accepted", expected_start_date: "2026-10-01" });
  assert.deepEqual(responseBody("declined", "", "时间不合适"), { decision: "declined", reason_text: "时间不合适" });
});

test("only a missing public resource becomes an invalid link", () => {
  assert.equal(publicOfferLoadState({ status: 404 }), "invalid");
  assert.equal(publicOfferLoadState({ status: 503 }), "error");
});
