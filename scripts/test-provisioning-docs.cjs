/* Configurador real con transporte simulado: no crea recursos de infraestructura. */
const {chromium,firefox}=require('playwright');
const {expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const base=process.env.IACP_DOCS_BASE||'http://127.0.0.1:8765';
const output=process.env.IACP_DOCS_EVIDENCE_DIR;
if(!output)throw Error('Falta directorio de evidencia');
fs.mkdirSync(output,{recursive:true});
const password='Solo-prueba-documental-2026!';
const plans=[1,2,3,4].map((n,i)=>({plan_id:'kvm-'+n,name:'KVM '+n,vcpus:[1,2,4,8][i],memory_mb:2048*2**i,disk_gb:50*2**i,availability:{available:true,reasons:[]}}));
const images=['ubuntu-2404','ubuntu-2604','almalinux-9','almalinux-10'].map(id=>({image_id:id,operating_system:id,version:id,authorized:true,compatible_plans:plans.map(p=>p.plan_id),availability:{available:true,reasons:[]}}));
(async()=>{
 const results=[];
 for(const mode of ['chromium-escritorio','chromium-movil','firefox-escritorio']){
  const engine=mode.startsWith('firefox')?firefox:chromium;
  const browser=await engine.launch({headless:true});
  try{
   const context=await browser.newContext({viewport:mode.includes('movil')?{width:390,height:844}:{width:1536,height:1000},httpCredentials:{username:'iacp',password:'identidad-sintetica-solo-pruebas-documentales'}});
   const sent=[],errors=[];
   await context.addInitScript(()=>{window.testClipboard='';Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.testClipboard=text;}}});});
   await context.route('**/v1/**',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(['POST','DELETE'].includes(request.method())){
     sent.push({key:request.headers()['idempotency-key'],body:request.postDataJSON()});
     return route.fulfill({status:202,json:{instance_id:'11111111-1111-4111-8111-111111111111',operation_id:'22222222-2222-4222-8222-222222222222',status:'queued'}});
    }
    let json=url.pathname.endsWith('/power')?{power_state:'stopped',mutations_available:true}:url.pathname==='/v1/plans'?plans:url.pathname==='/v1/images'?images:url.pathname==='/v1/lab/usage'?{tenant_id:'fixture-tenant'}:url.pathname.startsWith('/v1/operations/')?{status:'completed',normalized_status:'completed'}:{status:'ready',ip_addresses:['192.0.2.10'],access:{status:'completed'}};
    return route.fulfill({status:200,json});
   });
   const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
   await page.goto(base+'/docs',{waitUntil:'networkidle'});
   await expect(page.locator('#pilot-title')).toHaveText('Crear máquina virtual');
   await expect(page.locator('#pilot-execute')).toBeDisabled();
   await page.locator('#load-pilot-catalog').click();
   await expect(page.locator('#pilot-catalog-status')).toContainText('Authorize');
   await page.evaluate(()=>window.ui.preauthorizeApiKey('HTTPBearer','identidad-sintetica-solo-pruebas-documentales'));
   await page.locator('#load-pilot-catalog').click();
   await expect(page.locator('#pilot-catalog-status')).toContainText('Catálogo real cargado');
   await page.locator('#pilot-name').fill('prueba-configurador');
   await page.locator('#pilot-password').fill(password);
   const key=await page.locator('#pilot-key').inputValue();
   for(const plan of plans)for(const image of images){
    await page.locator('#pilot-plan').selectOption(plan.plan_id);await page.locator('#pilot-image').selectOption(image.image_id);
    await page.locator('[data-copy-target="pilot-json"]').click();
    const body=JSON.parse(await page.evaluate(()=>window.testClipboard));
    expect(body).toEqual({name:'prueba-configurador',plan_id:plan.plan_id,image_id:image.image_id,access:{root_password:password}});
    expect(await page.locator('#pilot-json').textContent()).not.toContain(password);
   }
   await page.locator('#pilot-password').fill('invalida');await expect(page.locator('#pilot-execute')).toBeDisabled();
   await page.locator('#pilot-password').fill(password);
   await page.locator('.pilot .guide-toc a[href="#piloto-curl"]').click();
   await page.locator('[data-copy-target="pilot-curl"]').click();
   const command=await page.evaluate(()=>window.testClipboard);
   // Ejecuta el Bash copiado con curl sustituido por una función local sin red.
   const actual=cp.execFileSync('bash',['-c','curl() { cat; }; export -f curl; '+command],{env:{...process.env,IACP_API_TOKEN:'fixture-sin-red'},encoding:'utf8'});
   expect(JSON.parse(actual).access.root_password).toBe(password);
   await page.locator('.pilot .guide-toc a[href="#piloto-json"]').click();
   await page.locator('#pilot-execute').click();await expect(page.locator('#pilot-live-status')).toContainText('HTTP 202');
   await page.locator('#pilot-execute').click();await expect.poll(()=>sent.length).toBe(2);
   expect(sent[0]).toEqual(sent[1]);expect(sent[0].key).toBe(key);
   const storage=await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));
   expect(storage).not.toContain(password);expect(storage).not.toContain('root_password');
   expect(page.url()).not.toContain(password);
   await page.locator('#pilot-refresh').click();await expect(page.locator('#pilot-live-result')).toContainText('192.0.2.10');
   await page.locator('[data-pilot-action="start"]').click();
   await expect(page.locator('#pilot-live-status')).toContainText('start: HTTP 202');
   await page.locator('#pilot-refresh').click();
   await expect(page.locator('[data-pilot-action="delete"]')).toBeEnabled();
   await page.locator('[data-pilot-action="delete"]').click();
   await expect(page.locator('#pilot-live-status')).toContainText('delete: HTTP 202');
   expect(sent[2].key).not.toBe(key);expect(sent[3].key).not.toBe(sent[2].key);
   await page.reload({waitUntil:'networkidle'});
   expect(await page.locator('#pilot-key').inputValue()).toBe(key);
   expect(await page.locator('#pilot-password').inputValue()).toBe('');
   await page.locator('#pilot-new-intent').click();
   expect(await page.locator('#pilot-key').inputValue()).not.toBe(key);
   await expect(page.locator('#pilot-execute')).toBeDisabled();
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
   expect(errors).toEqual([]);
   await page.screenshot({path:path.join(output,mode+'.png'),fullPage:true});
   results.push({modo:mode,estado:'PASS',combinaciones_generadas:16,solicitudes_simuladas:sent.length,secretos_persistidos:false,curl_bash_sin_red:'PASS',idempotencia:'PASS'});
  }finally{await browser.close();}
 }
 fs.writeFileSync(path.join(output,'resultado.json'),JSON.stringify({alcance:'Navegador y transporte simulado; no certifica aprovisionamiento físico',resultados:results},null,2)+'\n');
 console.log(JSON.stringify(results));
})().catch(error=>{console.error(error);process.exit(1);});
