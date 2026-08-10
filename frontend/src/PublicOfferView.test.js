import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { publicOfferLoadState, publicOfferState, responseBody } from "./publicOfferViewState.js";

const source = readFileSync(new URL("./PublicOfferView.jsx", import.meta.url), "utf8");

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

test("public Offer page is company branded, HTML first, and keeps PDF optional", () => {
  assert.match(source, /offer\.companyName/);
  assert.doesNotMatch(source, /BeyondCandidate/);
  assert.match(source, /offer\.pdfAvailable/);
  assert.doesNotMatch(source, /<iframe/);
  assert.match(source, /confirm === "accepted"[\s\S]*预计到岗日期/);
  assert.match(source, /确认婉拒 Offer/);
});
