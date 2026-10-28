import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_BRANDING, parseBranding, loadBranding, monogramIcon } from "./branding.js";

const custom = { name: "  Example Hiring  ", short_name: "EH", logo_url: "/branding/logo.svg" };

test("runtime contract trims names, resolves favicon, and supports no logo", () => {
  assert.deepEqual(parseBranding(custom), { ...custom, name: "Example Hiring", favicon_url: custom.logo_url });
  assert.equal(parseBranding({ ...custom, logo_url: null }).favicon_url, null);
  assert.equal(parseBranding({ ...custom, favicon_url: "/icon.png" }).favicon_url, "/icon.png");
  assert.equal(parseBranding({ ...custom, favicon_url: null }).favicon_url, null);
  assert.equal(parseBranding({ ...custom, name: "a".repeat(80), short_name: "ABCDEFG8" }).name.length, 80);
});

test("invalid fields and unsafe asset paths reject the whole config", () => {
  for (const field of [{ name: " " }, { name: "a".repeat(81) }, { name: "A\nB" }, { name: "\tName" }, { name: "Name\r" }, { name: "Na\u007fme" }, { secret: "not-public" }, { short_name: "中文" }, { short_name: "a-b" }, { short_name: "123456789" }, { logo_url: undefined }]) {
    assert.throws(() => parseBranding({ ...custom, ...field }));
  }
  for (const path of ["//host/icon.svg", "https://host/icon.svg", "data:image/svg+xml,x", "icon.svg", "/a?b", "/a#b", "/a\\b", "/a\nb", "/a/../b", "/%2e%2e/b", "/a/%252e%252e/b", "/%2fhost/icon", "/a%5cb", "/a%00b", "/bad%", "/a%3fb"]) {
    assert.throws(() => parseBranding({ ...custom, logo_url: path }), path);
    assert.throws(() => parseBranding({ ...custom, favicon_url: path }), path);
  }
  for (const value of [null, [], {}, { ...custom, short_name: 3 }]) assert.throws(() => parseBranding(value));
});

test("branding fetch is public, uncached, bounded through body parsing, and has no late side effects", async () => {
  let options;
  const warnings = [];
  const warn = (message) => warnings.push(message);
  const result = await loadBranding({ warn, fetchImpl: async (url, init) => {
    assert.equal(url, "/branding.json"); options = init;
    return { ok: true, json: async () => custom };
  } });
  assert.equal(result.name, "Example Hiring");
  assert.equal(options.credentials, "omit");
  assert.equal(options.cache, "no-store");
  assert.equal(options.redirect, "error");
  assert.deepEqual(warnings, []);
  for (const fetchImpl of [async () => { throw Error("offline"); }, async () => ({ ok: false }), async () => ({ ok: true, json: async () => ({}) }), async () => ({ ok: true, json: async () => { throw SyntaxError(); } })]) {
    const before = warnings.length;
    assert.deepEqual(await loadBranding({ fetchImpl, warn }), DEFAULT_BRANDING);
    assert.equal(warnings.length, before + 1);
  }
  let resolveBody;
  const slow = await loadBranding({ warn, timeoutMs: 15, fetchImpl: async (_, init) => {
    options = init;
    return { ok: true, json: () => new Promise((resolve) => { resolveBody = resolve; }) };
  } });
  assert.deepEqual(slow, DEFAULT_BRANDING);
  assert.equal(options.signal.aborted, true);
  resolveBody(custom);
  assert.equal((await loadBranding({ warn, fetchImpl: async () => ({ ok: false }) })).name, "BeyondCandidate");
  assert.deepEqual(warnings, ["product_branding_unavailable", "product_branding_unavailable", "product_branding_invalid", "product_branding_invalid", "product_branding_timeout", "product_branding_unavailable"]);
});

test("generated favicon encodes validated monogram and never interpolates name markup", () => {
  const icon = decodeURIComponent(monogramIcon({ ...custom, name: '<img src=x onerror="alert(1)">' }).split(",")[1]);
  assert.match(icon, />EH<\/text>/);
  assert.doesNotMatch(icon, /<img|onerror/);
});
