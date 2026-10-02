// Authorized task-scoped screenshot fallback: installed Chrome, synthetic local API.
// npm install --prefix .browser-tools playwright
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { chromium } from '../.browser-tools/node_modules/playwright/index.mjs';

const base = 'http://127.0.0.1:18082';
const output = 'evidence/support-gap-bridge';
mkdirSync(output, {recursive:true});
const browser = await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless:true});
const page = await browser.newPage({viewport:{width:1280,height:1000}});
const observations = [];
const errors = [];
page.on('pageerror', error => errors.push(error.message));
async function capture(name) {
  await page.screenshot({path:`${output}/${name}.png`, fullPage:true});
  observations.push({name,url:page.url(),viewport:page.viewportSize(),overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),text:await page.locator('body').innerText()});
  assert.equal(observations.at(-1).overflow,false);
}
async function waitRun(id, status) {
  for(let i=0;i<200;i++){
    const response=await page.request.get(`${base}/runs/${id}`);
    const run=await response.json();
    assert.notEqual(run.status,'failed',JSON.stringify(run));
    if(run.status===status)return run;
    await page.waitForTimeout(100);
  }
  throw new Error(`Timeout ${id}`);
}
await page.goto(base);
await page.getByText('Check your orders',{exact:true}).click();
await page.getByLabel('Name',{exact:true}).fill('Maya Chen');
await page.getByLabel('Email',{exact:true}).fill('maya@example.test');
await page.getByRole('button',{name:'Find my orders'}).click();
await page.getByRole('heading',{name:'My orders',exact:true}).waitFor();
await capture('customer-orders-desktop');
const runs=[];
for(let i=0;i<2;i++){
  await page.goto(base);
  await page.getByLabel('What is this about?').selectOption('ORD-1001');
  await page.getByLabel('Message',{exact:true}).fill(`Refund my damaged 4K order ORD-1001. Conversation ${i+1}.`);
  const submitted=page.waitForResponse(r=>r.url().endsWith('/runs')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Start support request'}).click();
  const info=await(await submitted).json();
  runs.push(info);
  await waitRun(info.run_id,'awaiting_approval');
}
await page.goto(`${base}/employees/approvals`);
await page.getByRole('button',{name:'Approve refund'}).first().waitFor();
await capture('employee-approval-desktop');
await page.setViewportSize({width:390,height:844});
await capture('employee-approval-mobile');
await page.setViewportSize({width:1280,height:1000});
// Choose the card by exact run ID, not list order.
const first=page.locator('article.inbox-item').filter({hasText:runs[0].run_id});
await first.getByRole('button',{name:'Approve refund',exact:true}).click();
await waitRun(runs[0].run_id,'completed');
const second=page.locator('article.inbox-item').filter({hasText:runs[1].run_id});
await second.getByRole('button',{name:'Reject refund',exact:true}).click();
const completed=await waitRun(runs[1].run_id,'completed');
assert.match(completed.answer,/already.*approved/i);
await page.goto(base);
await page.locator('.order-card').filter({hasText:'ORD-1001'}).waitFor();
const card=page.locator('.order-card').filter({hasText:'ORD-1001'});
assert.match(await card.innerText(),/approved/);
await capture('customer-approved-desktop');
await page.setViewportSize({width:390,height:844});
await capture('customer-approved-mobile');
await page.goto(`${base}/employees/approvals`);
await page.getByRole('heading',{name:'Refunds waiting for a person.'}).waitFor();
await capture('employee-inbox-mobile');
assert.deepEqual(errors,[]);
writeFileSync(`${output}/browser-observations.json`,JSON.stringify({method:'task-scoped Playwright with installed Chrome; screenshot fallback after bridge filePath denial',runs,observations,errors},null,2)+'\n');
await browser.close();
console.log('Actual customer and employee screens, two conversations, approve/reject, desktop/mobile: PASS');
