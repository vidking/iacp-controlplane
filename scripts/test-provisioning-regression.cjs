/* Regresión mínima de autenticación y selección; no crea ni muta infraestructura. */
const {chromium}=require('playwright');
const {expect}=require('playwright/test');

const base=process.env.IACP_DOCS_BASE||'http://127.0.0.1:8765';
const token='fixture-bearer-sin-red';
const ids={ready:'11111111-1111-4111-8111-111111111111',failed:'22222222-2222-4222-8222-222222222222'};

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.IACP_PLAYWRIGHT_EXECUTABLE||undefined});
  try{
    const context=await browser.newContext({httpCredentials:{username:'iacp',password:'identidad-sintetica-solo-pruebas-documentales'}});
    const requests=[];let instances=[];let nextStatus=null;
    await context.route('**/v1/**',async route=>{
      const request=route.request(),url=new URL(request.url());
      requests.push({path:url.pathname,authorization:request.headers().authorization});
      if(nextStatus){const status=nextStatus;nextStatus=null;return route.fulfill({status,json:{detail:'fixture'}});}
      if(url.pathname==='/v1/instances'&&request.method()==='GET')return route.fulfill({status:200,json:instances});
      if(url.pathname==='/v1/plans')return route.fulfill({status:200,json:[{plan_id:'kvm-1',name:'KVM 1',vcpus:1,memory_mb:2048,disk_gb:50,availability:{available:true,reasons:[]}}]});
      if(url.pathname==='/v1/images')return route.fulfill({status:200,json:[{image_id:'ubuntu-2404',operating_system:'Ubuntu',version:'24.04',authorized:true,compatible_plans:['kvm-1'],availability:{available:true,reasons:[]}}]});
      if(url.pathname===`/v1/instances/${ids.failed}`)return route.fulfill({status:200,json:{instance_id:ids.failed,name:'fallida-sin-vmid',image:'ubuntu-2404',vcpus:1,memory_mb:2048,disk_gb:50,status:'failed',vmid:null,node:null,ip_addresses:[],result:{error_code:'PLAN_FAILED'}}});
      if(url.pathname===`/v1/instances/${ids.ready}`)return route.fulfill({status:200,json:{instance_id:ids.ready,name:'lista-mng',image:'ubuntu-2404',vcpus:1,memory_mb:2048,disk_gb:50,status:'ready',vmid:30071,node:'mng01',ip_addresses:[],result:null}});
      if(url.pathname==='/v1/operations')return route.fulfill({status:200,json:[{operation_id:url.searchParams.get('instance_id')===ids.failed?'op-failed':'op-ready',instance_id:url.searchParams.get('instance_id'),status:'completed',normalized_status:'completed',created_at:'2026-10-27T20:00:00Z',events:[],error_code:url.searchParams.get('instance_id')===ids.failed?'PLAN_FAILED':null}]});
      if(url.pathname===`/v1/instances/${ids.ready}/power`)return route.fulfill({status:200,json:{power_state:'stopped',mutations_available:true}});
      return route.fulfill({status:200,json:{}});
    });
    const page=await context.newPage();
    await page.goto(base+'/docs',{waitUntil:'networkidle'});
    await page.locator('#pilot-load-instances').click();
    await expect(page.locator('#pilot-instance-status')).toContainText('No hay un Bearer autorizado');

    await page.evaluate(value=>window.ui.preauthorizeApiKey('HTTPBearer',value),'Bearer '+token);
    instances=[
      {instance_id:ids.ready,name:'lista-mng',status:'ready',vmid:30071},
      {instance_id:ids.failed,name:'fallida-sin-vmid',status:'failed',vmid:null},
    ];
    await page.locator('#pilot-load-instances').click();
    await expect(page.locator('#pilot-instance-select')).toHaveValue('');
    await expect(page.locator('#pilot-instance-select option').first()).toHaveText('Seleccionar instancia');
    await page.locator('#pilot-instance-select').selectOption(ids.ready);
    await expect(page.locator('#pilot-live-result')).toContainText('30071');
    await expect(page.locator('[data-pilot-action="start"]')).toBeEnabled();
    await page.locator('#pilot-instance-select').selectOption(ids.failed);
    await expect(page.locator('#pilot-power-status')).toContainText('failed y sin VMID');
    await expect(page.locator('[data-pilot-action="start"]')).toBeDisabled();

    nextStatus=401;await page.locator('#pilot-load-instances').click();
    await expect(page.locator('#pilot-instance-status')).toContainText('Bearer autorizado fue rechazado por la API (401)');
    nextStatus=403;await page.locator('#pilot-load-instances').click();
    await expect(page.locator('#pilot-instance-status')).toContainText('Bearer válido, pero sin permiso');
    expect(requests.filter(row=>row.path==='/v1/instances').some(row=>row.authorization==='Bearer '+token)).toBe(true);
    const storage=await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));
    expect(storage).not.toContain(token);
    console.log(JSON.stringify({estado:'PASS',placeholder:'PASS',seleccion:'PASS',auth:'PASS',secretos_persistidos:false}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
