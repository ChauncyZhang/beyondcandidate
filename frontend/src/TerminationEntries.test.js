import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";

let vite, browser, baseUrl;
const fixture = `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { CandidatesWorkspace, initialCandidateRecords } from '/src/CandidateViews.jsx';
import { CandidateOfferView } from '/src/OfferViews.jsx';
import '/src/styles.css';
const params = new URLSearchParams(location.search);
const role = params.get('role') || 'HR 招聘专员';
const tab = params.get('tab') || '档案与简历';
const candidate = { ...initialCandidateRecords[0], id: 'c', candidateId: 'c', applicationId: 'a', jobId: 'j',
  serverBacked: true, stage: '面试中', jobStatus: 'open', application: { id:'a', stage:'interviewing', version:3 },
  profileStatus:'ready', resume:null, interviews:[{ interviewId:'i', applicationId:'a', version:7,
  status:'待评价', workflowStatus:'已确认', feedbackStatus:'待反馈', round:'一面', time:'2026-10-28 10:00', interviewer:'测试面试官', feedback:'尚未提交' }] };
const noop = () => {};
const controller = { getApplicationOffer: async () => ({ id:'o', applicationId:'a', jobId:'j', status:'accepted',
  version:2, candidateName:candidate.name, jobTitle:candidate.position, allowedActions:{withdraw:true}, canViewSensitiveContent:false }),
  listTemplates:async()=>[], listHistory:async()=>({responses:[],approvals:[],versions:[]}),
  getJobOfferDefaults:async()=>({}), getOnboarding:async()=>({status:'ready',complete:false,canUpdate:false,canSubmit:false}),
  withdraw:async()=>{window.withdrawCalled=true;} };
createRoot(document.getElementById('fixture')).render(params.has('offer')
  ? React.createElement(CandidateOfferView,{candidate,role,controller,onNotify:noop})
  : React.createElement(CandidatesWorkspace,{mode:'detail',selectedCandidate:candidate,currentRole:role,currentUserId:'hr',
    setSelectedCandidate:noop,setMode:noop,setRecords:noop,records:[],controller:{},offerController:controller,
    onNotify:noop,onRetryDetail:noop,detailState:{status:'ready'},detailTab:tab,onDetailTabChange:noop,
    onScheduleInterview:noop,onOpenInterviewFeedback:noop}));
`;
before(async () => {
  vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), logLevel: "silent", server: { host: "127.0.0.1", port: 0 }, plugins: [{
    name: "termination-entry-fixture",
    resolveId(id) { if (id === "virtual:termination-entry") return "\0termination-entry"; },
    load(id) { if (id === "\0termination-entry") return fixture; },
    configureServer(server) { server.middlewares.use(async (req, res, next) => {
      if (!req.url.startsWith("/__termination-entry")) return next();
      const html = await server.transformIndexHtml(req.url, '<html><head></head><body><div id="fixture"></div><script type="module" src="/@id/__x00__termination-entry"></script></body></html>');
      res.setHeader("Content-Type", "text/html"); res.end(html);
    }); },
  }] });
  await vite.listen(); baseUrl = vite.resolvedUrls.local[0]; browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await vite?.close(); });

