import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

let vite, browser, baseUrl;
const preview = (extra = {}) => ({ application_id:"a",candidate_id:"c",job_id:"j",version:5,stage:"hired",preview_version:"fingerprint-1",
  counts:{review_tasks:2,interviews:3,feedback_tasks:4,offers:1,approvals:1,queued_messages:2,onboarding:1},
  requires_not_onboarded_confirmation:true,allowed_actions:{terminate:true,confirm_external:false},termination:null,
  external_items:[{kind:"feishu_onboarding",resource_id:"onboarding-1",reference:"instance-1",generation:3,state:"needs_action"}],...extra });
const terminated = (state = "needs_action", version = 8) => preview({stage:"withdrawn",requires_not_onboarded_confirmation:false,
  allowed_actions:{terminate:false,confirm_external:true},termination:{id:"t",version,source_stage:"hired",reason_text:"候选人退出",terminated_at:"2026-10-28T10:00:00Z",external_status:"pending",
    external_items:[{kind:"feishu_onboarding",resource_id:"onboarding-1",reference:"instance-1",generation:3,state}],resolution_history:[]} });
const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {TerminationEntry,TerminationDialog,PendingTerminationPanel,InterviewCancellationEntry} from '/src/TerminationViews.jsx';
import '/src/styles.css';
const mode=new URLSearchParams(location.search).get('mode');
const props={applicationId:'a',candidateName:'测试候选人',jobTitle:'测试职位',role:new URLSearchParams(location.search).get('role')||'HR 招聘专员'};
const changed=value=>{window.changed=value;};
const root=createRoot(document.getElementById('fixture'));
window.unmountFixture=()=>root.unmount();
root.render(mode==='pending'
  ? React.createElement(PendingTerminationPanel,{role:props.role,onOpen:value=>{window.opened=value;}})
  : mode==='interview' ? React.createElement(InterviewCancellationEntry,{...props,interview:{interviewId:'i',version:7,status:'待评价',workflowStatus:'已确认',feedbackStatus:'待反馈'},onChanged:changed})
  : mode==='direct' ? React.createElement(TerminationDialog,{...props,onChanged:changed,onClose:()=>{}})
  : React.createElement(TerminationEntry,{...props,onChanged:changed}));
