/* Certificación de la candidata local con Playwright ya instalado; solo lectura. */
const {chromium, firefox} = require('playwright');
const {expect: baseExpect} = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const expect = baseExpect.configure({timeout:15000});

async function main() {
  const output = process.env.IACP_DOCS_EVIDENCE_DIR;
  if (!output) throw new Error('Falta IACP_DOCS_EVIDENCE_DIR');
  fs.mkdirSync(output, {recursive: true});
  const results = [];
  for (const mode of ['chromium-escritorio', 'chromium-movil', 'firefox-escritorio']) {
    console.log('Comprobando '+mode);
    const mobile = mode.includes('movil');
    const engine = mode.startsWith('firefox') ? firefox : chromium;
    const browser = await engine.launch({headless:true, ...(engine === chromium && process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
    try {
      const context = await browser.newContext({viewport: mobile ? {width:390,height:844} : {width:1536,height:1000},
        httpCredentials:{username:'iacp',password:'identidad-sintetica-solo-pruebas-documentales'}});
      const page = await context.newPage();
      const errors = [], external = [], mutations = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
      page.on('request', r => {
        if (!r.url().startsWith('http://127.0.0.1:8765/')) external.push(r.url());
        if (!['GET','HEAD','OPTIONS'].includes(r.method())) mutations.push(r.method());
      });
      await page.goto('http://127.0.0.1:8765/docs', {waitUntil:'networkidle'});
      await expect(page.locator('#operations-instances-listKVMPlans')).toBeVisible();
      await expect(page.locator('#operations-instances-listGoldenImages')).toBeVisible();
      const selectGuide = async id => {
        if (mobile && !(await page.locator('#doc-navigation').isVisible())) await page.locator('.menu-toggle').click();
        await page.locator(`[data-doc-target="${id}"]`).click();
        const root = page.locator(`#${id}`);
        const details = id === 'guia-crm' ? root.locator(':scope > details') : root;
        await expect(details).toHaveAttribute('open','');
        await expect.poll(async () => root.evaluate(e => {
          const r=e.getBoundingClientRect(),h=document.querySelector('.masthead').getBoundingClientRect();
          return r.top >= h.bottom+15 && r.top < h.bottom+40;
        })).toBe(true);
      };
      await selectGuide('guia-crm');
      await expect(page.locator('#crm-catalogo-g')).toBeInViewport();
      const text = await page.locator('#guia-crm .guide-content').innerText();
      for (const word of ['KVMHOST-54','kvm-1','kvm-4','creación todavía bloqueada','root_password','capacity_verified']) expect(text).toContain(word);
      const code = page.locator('#guia-crm pre').filter({hasText:'"plan_id": "kvm-2"'});
      await code.scrollIntoViewIfNeeded();
      await expect(code).toBeInViewport();
      await expect(code.locator('code')).toHaveCSS('background-color','rgba(0, 0, 0, 0)');
      await expect(code.locator('code')).toHaveCSS('display','block');
      const style = await code.locator('code').evaluate(e => {
        const s=getComputedStyle(e);return {background:s.backgroundColor,color:s.color,display:s.display};
      });
      expect(style).toEqual({background:'rgba(0, 0, 0, 0)',color:'rgb(231, 242, 245)',display:'block'});
      await page.screenshot({path:path.join(output,mode+'.png')});
      await page.locator('[data-operation-id="listKVMPlans"]').click();
      await expect(page).toHaveURL(/#\/instances\/listKVMPlans$/);
      await expect(page.locator('#operations-instances-listKVMPlans')).toHaveClass(/is-open/);
      await page.goBack();
      await expect(page).toHaveURL(/#guia-crm$/);
      await selectGuide('guia-operaciones');
      await selectGuide('guia-despliegue');
      await page.reload({waitUntil:'networkidle'});
      await expect(page.locator('#guia-despliegue')).toHaveAttribute('open','');
      const contract = await context.request.get('http://127.0.0.1:8765/openapi.json');
      expect(contract.status()).toBe(200);
      expect((await contract.json()).openapi).toBe('3.1.0');
      expect(errors).toEqual([]);expect(external).toEqual([]);expect(mutations).toEqual([]);
      results.push({mode,estado:'PASS',errors,external,mutations,style});
    } finally { await browser.close(); }
  }
  const report={fecha:new Date().toLocaleString('es-CR',{timeZone:'America/Costa_Rica'}),
    zona:'America/Costa_Rica',alcance:'Candidata local descartable; no certifica VM 104',results};
  fs.writeFileSync(path.join(output,'playwright.json'), JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
}
main().catch(error => { console.error(error.message); process.exitCode=1; });