for (const width of [1440, 390]) {
  test(`candidate top, application and interview entries use the shared confirmation at ${width}px`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 1000 } });
    try {
      await page.route("**/api/v1/applications/a/termination", (route) => route.fulfill({ json: { data: {
        application_id:"a",version:3,stage:"interviewing",preview_version:"preview",counts:{interviews:1},
        allowed_actions:{terminate:true,confirm_external:false},termination:null,requires_not_onboarded_confirmation:false,
      } } }));
      await page.goto(`${baseUrl}__termination-entry`);
      await page.locator(".candidate-detail-hero").getByRole("button", { name: "终止本次招聘", exact:true }).click();
      await page.getByRole("dialog", { name: "终止本次招聘", exact:true }).waitFor();
      assert.equal(await page.getByRole("button", { name:"确认终止本次招聘" }).isDisabled(), true);
      await page.getByRole("button", { name:"关闭终止确认" }).click();
      await page.goto(`${baseUrl}__termination-entry?tab=${encodeURIComponent("职位申请")}`);
      await page.locator(".candidate-tab-content").getByRole("button", { name:"终止本次招聘",exact:true }).click();
      await page.getByRole("dialog").waitFor();
      await page.getByRole("button", { name:"关闭终止确认" }).click();
      await page.goto(`${baseUrl}__termination-entry?tab=${encodeURIComponent("面试与反馈")}`);
      await page.locator(".candidate-interview-actions").getByRole("button", { name:"修改面试" }).waitFor();
      await page.locator(".candidate-interview-actions").getByRole("button", { name:"取消面试",exact:true }).click();
      await page.getByRole("dialog", { name:"仅取消本场面试" }).waitFor();
      await page.getByRole("button", { name:"候选人退出，终止本次招聘" }).click();
      await page.getByRole("dialog", { name:"终止本次招聘",exact:true }).waitFor();
      const box = await page.getByRole("dialog").boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width + 1);
      await mkdir(fileURLToPath(new URL("../../.tmp/", import.meta.url)), {recursive:true});
      await page.screenshot({path:fileURLToPath(new URL(`../../.tmp/termination-entry-${width}.png`, import.meta.url))});
      await page.getByLabel('终止原因（必填）').fill('候选人主动退出');
      const danger = page.getByRole('button',{name:'确认终止本次招聘',exact:true});
      await danger.hover();
      const contrast = await danger.evaluate(button => {
        const style=getComputedStyle(button);
        const luminance=color=>{
          const values=color.match(/[\d.]+/g).slice(0,3).map(Number).map(value=>value/255).map(value=>value<=0.04045?value/12.92:((value+0.055)/1.055)**2.4);
          return values[0]*0.2126+values[1]*0.7152+values[2]*0.0722;
        };
        const foreground=luminance(style.color),background=luminance(style.backgroundColor);
        return (Math.max(foreground,background)+0.05)/(Math.min(foreground,background)+0.05);
      });
      assert.ok(contrast>=4.5,`danger hover contrast ${contrast}`);
      const layout=await page.getByRole('dialog').evaluate(dialog=>({width:dialog.clientWidth,scrollWidth:dialog.scrollWidth}));
      assert.ok(layout.scrollWidth<=layout.width+1,JSON.stringify(layout));
      await page.screenshot({path:fileURLToPath(new URL(`../../.tmp/termination-confirm-hover-${width}.png`,import.meta.url))});
    } finally { await page.close(); }
  });
  test(`accepted Offer uses termination immediately above onboarding at ${width}px`, async () => {
    const page = await browser.newPage({ viewport:{width,height:1000} });
    try {
      await page.goto(`${baseUrl}__termination-entry?offer=1`);
      const entry = page.getByRole("button", {name:"放弃入职 / 终止本次招聘",exact:true});
      await entry.waitFor();
      assert.equal(await page.getByRole("button", {name:"撤回 Offer",exact:true}).count(),0);
      assert.ok(await entry.evaluate((button) => Boolean(button.nextElementSibling?.classList.contains('onboarding-panel'))));
      assert.equal(await page.evaluate(()=>Boolean(window.withdrawCalled)),false);
    } finally { await page.close(); }
  });
}

test("manager history remains readable without termination or single-cancel privilege", async () => {
  const page = await browser.newPage();
  try {
    await page.goto(`${baseUrl}__termination-entry?role=${encodeURIComponent("用人经理")}&tab=${encodeURIComponent("面试与反馈")}`);
    await page.getByRole("heading", {name:"面试记录",exact:true}).waitFor();
    assert.equal(await page.getByRole("button",{name:"终止本次招聘",exact:true}).count(),0);
    assert.equal(await page.getByRole("button",{name:"取消面试",exact:true}).count(),0);
    assert.equal(await page.getByText("测试面试官",{exact:false}).count()>0,true);
  } finally { await page.close(); }
});
