// Actual customer UI + employee API/approval controls; local synthetic fixture only.
import assert from 'node:assert/strict';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {chromium} from '../.browser-tools/node_modules/playwright/index.mjs';

mkdirSync('.test-tmp/browser',{recursive:true});
process.env.TMPDIR=resolve('.test-tmp/browser');
const base='http://127.0.0.1:18082';
const out='evidence/support-gap-bridge';
const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
const page=await browser.newPage({viewport:{width:1280,height:1000}});
const original=JSON.parse(readFileSync(`${out}/evaluation-live.json`));
const old=original.cases.find(c=>c.case_id==='conversation-30');
const mode=process.argv[2]||'before';
await page.goto(`${base}/customer/runs/${old.run.run_id}`);
await page.getByText('Final answer',{exact:true}).waitFor();
if(mode==='before') {
  await page.getByText('Your refund request for order ORD-3015 is under review.',{exact:false}).waitFor();
  await page.screenshot({path:`${out}/amount-change-original-ui.png`,fullPage:true});
  writeFileSync(`${out}/amount-change-before-browser.json`,JSON.stringify({run:old.run,body:await page.locator('body').innerText(),api:await(await page.request.get(`${base}/runs/${old.run.run_id}`)).json()},null,2)+'\n');
} else {
  assert.match(await page.locator('.answer-card').innerText(),/amount changed.*new review/i);
  await page.screenshot({path:`${out}/amount-change-legacy-projection.png`,fullPage:true});
  const orderNumber='ORD-4001';
  const existingOrders=await(await page.request.get(`${base}/orders`)).json();
  let order=existingOrders.orders.find(o=>o.order_number===orderNumber);
  if(!order){
    const create=await page.request.post(`${base}/orders`,{data:{order_number:orderNumber,customer_id:1,product_id:1,quantity:1,ordered_at:new Date().toISOString(),status:'delivered',refund_status:'none'}});
    assert.equal(create.status(),201);
    order=await create.json();
  }
  const priorRuns=await(await page.request.get(`${base}/runs?limit=100`)).json();
  let run=priorRuns.runs.find(r=>r.message_preview===`Refund my damaged 4K order ${orderNumber}.`);
  if(!run){
  await page.goto(base);
  await page.getByText('Check your orders',{exact:true}).click();
  await page.getByLabel('Name',{exact:true}).fill('Maya Chen');
  await page.getByLabel('Email',{exact:true}).fill('maya@example.test');
  await page.getByRole('button',{name:'Find my orders'}).click();
  await page.getByRole('heading',{name:'My orders',exact:true}).waitFor();
  await page.getByLabel('What is this about?').selectOption(orderNumber);
  await page.getByLabel('Message',{exact:true}).fill(`Refund my damaged 4K order ${orderNumber}.`);
  const submission=page.waitForResponse(r=>r.url().endsWith('/runs')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Start support request'}).click();
  run=await(await submission).json();
  }
  async function poll(status){for(let i=0;i<300;i++){const state=await(await page.request.get(`${base}/runs/${run.run_id}`)).json();if(state.status===status)return state;assert.notEqual(state.status,'failed',JSON.stringify(state));await page.waitForTimeout(100);}throw new Error('Timeout');}
  const paused=await poll('awaiting_approval');
  assert.equal(paused.proposed_refund.amount_cents,2999);
  const update=await page.request.put(`${base}/orders/${order.id}`,{data:{order_number:orderNumber,customer_id:1,product_id:1,quantity:2,ordered_at:order.ordered_at,status:'delivered',refund_status:'none'}});
  assert.equal(update.status(),200);
  await page.goto(`${base}/employees/approvals`);
  const card=page.locator('article.inbox-item').filter({hasText:run.run_id});
  await card.getByRole('button',{name:'Approve refund',exact:true}).waitFor();
  await page.screenshot({path:`${out}/amount-change-paused-before-decision.png`,fullPage:true});
  await card.getByRole('button',{name:'Approve refund',exact:true}).click();
  const completed=await poll('completed');
  assert.match(completed.answer,/amount changed.*new review/i);
  assert.doesNotMatch(completed.answer,/under review|will update/i);
  const rows=await(await page.request.get(`${base}/orders`)).json();
  assert.equal(rows.orders.find(o=>o.order_number===orderNumber).refund_status,'none');
  await page.goto(`${base}/customer/runs/${run.run_id}`);
  await page.getByText('Final answer',{exact:true}).waitFor();
  await page.screenshot({path:`${out}/amount-change-refusal-desktop.png`,fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:`${out}/amount-change-refusal-mobile.png`,fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  writeFileSync(`${out}/amount-change-after-browser.json`,JSON.stringify({original_run:old.run,new_run:run,paused,completed,order_after:rows.orders.find(o=>o.order_number===orderNumber),body:await page.locator('body').innerText()},null,2)+'\n');
}
await browser.close();
console.log(`Amount-change real UI/API ${mode}: PASS`);
