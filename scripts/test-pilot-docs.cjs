/* Pruebas de la candidata documental: solo consultas; ninguna VM se aprovisiona. */
const {chromium, firefox} = require('playwright');
const {expect: baseExpect} = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const expect = baseExpect.configure({timeout:10000});
const base = process.env.IACP_DOCS_BASE || 'http://127.0.0.1:8765';
const output = process.env.IACP_DOCS_EVIDENCE_DIR;
const bearerMode = process.env.IACP_DOCS_AUTH === 'bearer';
// En HTTPS real leer el secreto por stdin privado; nunca por argumentos o archivos Git.
const token = bearerMode ? fs.readFileSync(0,'utf8').trim() : 'identidad-sintetica-solo-pruebas-documentales';
if (!token) throw Error('Falta identidad autorizada');
if (!output) throw Error('Falta IACP_DOCS_EVIDENCE_DIR');
fs.mkdirSync(output,{recursive:true});
async function main() {
 const results=[];
 for (const mode of ['chromium-escritorio','chromium-movil','firefox-escritorio']) {
  console.log('Comprobando '+mode);
  const mobile=mode.includes('movil'),engine=mode.startsWith('firefox')?firefox:chromium;
  const browser=await engine.launch({headless:true});
  try {
   const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1536,height:1000},...(bearerMode ? {extraHTTPHeaders:{Authorization:'Bearer '+token}} : {httpCredentials:{username:'iacp',password:token}})});
   if(engine===chromium) await context.grantPermissions(['clipboard-read','clipboard-write']);
   const page=await context.newPage(),errors=[],external=[],mutations=[],positions=[];
   page.on('pageerror',e=>errors.push(e.message));
   page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
   await context.route('**/*',route=>{const r=route.request();if(new URL(r.url()).origin!==new URL(base).origin){external.push(r.url());return route.abort();}if(!['GET','HEAD','OPTIONS'].includes(r.method())){mutations.push(r.method());return route.abort();}return route.continue();});
   await page.goto(base+'/docs',{waitUntil:'networkidle'});
   await expect(page.locator('#pilot-title')).toBeInViewport();
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
   await expect(page.locator('#operations-tag-jobs')).toBeAttached();
   await page.screenshot({path:path.join(output,mode+'-inicio.png')});
   const sidebar=async selector=>{if(mobile&&!await page.locator('#doc-navigation').isVisible())await page.locator('.menu-toggle').click();await page.locator('#doc-navigation').locator(selector).click();if(mobile)await expect(page.locator('#doc-navigation')).not.toBeVisible();};
   const positioned=async selector=>{
    await expect.poll(()=>page.locator(selector).evaluate(e=>{const r=e.getBoundingClientRect(),h=document.querySelector('.masthead').getBoundingClientRect();return r.top>=h.bottom+15&&r.top<=h.bottom+32;})).toBe(true).catch(async e=>{console.error(JSON.stringify({selector,url:page.url(),bounds:await page.locator(selector).boundingBox(),header:await page.locator('.masthead').boundingBox()}));throw e;});
    positions.push(await page.locator(selector).evaluate(e=>({id:e.id,top:e.getBoundingClientRect().top,header_bottom:document.querySelector('.masthead').getBoundingClientRect().bottom})));
   };
   const root=page.locator('#pilot-panel');
   await expect(root).toHaveAttribute('open','');
   await root.locator(':scope > summary').click();
   await expect(page.locator('#pilot-json')).toBeHidden();
   await page.screenshot({path:path.join(output,mode+'-piloto-minimizado.png')});
   await sidebar('[data-doc-target="inicio"]');
   await expect(root).toHaveAttribute('open','');
   for(const step of ['piloto-json','piloto-curl','piloto-respuesta','piloto-seguimiento','piloto-ssh','piloto-lifecycle','piloto-delete']) {
    const panel=page.locator('#step-'+step);
    if(await panel.getAttribute('open')!==null)await panel.locator(':scope > summary').click();
    await expect(panel.locator('.pilot-step-content')).toBeHidden();
    await page.locator(`.pilot .guide-toc a[href="#${step}"]`).click();
    await expect(panel).toHaveAttribute('open','');await positioned('#'+step);
   }
   for(const id of ['pilot-json','pilot-curl']){
    const step=id==='pilot-json'?'piloto-json':'piloto-curl';
    await page.locator(`.pilot .guide-toc a[href="#${step}"]`).click();

    await page.locator(`[data-copy-target="${id}"]`).click();
    await expect(page.locator('#copy-status')).toContainText('copiado completo');
    if(engine===chromium)expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe(await page.locator('#'+id).textContent());
   }
   const body=JSON.parse(await page.locator('#pilot-json').textContent());
   expect(body.plan_id).toBe('kvm-1');expect(body.image_id).toBe('ubuntu-2404');
   expect(Object.keys(body).sort()).toEqual(['access','image_id','name','plan_id']);
   await page.locator('#pilot-json').scrollIntoViewIfNeeded();
   await expect(page.locator('#pilot-json')).toHaveCSS('background-color','rgba(0, 0, 0, 0)');
   await page.screenshot({path:path.join(output,mode+'-json.png')});
   await page.locator('#pilot-curl').scrollIntoViewIfNeeded();
   await page.screenshot({path:path.join(output,mode+'-curl.png')});
   for(const tag of ['instances','operations','kvm-lifecycle','disposable-deletion','observability','jobs','lab-administration']){
    await sidebar(`[data-tag-target="${tag}"]`);
    await expect(page).toHaveURL(new RegExp('#/'+tag+'$'));
    await positioned('#operations-tag-'+tag);
    await expect(page.locator(`[data-tag-target="${tag}"]`)).toHaveClass(/active/);
    const section=page.locator('#operations-tag-'+tag).locator('..');
    await expect(section.locator('.opblock').first()).toBeVisible();
    // Comprobar también que el menú reabre un grupo cerrado de verdad.
    await page.locator('#operations-tag-'+tag).click();
    await expect(page.locator('#operations-tag-'+tag)).toHaveAttribute('data-is-open','false');
    await sidebar(`[data-tag-target="${tag}"]`);
    await expect(page.locator('#operations-tag-'+tag)).toHaveAttribute('data-is-open','true');
    await positioned('#operations-tag-'+tag);
    await page.screenshot({path:path.join(output,mode+'-'+tag+'.png')});
    await page.reload({waitUntil:'networkidle'});await positioned('#operations-tag-'+tag);
   }
   for(const id of ['guia-crm','guia-operaciones','guia-despliegue']){
    await sidebar(`[data-doc-target="${id}"]`);await positioned('#'+id);
    await expect(page.locator('#'+id+' .guide-content')).toBeVisible();
    await expect(page.locator(`[data-doc-target="${id}"]`).first()).toHaveClass(/active/);
    await page.reload({waitUntil:'networkidle'});await positioned('#'+id);
   }
   await page.goto(base+'/docs#/guia-crm',{waitUntil:'networkidle'});await positioned('#guia-crm');
   await page.goto(base+'/docs#crm-idempotencia',{waitUntil:'networkidle'});await positioned('#crm-idempotencia');
   await sidebar('[data-doc-target="inicio"]');
   await page.locator('a[href="#piloto-seguimiento"]').click();await positioned('#piloto-seguimiento');
   await page.locator('[data-operation-id="getOperation"]').click();
   await expect(page.locator('#operations-operations-getOperation')).toHaveClass(/is-open/);
   await positioned('#operations-operations-getOperation');
   await page.goBack();await expect(page).toHaveURL(/#piloto-seguimiento$/);await positioned('#piloto-seguimiento');
   await page.goForward();await expect(page).toHaveURL(/#\/operations\/getOperation$/);await positioned('#operations-operations-getOperation');
   const historyBefore=await page.evaluate(()=>history.length);
   await page.reload({waitUntil:'networkidle'});await positioned('#operations-operations-getOperation');
   expect(await page.evaluate(()=>history.length)).toBe(historyBefore);
   await sidebar('[data-tag-target="instances"]');
   await page.locator('#endpoint-search').fill('/v1/images');
   await expect(page.locator('#operations-instances-listGoldenImages')).toBeVisible();
   await sidebar('[data-tag-target="jobs"]');await expect(page.locator('#endpoint-search')).toHaveValue('');
   // Abrir Try it out de una lectura autorizada, nunca Execute de una mutación.
   await sidebar('[data-tag-target="instances"]');
   await page.locator('.btn.authorize').first().click();
   await page.locator('.auth-container input').fill(token);
   await page.locator('.auth-btn-wrapper button.authorize').click();
   await page.locator('.auth-btn-wrapper button.btn-done').click();
   const op=page.locator('#operations-instances-listGoldenImages');
   await op.locator('.opblock-summary-control').click();
   await op.locator('button.try-out__btn').click();
   await op.locator('button.execute').click();
   await expect(op.locator('.live-responses-table tbody .response-col_status')).toContainText('200');
   if(mobile&&!await page.locator('#doc-navigation').isVisible())await page.locator('.menu-toggle').click();
   const popupEvent=context.waitForEvent('page');
   await page.locator('#doc-navigation a[href="/openapi.json"]').click();
   const popup=await popupEvent;await popup.waitForLoadState();expect(popup.url()).toBe(base+'/openapi.json');await popup.close();
   expect(await page.locator('#doc-navigation a[href^="https://scm.cyberfuel.com/"]').getAttribute('target')).toBe('_blank');
   const contract=await context.request.get(base+'/openapi.json');expect(contract.status()).toBe(200);expect((await contract.json()).openapi).toBe('3.1.0');
   expect(errors).toEqual([]);expect(external).toEqual([]);expect(mutations).toEqual([]);
   results.push({mode,estado:'PASS',positions,clipboard:engine===chromium?'Lectura real del portapapeles coincide exactamente':'writeText nativo completado; lectura no soportada por permisos Playwright Firefox',try_it_out:'GET /v1/images HTTP 200',errors,external,mutations});
  } finally {await browser.close();}
 }
 fs.writeFileSync(path.join(output,'playwright.json'),JSON.stringify({fecha_local:new Date().toLocaleString('es-CR',{timeZone:'America/Costa_Rica'}),zona:'America/Costa_Rica',alcance:bearerMode?'HTTPS real; registrar por separado SHA efectivamente ejecutado':'Candidata local; no certifica despliegue VM 104',results},null,2));
 console.log(JSON.stringify({estado:'PASS',modos:results.length}));
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
