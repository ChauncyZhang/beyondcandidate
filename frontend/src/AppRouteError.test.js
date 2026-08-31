import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const main = readFileSync(new URL("./main.jsx", import.meta.url), "utf8");
const errorView = readFileSync(new URL("./AppRouteError.jsx", import.meta.url), "utf8");

test("the root route replaces framework errors with a recoverable Chinese state", () => {
  assert.match(main, /errorElement: <AppRouteError \/>/);
  assert.match(errorView, /dynamically imported module/);
  assert.match(errorView, /页面资源加载失败/);
  assert.match(errorView, /window\.location\.reload\(\)/);
  assert.match(errorView, /返回工作台/);
});
