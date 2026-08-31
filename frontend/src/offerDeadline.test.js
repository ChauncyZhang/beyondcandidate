import assert from "node:assert/strict";
import test from "node:test";
import { displayOfferDeadline, offerDeadlineDateValue, offerDeadlineEndOfDay } from "./offerDeadline.js";

test("Offer deadlines use the Asia Shanghai calendar day in every browser timezone", () => {
  assert.equal(offerDeadlineDateValue("2099-09-01T15:59:59.999Z"), "2099-09-01");
  assert.equal(offerDeadlineDateValue("2099-09-01T16:00:00.000Z"), "2099-09-02");
  assert.equal(offerDeadlineEndOfDay("2099-09-01").toISOString(), "2099-09-01T15:59:59.999Z");
  assert.equal(displayOfferDeadline("2099-09-01T15:59:59.999Z"), "2099年9月1日");
});

test("Offer deadline helpers reject malformed values without changing their date meaning", () => {
  assert.equal(offerDeadlineDateValue("not-a-date"), "");
  assert.equal(Number.isNaN(offerDeadlineEndOfDay("2099-9-1").getTime()), true);
  assert.equal(displayOfferDeadline("not-a-date"), "not-a-date");
});
