// Run with the bundled Node runtime. Does not contact the working site.
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(pathToFileURL(path.resolve(__dirname,'../public/report-preview.html')).href);
  await page.waitForSelector('tbody tr');
  assert.equal(await page.locator('tbody tr').count(),4);
  assert.match(await page.locator('tbody tr').nth(1).innerText(),/\b0\b/);
  assert.match(await page.locator('tbody tr').nth(2).innerText(),/—/);
  assert.match(await page.locator('tbody tr').nth(3).innerText(),/Ошибка/);
  await page.getByRole('button',{name:'Вид таблицы',exact:true}).click();
  await page.getByRole('button',{name:'Результат',exact:true}).click();
  await page.getByRole('button',{name:'Готово',exact:true}).click();
  assert.match(await page.locator('thead').innerText(),/ROI/);
  await page.reload();await page.waitForSelector('tbody tr');
  assert.match(await page.locator('thead').innerText(),/ROI/);
  await page.getByRole('button',{name:'Вид таблицы',exact:true}).click();
  await page.locator('details[data-group="binom"] summary').click();
  await page.getByRole('button',{name:'Выше: Лиды',exact:true}).click();
  assert.equal(await page.locator('details[data-group="binom"]').getAttribute('open'),'');
  await page.getByRole('button',{name:'Стандартный вид',exact:true}).click();
  await page.getByRole('button',{name:'Готово',exact:true}).click();
  const search=page.getByRole('searchbox');await search.fill('бесплатно');
  assert.equal(await page.locator('tbody tr').count(),1); // searches a hidden title
  await search.fill('');
  await page.getByRole('button',{name:/Уник. клики витрины/}).click();
  assert.match(await page.locator('tbody tr').first().innerText(),/Архивный вариант/);
  await page.locator('.dm-dates input').nth(0).fill('2026-10-09');
  await page.locator('.dm-dates input').nth(1).fill('2026-10-01');
  await page.getByRole('button',{name:'Применить',exact:true}).click();
  assert.match(await page.locator('.dm-notice').innerText(),/не позже/);
  await page.evaluate(async()=>{
   const second=document.createElement('div');document.body.append(second);
   const other=DomonetkaReport.create({root:second,userId:'other-user',localPreferences:true});await other.ready;
   if(other.getPreferences().sort.dir!=='desc')throw new Error('Preference isolation failed');other.destroy();
  });
  await page.getByRole('button',{name:'Обновить Binom',exact:true}).click();
  assert.match(await page.locator('.dm-notice').innerText(),/Обновление запущено/);
  await page.evaluate(async()=>{
   const affiliate=DomonetkaReport.adaptBinom({rows:[{scope:'variant',offer_id:'a',variant_id:'av',metrics:{leads:0},affiliate_id:'p',affiliate_name:'Моя ПП',approved:null}]});
   if(affiliate.rows[0].affiliate_id!=='p'||affiliate.rows[0].affiliate_name!=='Моя ПП'||affiliate.rows[0].approved!==null||!DomonetkaReport.columns.some(c=>c.id==='affiliate_name'&&c.group==='identity'))throw Error('Affiliate mapping failed');
   if(!DomonetkaReport.adaptBinom({rows:[],pending_jobs:[{status:'queued'}]}).sync.running)throw Error('Queued period job not running');
   const root=document.createElement('div');document.body.append(root);const calls=[];let localPeriod={from:'',to:''},pending=false;
   const table=DomonetkaReport.createBinom({root,userId:'bridge-test',streamId:'s',syncUserId:()=> 'owner-s',loadPreferences:async()=>null,savePreferences:async()=>{},onLocalPeriodChange:async p=>{calls.push('local');localPeriod=p;pending=true;},localData:()=>({period:localPeriod,offers:[{id:'other',stream_id:'other-stream'}],variants:[{id:'other-v',offer_id:'other',stream_id:'other-stream'}]}),request:async(url,body)=>{if(url==='/api/binom/sync'){if(body.user_id!=='owner-s')throw Error('Dynamic sync owner failed');calls.push('sync');return {status:'queued'};}calls.push('report');return {rows:[],columns:[],bindings:[],sync:[],period:localPeriod,pending_jobs:pending?[{status:'queued'}]:[]};}});
   try {await table.ready;if(root.querySelector('tbody').innerText.includes('other'))throw Error('Foreign local placeholder leaked');root.querySelector('.dm-toolbar button').click();await new Promise(r=>setTimeout(r,10));if(!calls.includes('sync'))throw Error('Sync not dispatched');calls.length=0;root.querySelector('.dm-dates button').click();await new Promise(r=>setTimeout(r,10));if(calls.join(',')!=='local,report')throw Error('Local period hook order failed');if(!root.querySelector('.dm-toolbar button').disabled)throw Error('Queued period not shown as running');await new Promise(r=>setTimeout(r,5200));if(calls.join(',')!=='local,report,report')throw Error('Pending job polling/hook count failed');}finally{table.destroy();root.remove();}
  });
  await page.evaluate(async()=>{
   const originalFetch=window.fetch,calls=[];window.fetch=async(url,init)=>{calls.push({url,init});return {ok:true,json:async()=>({preferences:null})};};
   const node=document.createElement('div');document.body.append(node);
   try {const server=DomonetkaReport.create({root:node,userId:'server-user'});await server.ready;server.setPreferences(server.getPreferences());await new Promise(r=>setTimeout(r,10));
    if(calls[0].url!=='/api/report/preferences'||calls[0].init.method!=='GET'||calls[1].init.method!=='POST'||!JSON.parse(calls[1].init.body).preferences)throw Error('Server preferences contract broken');server.destroy();
   }finally{window.fetch=originalFetch;node.remove();}
  });
  await page.evaluate(()=>{
   const data=DomonetkaReport.adaptBinom({rows:[{offer_id:'o',stream_id:'s',campaign_id:908,scope:'campaign',day:'all',metrics:{leads:1}},{offer_id:'o',stream_id:'s',campaign_id:908,scope:'variant',variant_id:'v',day:'all',metrics:{leads:0,unique_clicks:null},approved:null,statuses:[{one:'new',two:'',leads:0}],precision_warnings:['Нет точных уникальных']}],columns:[{key:'leads',name:'Лиды',decimals:0},{key:'custom_metric',name:'Custom',decimals:3}],bindings:[{offer_id:'o',campaign_id:908,status:'error',last_success:'2026-10-08T09:00:00Z',last_attempt:'2026-10-08T10:00:00Z',error:'Сбой'}],sync:[],period:{from:'',to:''}},{offers:[{id:'o',name:'Баннер'}],variants:[{id:'v',offer_id:'o',clicks:1,impressions:10}],period:{from:'2026-10-01',to:'2026-10-02'}});
   if(data.rows.length!==2||data.rows[1].leads!==0||data.rows[1].approved!==null)throw Error('Scopes or null semantics broken');
   if(data.rows[1].local_clicks!==undefined)throw Error('Mismatched local period joined');
   if(!data.columns.some(c=>c.id==='custom_metric')||!data.sync.error.includes('Нет точных уникальных'))throw Error('Dynamic columns or warnings missing');
  });
  await page.screenshot({path:path.resolve(__dirname,'../preview-report-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.resolve(__dirname,'../preview-report-mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(await page.locator('.dm-table-scroll').evaluate(n=>n.scrollWidth>n.clientWidth),true);
  assert.deepEqual(errors,[]);
  console.log('PASS: missing/zero/error, presets and reload, reorder, hidden-field search, sorting, date validation, user isolation, mobile layout.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
