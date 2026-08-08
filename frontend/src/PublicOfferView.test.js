import assert from "node:assert/strict";
import test from "node:test";
import { publicOfferState } from "./publicOfferViewState.js";

test("public Offer state handles active and all terminal states", () => {
  assert.equal(publicOfferState({ status: "sent" }), "active");
  for (const status of ["accepted", "declined", "expired", "withdrawn", "superseded", "invalid"]) assert.equal(publicOfferState({ status }), status);
  assert.equal(publicOfferState(null), "invalid");
});
test("accept validation requires expected start date", () => {
  assert.equal(publicOfferState({ status: "sent" }, "accepted", ""), "start-date-required");
});
