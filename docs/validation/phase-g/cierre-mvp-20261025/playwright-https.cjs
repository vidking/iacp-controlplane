// Comprobación de lectura HTTPS; bloquea mutaciones y destinos externos.
const {chromium}=require(process.env.IACP_PLAYWRIGHT_MODULE || 'playwright');
const {execFileSync}=require('node:child_process');const fs=require('node:fs');
(async()=>{
 const py="import json,subprocess\nfrom pathlib import Path\nr=json.loads(subprocess.check_output(['docker','inspect','iacp-controlplane-api-1'],text=True))[0]\np=next(m['Source'] for m in r['Mounts'] if m['Destination']=='/run/secrets/api_token')\nprint(Path(p).read_text().strip())\n";
 const token=execFileSync('ssh',['-i','/home/despinoza/.ssh/iacp_work_ed25519','-o','IdentitiesOnly=yes','-o','BatchMode=yes','ubuntu@192.168.1.50','sudo python3 -'],{input:py,encoding:'utf8'}).trim();
 const browser=await chromium.launch({headless:true});const results=[];
 try{for(const mobile of [false,true]){
  const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1536,height:1000},extraHTTPHeaders:{Authorization:'Bearer '+token}});
  const errors=[],external=[],mutations=[],assets=[];
  await context.route('**/*',route=>{const r=route.request(),u=new URL(r.url());if(u.origin!=='https://iac.cyberfuel.com'){external.push(u.origin);return route.abort();}if(r.method()!=='GET'){mutations.push(r.method());return route.abort();}return route.continue();});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.name));page.on('response',r=>{if(/\.(js|css)(\?|$)/.test(r.url()))assets.push({path:new URL(r.url()).pathname+new URL(r.url()).search,status:r.status()});});
  const response=await page.goto('https://iac.cyberfuel.com/docs',{waitUntil:'networkidle'});if(response.status()!==200)throw Error('DOCS_HTTP');
  await page.locator('#operations-instances-listKVMPlans').waitFor({state:'visible'});
  if(mobile&&!await page.locator('#doc-navigation').isVisible())await page.locator('.menu-toggle').click();
  await page.locator('[data-doc-target="guia-crm"]').click();
  const guide=page.locator('#guia-crm .guide-content');await guide.waitFor({state:'visible'});
  const text=await guide.innerText();for(const word of ['kvm-1','kvm-2','root_password','Idempotency-Key'])if(!text.includes(word))throw Error('GUIDE_CONTENT');
  await page.screenshot({path:'docs/validation/phase-g/cierre-mvp-20261025/swagger-https-'+(mobile?'movil':'escritorio')+'.png'});
  if(errors.length||external.length||mutations.length||assets.some(a=>a.status!==200))throw Error('BROWSER_CHECKS');
  results.push({modo:mobile?'movil':'escritorio',https_real:true,autenticacion:'Bearer de automatización existente; no Basic de usuario documental',guia_visible:true,errores_js:errors,externos:external,mutaciones:mutations,assets});await context.close();
 }fs.writeFileSync('docs/validation/phase-g/cierre-mvp-20261025/playwright-https.json',JSON.stringify({estado:'PASS',fecha_local:new Intl.DateTimeFormat('sv-SE',{timeZone:'America/Costa_Rica',dateStyle:'short',timeStyle:'long'}).format(new Date()),resultados:results},null,2));console.log(JSON.stringify({estado:'PASS',vistas:results.length}));
 }finally{await browser.close();}
})().catch(e=>{console.log(JSON.stringify({estado:'FAIL',tipo:e.name}));process.exit(1)});
