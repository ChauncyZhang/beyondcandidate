import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

test("cancelled Offer approval is historical, not waiting or a fabricated decision", async () => {
  const vite = await createServer({root:fileURLToPath(new URL("../",import.meta.url)),logLevel:"silent",server:{middlewareMode:true},plugins:[{
    name:"termination-history-test-export",enforce:"pre",
    transform(code,id){if(id.replaceAll("\\","/").endsWith('/src/OfferViews.jsx'))return `${code}\nexport { OfferHistory as TestOfferHistory, OnboardingSection as TestOnboardingSection };`;},
  }]});
  try {
    const {TestOfferHistory,TestOnboardingSection,offerErrorMessage}=await vite.ssrLoadModule('/src/OfferViews.jsx');
    const cancelled=renderToStaticMarkup(createElement(TestOfferHistory,{history:{approvals:[{id:'cancelled',status:'cancelled',sequence:1,assigneeId:'original-approver',reason:'申请已终止'}]}}));
    assert.match(cancelled,/已取消/);assert.match(cancelled,/无需审批/);
    assert.doesNotMatch(cancelled,/待审批|尚未处理|已批准|已退回修改/);
    assert.match(cancelled,/申请已终止/);assert.match(cancelled,/original-approver/);
    const normal=renderToStaticMarkup(createElement(TestOfferHistory,{history:{approvals:[
      {id:'approved',status:'approved',sequence:1,decidedAt:'2026-10-27T10:00:00Z'},
      {id:'rejected',status:'rejected',sequence:2,reason:'原审批意见'},
      {id:'waiting',status:'waiting',sequence:3},
      {id:'pending',status:'pending',sequence:4},
    ]}}));
    for(const label of ['已批准','已退回修改','原审批意见','等待前序审批','待审批','尚未处理'])assert.ok(normal.includes(label),label);
    for(const status of ['ready','submitted','failed']) {
      const stopped=renderToStaticMarkup(createElement(TestOnboardingSection,{onboarding:{status,blockingReason:'application_terminated',canSubmit:false,canUpdate:false,complete:true,instanceCode:'original-instance'},controller:{},onChanged(){},onNotify(){}}));
      assert.match(stopped,/本次申请已终止，不能继续办理/);assert.match(stopped,/original-instance/);assert.match(stopped,/历史资料和外部审批记录仍可查看/);
      assert.doesNotMatch(stopped,/预计到岗日 .* 到达后可办理入职/);
      if(status!=='submitted')assert.match(stopped,/<button[^>]*disabled=""/);
    }
    assert.match(offerErrorMessage({code:'application_termination_required'}),/请使用“放弃入职 \/ 终止本次招聘”/);
    assert.match(offerErrorMessage({code:'application_terminated'}),/本次申请已终止，不能继续办理/);
  } finally {await vite.close();}
});
