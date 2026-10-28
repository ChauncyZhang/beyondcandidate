import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";

let browser, server, baseUrl;
before(async () => {
  const nginx = await readFile(new URL("../../deploy/nginx/default.conf", import.meta.url), "utf8");
  const policy = nginx.match(/add_header Content-Security-Policy "([^"]+)" always;/)?.[1];
  assert.ok(policy, "Use the actual public development policy");
  const brandingSource = await readFile(new URL("./branding.js", import.meta.url), "utf8");
  server = createServer((request, response) => {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/branding.js") {
      response.setHeader("Content-Type", "text/javascript");
      return response.end(brandingSource);
    }
    if (url.pathname === "/check.js") {
      response.setHeader("Content-Type", "text/javascript");
      return response.end(`import {applyDocumentBranding} from '/branding.js';
        window.violations=[];
        window.addEventListener('securitypolicyviolation',event=>window.violations.push(event.effectiveDirective));
        const broken=new URL(location.href).searchParams.has('broken');
        applyDocumentBranding({name:'Example Hiring',short_name:'EH',logo_url:null,favicon_url:broken?'/missing.svg':null});
        window.brandingReady=true;`);
    }
    if (url.pathname === "/missing.svg") {
      response.writeHead(404);
      return response.end();
    }
    response.setHeader("Content-Type", "text/html");
    response.setHeader("Content-Security-Policy", policy);
    response.end('<!doctype html><html><head><link rel="icon"><script type="module" src="/check.js"></script></head><body></body></html>');
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  if (server) await new Promise(resolve => server.close(resolve));
});

for (const broken of [false, true]) {
  test(`generated favicon loads under the real public CSP with ${broken ? "broken" : "no"} logo`, async () => {
    const page = await browser.newPage();
    try {
      await page.goto(baseUrl + (broken ? "/?broken=1" : "/"));
      await page.waitForFunction(() => window.brandingReady && document.querySelector('link[rel="icon"]').href.startsWith("data:image/svg+xml,"));
      const result = await page.evaluate(async () => {
        const icon = new Image();
        const loaded = await new Promise(resolve => {
          icon.onload = () => resolve(true);
          icon.onerror = () => resolve(false);
          icon.src = document.querySelector('link[rel="icon"]').href;
        });
        return { loaded, violations: window.violations };
      });
      assert.equal(result.loaded, true, "Generated favicon must render under the deployed policy");
      assert.deepEqual(result.violations, []);
    } finally { await page.close(); }
  });
}
