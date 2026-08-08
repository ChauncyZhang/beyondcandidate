import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";

const source = fs.readFileSync(new URL("./PublicOfferView.jsx", import.meta.url), "utf8");
test("candidate page has semantic confirmation and accessible response states", () => {
  assert.match(source, /role="dialog"/);
  assert.match(source, /expected_start_date/);
  assert.match(source, /aria-live="polite"/);
  assert.match(source, /disabled=\{submitting\}/);
  assert.match(source, /controller\.respond/);
});