requestAnimationFrame(()=>requestAnimationFrame(()=>{window.fixtureReady=true;}));
`;
before(async () => {
  vite = await createServer({root:fileURLToPath(new URL("../",import.meta.url)),logLevel:"silent",server:{host:"127.0.0.1",port:0},plugins:[{
    name:"termination-browser-fixture",
    resolveId(id){if(id==='virtual:termination-browser')return '\0termination-browser';},
    load(id){if(id==='\0termination-browser')return fixture;},
    configureServer(server){server.middlewares.use(async(req,res,next)=>{
      if(!req.url.startsWith('/__termination-browser'))return next();
      res.setHeader('Content-Type','text/html');res.end(await server.transformIndexHtml(req.url,'<html><head></head><body><div id="fixture"></div><script type="module" src="/@id/__x00__termination-browser"></script></body></html>'));
    });},
  }]});
  await vite.listen();baseUrl=vite.resolvedUrls.local[0];browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();await vite?.close();});
async function open(width=1440,mode=""){
  const page=await browser.newPage({viewport:{width,height:900}});
  await page.route('**/api/**',route=>route.fulfill({status:500,json:{}}));
  return {page,goto:()=>page.goto(`${baseUrl}__termination-browser?mode=${mode}`)};
}
async function openModal(page){await page.getByRole('button',{name:'终止本次招聘',exact:true}).click();await page.getByLabel('终止原因（必填）').waitFor();}

for(const width of [1440,390]){
  test(`termination preview, validation, conflict recovery and result at ${width}px`,async()=>{
    const {page,goto}=await open(width);let current=preview(),posts=[];
    try{
      await page.route('**/api/v1/applications/a/termination',async route=>{
        if(route.request().method()==='GET')return route.fulfill({json:{data:current}});
        posts.push({body:route.request().postDataJSON(),headers:route.request().headers()});
        if(posts.length===1){current=preview({version:6,preview_version:'fingerprint-2',counts:{interviews:9}});return route.fulfill({status:409,contentType:'application/problem+json',body:JSON.stringify({code:'termination_preview_changed'})});}
        current=terminated();return route.fulfill({json:{data:current}});
      });
      await goto();await openModal(page);
      assert.equal(await page.getByText('飞书入职审批 · 需人工处理',{exact:true}).count(),1);
      assert.equal(await page.getByText('实例 / 参考：instance-1',{exact:true}).count(),1);
      assert.match(await page.getByRole('dialog').innerText(),/不影响其他职位申请/);
      assert.match(await page.getByRole('dialog').innerText(),/旧流程不能恢复/);
      assert.match(await page.getByRole('dialog').innerText(),/不会自动发送新的候选人邮件/);
      const submit=page.getByRole('button',{name:'确认终止本次招聘',exact:true});
      assert.equal(await submit.isDisabled(),true);
      await page.getByLabel('终止原因（必填）').fill('候选人另有安排');assert.equal(await submit.isDisabled(),true);
      await page.getByRole('checkbox').check();await submit.click();
      await page.getByRole('alert').waitFor();assert.equal(await page.getByLabel('终止原因（必填）').inputValue(),'候选人另有安排');assert.equal(await submit.isDisabled(),true);
      await page.getByRole('button',{name:'重新加载预览'}).click();
      await page.getByText('9',{exact:true}).waitFor();assert.equal(await page.getByLabel('终止原因（必填）').inputValue(),'候选人另有安排');
      await submit.click();await page.getByRole('heading',{name:'本次招聘已终止',exact:true}).waitFor();
      assert.equal(posts.length,2);assert.equal(posts[0].headers['if-match'],'"5"');assert.equal(posts[1].headers['if-match'],'"6"');
      assert.equal(posts[0].body.preview_version,'fingerprint-1');assert.equal(posts[1].body.preview_version,'fingerprint-2');assert.equal(posts[1].body.confirm_not_onboarded,true);
      assert.ok(posts[0].headers['idempotency-key']);assert.notEqual(posts[0].headers['idempotency-key'],posts[1].headers['idempotency-key']);
      await page.getByRole('button',{name:'关闭终止确认'}).click();assert.equal((await page.evaluate(()=>window.changed)).stage,'withdrawn');
      assert.equal(await page.getByRole('button',{name:'终止本次招聘',exact:true}).evaluate(el=>el===document.activeElement),true);
    }finally{await page.close();}
  });

  test(`independent external workbench and versioned manual resolution at ${width}px`,async()=>{
    const {page,goto}=await open(width,'pending');let current=terminated(),pending=true,post;
    try{
      await page.route('**/api/v1/application-terminations/pending',route=>route.fulfill({json:{data:{items:pending?[{application_id:'a',candidate_id:'c',candidate_name:'已退出候选人',job_id:'j',job_title:'历史职位',version:8,external_items:current.termination.external_items}]:[],total:pending?1:0}}}));
      await page.route('**/api/v1/applications/a/termination',route=>route.fulfill({json:{data:current}}));
      await page.route('**/api/v1/applications/a/termination/external-resolution',route=>{
        post={body:route.request().postDataJSON(),headers:route.request().headers()};pending=false;
        current=preview({stage:'withdrawn',allowed_actions:{terminate:false,confirm_external:false},termination:{...current.termination,version:9,external_status:'resolved',external_items:[]}});
        return route.fulfill({json:{data:current}});
      });
      await goto();await page.getByText('已退出候选人 · 历史职位',{exact:true}).waitFor();
      await page.getByRole('button',{name:'查看申请历史'}).click();assert.equal((await page.evaluate(()=>window.opened)).applicationId,'a');
      await page.getByRole('button',{name:'查看终止记录与外部收尾'}).click();
      await page.getByLabel('外部处理说明（必填）').fill('已由飞书管理员撤销实例并核对');
      await page.getByRole('button',{name:'确认外部处理完成',exact:true}).click();
      await page.getByText('暂无待处理外部事项',{exact:true}).waitFor();
      assert.equal(post.headers['if-match'],'"8"');assert.ok(post.headers['idempotency-key']);assert.equal(post.body.reason_text,'已由飞书管理员撤销实例并核对');
    }finally{await page.close();}
  });

  test(`pending-feedback single cancellation retains reason and displayed version at ${width}px`,async()=>{
    const {page,goto}=await open(width,'interview');let requests=[];
    try{
      await page.route('**/api/v1/interviews/i/transitions',route=>{
        requests.push({body:route.request().postDataJSON(),headers:route.request().headers()});
        return route.fulfill({json:{data:{id:'i',application_id:'a',candidate:{id:'c'},job:{id:'j'},status:'cancelled',version:8,email_delivery:{status:'not_sent',safe_error_code:'interview_email_invalid'}}}});
      });
      await goto();await page.getByRole('button',{name:'取消面试',exact:true}).click();
      const submit=page.getByRole('button',{name:'仅取消本场面试',exact:true});assert.equal(await submit.isDisabled(),true);
      assert.equal(await page.getByLabel('操作原因（必填）').getAttribute('maxlength'),'1000');
      await page.getByLabel('操作原因（必填）').fill('本场时间不合适');await submit.click();
      await page.getByText('已取消，需人工通知候选人；外部同步状态请在面试记录中核对。',{exact:true}).waitFor();
      assert.equal(requests.length,1);assert.equal(requests[0].headers['if-match'],'"7"');assert.ok(requests[0].headers['idempotency-key']);assert.deepEqual(requests[0].body,{target:'cancelled',reason:'本场时间不合适'});
    }finally{await page.close();}
  });
}

for(const state of ['in_flight','unknown'])test(`external ${state} cannot be acknowledged even if server action is true`,async()=>{
  const {page,goto}=await open();try{
    await page.route('**/api/v1/applications/a/termination',route=>route.fulfill({json:{data:terminated(state)}}));
    await goto();await page.getByRole('button',{name:'终止本次招聘',exact:true}).click();
    await page.getByRole('heading',{name:'本次招聘已终止',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'确认外部处理完成',exact:true}).count(),0);
    assert.match(await page.getByRole('dialog').innerText(),/联系系统管理员/);assert.match(await page.getByRole('dialog').innerText(),/不要重新发起入职审批/);
    await page.keyboard.press('Escape');await page.getByRole('dialog').waitFor({state:'detached'});
  }finally{await page.close();}
});

for(const width of [1440,390])test(`read-only reconciliation queues, refreshes and preserves manual reason through a version race at ${width}px`,async()=>{
  const {page,goto}=await open(width);let current=terminated('unknown',8),queued,posts=[];
  current.allowed_actions={terminate:false,confirm_external:false,reconcile_external:true};
  try{
    await page.route('**/api/v1/applications/a/termination',route=>route.fulfill({json:{data:current}}));
    await page.route('**/api/v1/applications/a/termination/reconcile-external',route=>{
      queued={body:route.request().postDataJSON(),headers:route.request().headers()};
      return route.fulfill({json:{data:current}});
    });
    await page.route('**/api/v1/applications/a/termination/external-resolution',route=>{
      posts.push({body:route.request().postDataJSON(),headers:route.request().headers()});
      if(posts.length===1){current=terminated('needs_action',10);return route.fulfill({status:409,contentType:'application/problem+json',body:JSON.stringify({code:'resource_version_conflict'})});}
      current=terminated('resolved',11);current.termination.external_status='resolved';current.allowed_actions={terminate:false,confirm_external:false,reconcile_external:false};
      return route.fulfill({json:{data:current}});
    });
    await goto();await page.getByRole('button',{name:'终止本次招聘',exact:true}).click();
    const reason=page.getByLabel('外部处理说明（必填）');await reason.fill('原请求核对后由管理员处理');
    await page.getByRole('button',{name:'核对飞书结果',exact:true}).click();
    await page.getByText('核对任务已提交，请稍后刷新',{exact:true}).waitFor();
    assert.equal(queued.headers['if-match'],'"8"');assert.ok(queued.headers['idempotency-key']);assert.deepEqual(queued.body,{reason_text:'核对飞书原请求结果'});
    assert.equal(await page.getByRole('button',{name:'确认外部处理完成',exact:true}).count(),0);
    assert.equal(await reason.inputValue(),'原请求核对后由管理员处理');
    await page.getByRole('button',{name:'刷新处理状态',exact:true}).click();
    await page.getByText('飞书入职审批 · 结果未知',{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'确认外部处理完成',exact:true}).count(),0);
    assert.equal(await reason.inputValue(),'原请求核对后由管理员处理');
    current=terminated('needs_action',9);
    await page.getByRole('button',{name:'刷新处理状态',exact:true}).click();
    const resolve=page.getByRole('button',{name:'确认外部处理完成',exact:true});await resolve.waitFor();
    assert.equal(await reason.inputValue(),'原请求核对后由管理员处理');
    await resolve.click();await page.getByRole('alert').waitFor();assert.equal(await resolve.isDisabled(),true);
    assert.equal(await reason.inputValue(),'原请求核对后由管理员处理');
    await page.getByRole('button',{name:'刷新处理状态',exact:true}).click();await resolve.waitFor();
    await resolve.click();await page.getByRole('heading',{name:'外部收尾已确认',exact:true}).waitFor();
    assert.deepEqual(posts.map(post=>post.headers['if-match']),['"9"','"10"']);
    assert.ok(posts.every(post=>post.body.reason_text==='原请求核对后由管理员处理'));
  }finally{await page.close();}
});

test('reconciliation remains server-gated and forbidden roles receive no action',async()=>{
  const {page,goto}=await open();try{
    const current=terminated('unknown');current.allowed_actions={terminate:false,confirm_external:false,reconcile_external:false};
    await page.route('**/api/v1/applications/a/termination',route=>route.fulfill({json:{data:current}}));
    await goto();await page.getByRole('button',{name:'终止本次招聘',exact:true}).click();await page.getByRole('heading',{name:'本次招聘已终止',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'核对飞书结果',exact:true}).count(),0);
    await page.getByRole('button',{name:'刷新处理状态',exact:true}).waitFor();
    for(const role of ['用人经理','面试官','系统管理员']){
      await page.goto(`${baseUrl}__termination-browser?role=${encodeURIComponent(role)}`);
      await page.waitForFunction(()=>window.fixtureReady===true);
      assert.equal(await page.getByRole('button',{name:/终止本次招聘|核对飞书结果|刷新处理状态/}).count(),0);
    }
  }finally{await page.close();}
});

test('a queued reconciliation response after navigation cannot update the closed workflow',async()=>{
  const {page,goto}=await open(1440,'direct');let release,observed;
  const gate=new Promise(resolve=>{release=resolve;}),seen=new Promise(resolve=>{observed=resolve;});
  try{
    const current=terminated('unknown');current.allowed_actions={terminate:false,confirm_external:false,reconcile_external:true};
    await page.route('**/api/v1/applications/a/termination',route=>route.fulfill({json:{data:current}}));
    await page.route('**/api/v1/applications/a/termination/reconcile-external',async route=>{observed();await gate;await route.fulfill({json:{data:current}});});
    await goto();
    await page.getByRole('button',{name:'核对飞书结果',exact:true}).click();await seen;
    await page.evaluate(()=>window.unmountFixture());
    const response=page.waitForResponse('**/api/v1/applications/a/termination/reconcile-external');release();await response;
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.equal(await page.evaluate(()=>window.changed),undefined);
    assert.equal(await page.getByText('核对任务已提交，请稍后刷新',{exact:true}).count(),0);
  }finally{release();await page.close();}
});
