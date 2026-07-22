import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const root = new URL("../", import.meta.url);

test("the product shell uses the BeyondCandidate identity instead of a prototype label", async () => {
  const [index, app, login, invite, icon, dockerfile] = await Promise.all([
    readFile(new URL("index.html", root), "utf8"),
    readFile(new URL("src/App.jsx", root), "utf8"),
    readFile(new URL("src/LoginView.jsx", root), "utf8"),
    readFile(new URL("src/InviteAcceptView.jsx", root), "utf8"),
    readFile(new URL("public/favicon.svg", root), "utf8"),
    readFile(new URL("../../deploy/nginx/Dockerfile", import.meta.url), "utf8"),
  ]);

  for (const source of [index, app, login, invite]) {
    assert.match(source, /BeyondCandidate/);
    assert.doesNotMatch(source, />Prototype</i);
  }
  assert.match(index, /favicon\.svg/);
  assert.match(icon, /aria-label="BeyondCandidate"/);
  assert.match(dockerfile, /COPY frontend\/public \.\/public/);
});
