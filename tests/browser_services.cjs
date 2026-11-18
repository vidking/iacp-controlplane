// Prueba del formulario real con API simulada y credenciales exclusivamente ficticias.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve('api/static');
const server = http.createServer((request, response) => {
  let name = new URL(request.url, 'http://localhost').pathname;
  if (name === '/docs' || name === '/docs/') name = '/docs/index.html';
  const file = path.resolve(root, '.' + name);
  if (!file.startsWith(root + path.sep)) { response.writeHead(403).end(); return; }
  fs.readFile(file, (error, data) => {
    if (error) { response.writeHead(404).end(); return; }
    response.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    response.end(data);
  });
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: process.env.IACP_CHROMIUM || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage();
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    let created = false, retrieved = false, delivered = false, deleted = false, powerState = 'running', credentialPosts = 0, deletePosts = 0;
    let payload, deliveryKey;
    const service = () => ({ service_id: 'service-test', instance_id: 'instance-test', name: 'prueba-service-n8n', application_id: 'n8n', status: deleted ? 'deleted' : delivered ? 'delivered' : 'awaiting_customer', infrastructure_status: deleted ? 'deleted' : 'ready', power_state: powerState, vmid: 40000, credentials_available: !retrieved, access_url: 'https://192.0.2.10/', configuration: {access_mode: 'ip'}, jobs: [{status:'succeeded',phase:'completed'}] });
    const instance = {instance_id:'instance-test',name:'prueba-service-n8n',status:'ready',vmid:40000,image_id:'ubuntu-2404',spec:{}};
    page.on('dialog', dialog => dialog.accept());
    await page.route('**/v1/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      let body = {};
      if (pathname === '/v1/plans') body = {plans:[{plan_id:'kvm-2',vcpus:2,memory_mb:4096,disk_gb:100}]};
      else if (pathname === '/v1/images') body = {images:['ubuntu-2404','ubuntu-2604','almalinux-9','almalinux-10'].map((image_id,index) => ({image_id,template_vm_id:29000+index}))};
      else if (pathname === '/v1/network-profiles') body = {network_profiles:[{profile_id:'lab_dhcp',enabled:true}],active_profile:'lab_dhcp'};
      else if (pathname === '/v1/applications') body = ['nextcloud','n8n'].map(application_id => ({application_id,label:application_id,enabled:true,version:'test',supported_image_ids:['ubuntu-2404','almalinux-9']}));
      else if (pathname === '/v1/service-instances' && request.method() === 'POST') { created = true; payload = request.postDataJSON(); body = {operation_id:'operation-test',instance_id:'instance-test',service_id:'service-test',job_id:'job-test',status:'waiting_vm'}; }
      else if (pathname === '/v1/service-instances/service-test' && request.method() === 'DELETE') { deleted=true; deletePosts+=1; body={operation_id:'operation-delete-test'}; }
      else if (pathname === '/v1/service-instances') body = {services:created && !deleted ? [service()] : [],total:created && !deleted ? 1 : 0};
      else if (pathname === '/v1/service-instances/service-test') body = service();
      else if (pathname.endsWith('/credentials')) { assert.equal(request.method(),'POST'); retrieved=true; credentialPosts+=1; body={owner_setup_required:true,access_instructions:'Crea la cuenta propietaria de n8n en el primer acceso.'}; }
      else if (pathname.endsWith('/deliver')) { deliveryKey=request.headers()['idempotency-key']; delivered=true; body={job_id:'delivery-test'}; }
      else if (pathname.startsWith('/v1/instances/instance-test/power/')) { const action=pathname.split('/').at(-1); powerState=action==='shutdown'?'stopped':'running'; body={operation_id:`operation-${action}-test`}; }
      else if (pathname.startsWith('/v1/operations/')) body = {operation_id:pathname.split('/').at(-1),instance_id:'instance-test',status:'completed'};
      else if (pathname === '/v1/instances/instance-test') body = instance;
      else if (pathname === '/v1/instances') body = {instances:created ? [instance] : [],total:created ? 1 : 0};
      else if (pathname === '/v1/observability') body = {workers:[],performance:{}};
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/docs#create`);
    await page.locator('#step-access').evaluate(element => element.open=true);
    await page.locator('#api-token').fill('token-ficticio');
    await page.locator('#auth-form').evaluate(form => form.requestSubmit());
    await page.waitForFunction(() => document.querySelector('#auth-message').textContent.includes('API autorizada'));
    assert.equal(await page.locator('#services-panel').evaluate(element => element.open),false);
    await page.locator('#services-panel > summary').click();
    assert.equal(await page.locator('#services-panel').evaluate(element => element.open),true);
    assert.equal(await page.locator('#image option').count(),4);
    assert.equal(await page.locator('[data-application-id="nextcloud"]').isEnabled(),true);
    assert.equal(await page.locator('[data-application-id="n8n"]').isEnabled(),true);
    await page.locator('[data-application-id="nextcloud"]').click();
    assert.equal(await page.locator('[data-application-id="nextcloud"]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#ssh-key').getAttribute('required'),null);
    assert.equal(await page.locator('#ssh-key').evaluate(field => field.required),false);
    assert.match(await page.locator('#root-password-label').textContent(),/contraseña root.*obligatoria/i);
    assert.match(await page.locator('#ssh-key-help').textContent(),/ya definiste contraseña root.*no necesitas una llave SSH/i);
    await page.locator('[data-application-id="n8n"]').click();
    assert.equal(await page.locator('[data-application-id="n8n"]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#image option').count(),2);
    assert.equal(await page.locator('#ssh-key').getAttribute('required'),null);
    assert.equal(await page.locator('#ssh-key').evaluate(field => field.required),false);
    assert.match(await page.locator('#root-password-label').textContent(),/contraseña root.*obligatoria/i);
    assert.match(await page.locator('#ssh-key-help').textContent(),/ya definiste contraseña root.*no necesitas una llave SSH/i);
    assert.equal(await page.locator('#started').isChecked(),true);
    assert.equal(await page.locator('#started').isDisabled(),true);
    await page.locator('#name').fill('prueba-service-n8n');
    await page.locator('#root-password').fill('password-ficticio-123');
    assert.equal((await page.locator('#json-preview').textContent()).includes('password-ficticio'),false);
    await page.locator('#create-form').evaluate(form => form.requestSubmit());
    await page.waitForFunction(() => document.querySelector('#create-button').disabled === false);
    assert.equal(payload.application_id,'n8n'); assert.equal(payload.instance.image_id,'ubuntu-2404');
    assert.deepEqual(payload.configuration,{access_mode:'ip'});
    assert.deepEqual(payload.instance.access.ssh_public_keys,[]);
    assert.equal(payload.instance.access.root_password,'password-ficticio-123');
    assert.equal(payload.instance.start_on_create,true);
    assert.match(await page.locator('#curl-preview').textContent(),/\/v1\/service-instances/);
    assert.equal(await page.locator('#service-url').getAttribute('href'),'https://192.0.2.10/');
    await page.locator('[data-service-select="service-test"]').click();
    assert.equal(await page.locator('#service-detail').isVisible(),true);
    assert.match(await page.locator('#service-credentials').textContent(),/instrucciones de acceso/);
    assert.equal(await page.locator('#service-reboot').isEnabled(),true);
    assert.equal(await page.locator('#service-power-shutdown').isEnabled(),true);
    assert.equal(await page.locator('#delete-service').isEnabled(),false);
    assert.equal(credentialPosts,0);
    await page.locator('#service-credentials').click();
    await page.waitForFunction(() => document.querySelector('#service-credentials-output').textContent.includes('cuenta propietaria'));
    assert.equal(credentialPosts,1);
    await page.locator('#clear-service-credentials').click();
    assert.equal((await page.locator('#service-credentials-output').textContent()).includes('cuenta propietaria'),false);
    await page.locator('#deliver-service').click();
    await page.waitForFunction(() => document.querySelector('#service-status').textContent.includes('delivered'));
    assert.ok(deliveryKey); assert.equal(credentialPosts,1);
    await page.locator('#service-power-shutdown').click();
    await page.waitForFunction(() => document.querySelector('#service-power-start').disabled === false);
    assert.equal(await page.locator('#delete-service').isEnabled(),true);
    await page.locator('#service-power-start').click();
    await page.waitForFunction(() => document.querySelector('#service-power-shutdown').disabled === false);
    assert.equal(await page.locator('#delete-service').isEnabled(),false);
    await page.locator('#service-power-shutdown').click();
    await page.waitForFunction(() => document.querySelector('#delete-service').disabled === false);
    await page.locator('#delete-service').click();
    await page.waitForFunction(() => document.querySelector('#service-detail').classList.contains('hidden'));
    assert.equal(deletePosts,1);
    await page.waitForFunction(() => document.querySelector('[data-service-row="service-test"]') === null);
    assert.equal(await page.locator('[data-service-row="service-test"]').count(),0);
    await page.locator('#services-panel > summary').click();
    assert.equal(await page.locator('#services-panel').evaluate(element => element.open),false);
    await page.locator('[data-application-id="kvm"]').click();
    assert.equal(await page.locator('#image option').count(),4);
    assert.deepEqual(errors,[]);
    console.log('PASS: catálogo KVM/servicios, sección plegable, administración, contrato n8n, redacción y entrega. API simulada; no certificación física.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; }).finally(() => server.close());
