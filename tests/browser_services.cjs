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
    let created = false, retrieved = false, delivered = false, credentialPosts = 0;
    let payload, deliveryKey;
    const service = () => ({ service_id: 'service-test', instance_id: 'instance-test', name: 'prueba-service-n8n', application_id: 'n8n', status: delivered ? 'delivered' : 'awaiting_customer', infrastructure_status: 'ready', credentials_available: !retrieved, access_url: 'https://192.0.2.10/', configuration: {access_mode: 'ip'}, jobs: [{status:'succeeded',phase:'completed'}] });
    const instance = {instance_id:'instance-test',name:'prueba-service-n8n',status:'ready',vmid:40000,image_id:'ubuntu-2404',spec:{}};
    await page.route('**/v1/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      let body = {};
      if (pathname === '/v1/plans') body = {plans:[{plan_id:'kvm-2',vcpus:2,memory_mb:4096,disk_gb:100}]};
      else if (pathname === '/v1/images') body = {images:['ubuntu-2404','ubuntu-2604','almalinux-9','almalinux-10'].map((image_id,index) => ({image_id,template_vm_id:29000+index}))};
      else if (pathname === '/v1/network-profiles') body = {network_profiles:[{profile_id:'lab_dhcp',enabled:true}],active_profile:'lab_dhcp'};
      else if (pathname === '/v1/applications') body = ['nextcloud','n8n'].map(application_id => ({application_id,label:application_id,enabled:true,version:'test',supported_image_ids:['ubuntu-2404','almalinux-9']}));
      else if (pathname === '/v1/service-instances' && request.method() === 'POST') { created = true; payload = request.postDataJSON(); body = {operation_id:'operation-test',instance_id:'instance-test',service_id:'service-test',job_id:'job-test',status:'waiting_vm'}; }
      else if (pathname === '/v1/service-instances') body = {services:created ? [service()] : [],total:created ? 1 : 0};
      else if (pathname === '/v1/service-instances/service-test') body = service();
      else if (pathname.endsWith('/credentials')) { assert.equal(request.method(),'POST'); retrieved=true; credentialPosts+=1; body={onboarding_username:'installer',onboarding_password:'clave-ficticia'}; }
      else if (pathname.endsWith('/deliver')) { deliveryKey=request.headers()['idempotency-key']; delivered=true; body={job_id:'delivery-test'}; }
      else if (pathname === '/v1/operations/operation-test') body = {operation_id:'operation-test',instance_id:'instance-test',status:'completed'};
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
    assert.equal(await page.locator('#image option').count(),4);
    await page.locator('#application').selectOption('n8n');
    assert.equal(await page.locator('#image option').count(),2);
    assert.equal(await page.locator('#ssh-key').getAttribute('required'),'');
    assert.equal(await page.locator('#started').isChecked(),true);
    assert.equal(await page.locator('#started').isDisabled(),true);
    await page.locator('#name').fill('prueba-service-n8n');
    await page.locator('#root-password').fill('password-ficticio-123');
    await page.locator('#ssh-key').fill('ssh-ed25519 AAAAexample');
    await page.locator('#ssh-key').dispatchEvent('input');
    assert.equal((await page.locator('#json-preview').textContent()).includes('password-ficticio'),false);
    await page.locator('#create-form').evaluate(form => form.requestSubmit());
    await page.waitForFunction(() => document.querySelector('#create-button').disabled === false);
    assert.equal(payload.application_id,'n8n'); assert.equal(payload.instance.image_id,'ubuntu-2404');
    assert.deepEqual(payload.configuration,{access_mode:'ip'});
    assert.equal(payload.instance.start_on_create,true);
    assert.match(await page.locator('#curl-preview').textContent(),/\/v1\/service-instances/);
    assert.equal(await page.locator('#service-url').getAttribute('href'),'https://192.0.2.10/');
    assert.equal(credentialPosts,0);
    await page.locator('#service-credentials').click();
    await page.waitForFunction(() => document.querySelector('#service-credentials-output').textContent.includes('clave-ficticia'));
    assert.equal(credentialPosts,1);
    await page.locator('#clear-service-credentials').click();
    assert.equal((await page.locator('#service-credentials-output').textContent()).includes('clave-ficticia'),false);
    await page.locator('#deliver-service').click();
    await page.waitForFunction(() => document.querySelector('#service-status').textContent.includes('delivered'));
    assert.ok(deliveryKey); assert.equal(credentialPosts,1);
    await page.locator('#application').selectOption('kvm');
    assert.equal(await page.locator('#image option').count(),4);
    assert.deepEqual(errors,[]);
    console.log('PASS: catálogo KVM/servicios, contrato n8n, redacción, consulta explícita única de credenciales y entrega. API simulada; no certificación física.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; }).finally(() => server.close());
