/* Configurador de la API existente. Secretos en memoria; sesión guarda solo metadatos. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('pilot-form');
  if (!form) return;
  const fields = {name:$('pilot-name'),plan:$('pilot-plan'),image:$('pilot-image'),password:$('pilot-password'),ssh:$('pilot-ssh')};
  const storageKey = 'iacp.pilot.intent.v1';
  let catalog = null, frozen = null, busy = false, saved = null, accepted = null;
  try { saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (_) { /* La API conserva la intención. */ }
  const newKey = () => 'swagger-' + crypto.randomUUID();
  if (saved && (!/^[A-Za-z0-9._:-]{1,128}$/.test(saved.key || '') || !saved.draft?.access || !Array.isArray(saved.draft.access.ssh_public_keys))) saved=null;
  let key = saved?.key || newKey();
  let tenant = saved?.tenant || null;
  let actionIntent = saved?.actionIntent || null, live = null;
  accepted = saved?.accepted || null;
  $('pilot-key').value = key;
  if (saved?.draft) { fields.name.value=saved.draft.name; fields.ssh.value=(saved.draft.access.ssh_public_keys || []).join('\n'); }
  const token = () => {
    const credential = Object.values(window.ui.authSelectors.authorized().toJS())
      .find(item => item.schema?.scheme?.toLowerCase() === 'bearer');
    if (!credential?.value) throw Object.assign(new Error('Pulsa Authorize e introduce el Bearer de tu tenant.'), {status:401});
    return credential.value;
  };
  const store = () => {
    if (!frozen && !saved) return;
    const body = frozen || saved.draft;
    const draft = {name:body.name,plan_id:body.plan_id,image_id:body.image_id,
      access:{ssh_public_keys:body.access.ssh_public_keys || []}};
    saved = {key,tenant,draft,accepted,actionIntent};
    try { sessionStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_) { /* No guardar secretos como alternativa. */ }
  };
  const friendly = error => {
    const messages={401:'Token inválido o ausente. Usa Authorize.',403:'Permiso insuficiente para esta operación.',
      409:'Conflicto de intención, estado, compatibilidad o capacidad. Conserva la misma clave al reintentar.',
      422:'La solicitud no cumple el contrato. Revisa los campos; prepara una nueva intención para corregir el cuerpo.',
      429:'Límite de solicitudes. Espera y reintenta con la misma clave.',503:'Servicio o admisión no disponible. Conserva intención y clave.'};
    let detail=error.message || 'No se recibió una respuesta; reintenta esta misma intención.';
    for (const secret of [fields.password.value,frozen?.access.root_password]) if (secret) detail=detail.split(secret).join('[secreto omitido]');
    return (messages[error.status] || 'No se pudo completar la consulta. ') + ' ' + detail.slice(0,400);
  };
  const api = async (path,method='GET',body=null,intentionKey=null,authorization=null) => {
    const headers={Authorization:'Bearer '+(authorization || token())};
    if (body!==null) headers['Content-Type']='application/json';
    if (intentionKey) headers['Idempotency-Key']=intentionKey;
    const response=await fetch(path,{method,headers,cache:'no-store',...(body!==null?{body:JSON.stringify(body)}:{})});
    const data=await response.json();
    if (!response.ok) throw Object.assign(new Error(typeof data.detail==='string'?data.detail:'Comprueba el esquema y los permisos.'),{status:response.status});
    return {status:response.status,data};
  };
  const schemaReady = () => {
    const schema=window.ui.specSelectors.specJson().toJS();
    const required=schema.components?.schemas?.CommercialInstanceCreate?.required || [];
    return ['name','plan_id','image_id','access'].every(field=>required.includes(field))
      && !required.includes('network') && !required.includes('commercial_context');
  };
  const draft = () => {
    if (!schemaReady()) throw new Error('Esta instalación no publica el contrato simplificado. Consulta la referencia API.');
    if (!catalog) throw new Error('Consulta primero el catálogo autorizado.');
    const plan=catalog.plans.find(p=>p.plan_id===fields.plan.value);
    const image=catalog.images.find(i=>i.image_id===fields.image.value);
    if (!plan?.availability.available || !image?.availability.available || !image.authorized
        || !image.compatible_plans.includes(plan.plan_id)) throw new Error('Selecciona un plan e imagen compatibles y disponibles.');
    const name=fields.name.value;
    if (!/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(name) || name.length>63) throw new Error('Nombre: 1–63 caracteres, minúsculas, números y guiones.');
    const password=fields.password.value;
    if (password.length<16 || password.length>128 || /[^\x20-\x7e]/.test(password)) throw new Error('Contraseña: 16–128 caracteres ASCII imprimibles.');
    const keys=fields.ssh.value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
    if (keys.length>16 || keys.some(v=>v.length>8192 || /[^\x20-\x7e]/.test(v) || !/^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+)\s+/.test(v))) throw new Error('Introduce únicamente llaves públicas SSH válidas, una por línea.');
    const body={name,plan_id:plan.plan_id,image_id:image.image_id,access:{root_password:password}};
    if (keys.length) body.access.ssh_public_keys=keys;
    if (saved?.draft && (body.name!==saved.draft.name || body.plan_id!==saved.draft.plan_id || body.image_id!==saved.draft.image_id)) throw new Error('Esta clave pertenece a la intención guardada. Usa Nueva intención para cambiar sus parámetros.');
    return body;
  };
  const masked = body => ({...body,access:{...body.access,root_password:'[oculta en la vista; Copiar JSON utiliza el valor real]'}});
  const curl = body => [
    '# Bash privado, sin historial ni trazas. La copia contiene la contraseña.',
    'set +o history', 'set +x', `IACP_KEY='${key}'`,
    'curl --fail-with-body --silent --show-error --config /dev/fd/3 \\',
    "  --request POST --url 'https://iac.cyberfuel.com/v1/instances' --data-binary @- \\",
    "  3<<IACP_HEADERS <<'IACP_BODY'",
    'header = "Authorization: Bearer ${IACP_API_TOKEN}"',
    'header = "Content-Type: application/json"',
    'header = "Idempotency-Key: ${IACP_KEY}"',
    'IACP_HEADERS', JSON.stringify(body,null,2), 'IACP_BODY'
  ].join('\n');
  const render = () => {
    let body=null;
    try { body=frozen || draft(); $('pilot-validation').textContent=frozen?'Intención enviada: reintentar conserva estos mismos datos.':'Solicitud válida para enviar; la API vuelve a comprobar permisos y capacidad.'; }
    catch (error) { $('pilot-validation').textContent=error.message; }
    $('pilot-json').textContent=body?JSON.stringify(masked(body),null,2):'Completa el formulario. No se generará una solicitud con marcadores de contraseña.';
    $('pilot-curl').textContent=body?curl(masked(body)):'Completa el formulario para generar el cURL.';
    for (const target of ['pilot-json','pilot-curl']) document.querySelector(`[data-copy-target="${target}"]`).disabled=!body;
    $('pilot-execute').disabled=busy || !body || !!actionIntent;
    $('pilot-execute').textContent=frozen || saved?'Reintentar la misma solicitud':'Crear máquina virtual';
    $('pilot-new-intent').disabled=busy;
    for (const field of [fields.name,fields.plan,fields.image,fields.ssh]) field.disabled=busy || !!frozen || !!saved;
    fields.password.disabled=busy || !!frozen;
    $('pilot-refresh').disabled=busy || !accepted?.instance_id;
    for (const button of document.querySelectorAll('[data-pilot-action]')) {
      const action=button.dataset.pilotAction;
      const pending=actionIntent?.action===action && !actionIntent?.blocked;
      const ready=live?.ready && !actionIntent && (action==='start'?live.power==='stopped':
        action==='delete'?live.power==='stopped':live.power==='running');
      button.disabled=busy || !accepted?.instance_id || !(pending || ready);
    }
  };
  const compatible = () => {
    const plan=fields.plan.value;
    for (const option of fields.image.options) {
      const image=catalog.images.find(i=>i.image_id===option.value);
      option.disabled=!image?.authorized || !image?.availability.available || !image.compatible_plans.includes(plan);
    }
    if (fields.image.selectedIndex<0 || fields.image.selectedOptions[0]?.disabled)
      fields.image.selectedIndex=Array.from(fields.image.options).findIndex(o=>!o.disabled);
  };
  $('load-pilot-catalog').addEventListener('click',async () => {
    try {
      const [plans,images]=await Promise.all([api('/v1/plans'),api('/v1/images')]);
      catalog={plans:plans.data,images:images.data};
      for (const [select,rows,id] of [[fields.plan,catalog.plans,'plan_id'],[fields.image,catalog.images,'image_id']]) {
        const previous=saved?.draft?.[id] || select.value;
        select.replaceChildren();
        for (const row of rows) {
          const option=document.createElement('option');option.value=row[id];
          const label=row.vcpus?`${row.name} · ${row.vcpus} vCPU · ${row.memory_mb} MiB · ${row.disk_gb} GiB`:`${row.operating_system} ${row.version} · ${row.image_id}`;
          option.textContent=label+(row.availability.available?' · disponible':' · '+row.availability.reasons.join(', '));
          option.disabled=!row.availability.available || (id==='image_id' && !row.authorized);select.append(option);
        }
        select.value=previous;
        if (select.selectedIndex<0) select.selectedIndex=Array.from(select.options).findIndex(o=>!o.disabled);
      }
      compatible();
      $('pilot-catalog-status').textContent='Catálogo real cargado. La disponibilidad no reserva capacidad ni certifica SSH.';
    } catch (error) { $('pilot-catalog-status').textContent=friendly(error); }
    render();
  });
  form.addEventListener('input',render);
  fields.plan.addEventListener('change',()=>{if(catalog)compatible();render();});
  fields.image.addEventListener('change',render);
  const refresh = async () => {
    if (!accepted?.instance_id) return;
    const [instance,operation]=await Promise.all([api('/v1/instances/'+accepted.instance_id),api('/v1/operations/'+accepted.operation_id)]);
    const value=instance.data;
    const status=operation.data.normalized_status || operation.data.status;
    const power=value.status==='ready'?(await api('/v1/instances/'+accepted.instance_id+'/power')).data:null;
    live={ready:value.status==='ready' && ['completed','failed','cancelled'].includes(status) && power?.mutations_available===true,power:power?.power_state};
    if(actionIntent?.operation_id){
      if(['completed','failed','cancelled'].includes(status)) actionIntent=null;
      else if(status==='uncertain') actionIntent.blocked=true;
      store();
    }
    $('pilot-power-status').textContent=value.status==='deleted'?'VM eliminada.':
      `Energía: ${power?.power_state || 'pendiente'}. ${actionIntent?'Conserva la acción pendiente y consulta su operación.':'Ejecuta una acción y actualiza hasta conocer su resultado.'}`;
    $('pilot-live-result').textContent=JSON.stringify({instance_id:value.instance_id || accepted.instance_id,status:value.status,
      operation_id:accepted.operation_id,operation_status:operation.data.normalized_status || operation.data.status,
      ip_addresses:value.ip_addresses || [],access_status:value.access?.status || null},null,2);
    $('pilot-live-status').textContent=value.status==='deleted'?'Eliminación registrada. Consulta la operación para confirmar resultado.':
      'Estado consultado. 202 o ready no prueban acceso SSH; verifica también red y bootstrap.';
    $('pilot-operation-links').replaceChildren();
    for (const [text,hash,op] of [['Abrir consulta de instancia','#/instances/getInstance','getInstance'],['Abrir seguimiento de operación','#/operations/getOperation','getOperation']]) {
      const link=document.createElement('a');link.textContent=text;link.href=hash;link.dataset.operationId=op;
      $('pilot-operation-links').append(link,document.createTextNode(' · '));
    }
  };
  form.addEventListener('submit',async event => {
    event.preventDefault(); if(busy)return;
    try {
      const body=frozen || draft(); const authorization=token(); busy=true;render();
      const identity=(await api('/v1/lab/usage','GET',null,null,authorization)).data.tenant_id;
      if(tenant && identity!==tenant) throw new Error('El token corresponde a otro tenant; conserva la identidad original de esta intención.');
      tenant=identity;frozen=JSON.parse(JSON.stringify(body));store();render();
      $('pilot-live-status').textContent='Enviando la intención. Ante un timeout se conserva la misma clave y cuerpo.';
      const result=await api('/v1/instances','POST',frozen,key,authorization);
      accepted={instance_id:result.data.instance_id,operation_id:result.data.operation_id,status:result.data.status};store();
      $('pilot-live-status').textContent=`HTTP ${result.status}: solicitud admitida; aún no significa VM lista ni SSH disponible.`;
      $('pilot-live-result').textContent=JSON.stringify({instance_id:accepted.instance_id,status:accepted.status,operation_id:accepted.operation_id},null,2);
    } catch (error) { $('pilot-live-status').textContent=friendly(error); }
    finally {busy=false;render();}
  });
  $('pilot-refresh').addEventListener('click',async ()=>{try {busy=true;render();await refresh();}catch(error){$('pilot-live-status').textContent=friendly(error);}finally{busy=false;render();}});
  for(const button of document.querySelectorAll('[data-pilot-action]')) button.addEventListener('click',async()=>{
    if(busy || !accepted?.instance_id)return;
    const action=button.dataset.pilotAction;
    if(actionIntent && (actionIntent.action!==action || actionIntent.blocked))return;
    try{
      const authorization=token();busy=true;
      actionIntent=actionIntent || {action,key:newKey()};store();render();
      const path='/v1/instances/'+accepted.instance_id+(action==='delete'?'':'/power/'+action);
      const response=await api(path,action==='delete'?'DELETE':'POST',{},actionIntent.key,authorization);
      accepted.operation_id=response.data.operation_id;actionIntent.operation_id=response.data.operation_id;store();live=null;
      $('pilot-live-status').textContent=`${action}: HTTP ${response.status}. Actualiza el estado hasta confirmar el resultado.`;
    }catch(error){$('pilot-live-status').textContent=friendly(error);}
    finally{busy=false;render();}
  });
  $('pilot-new-intent').addEventListener('click',()=>{
    if(busy)return;frozen=null;saved=null;tenant=null;accepted=null;actionIntent=null;live=null;key=newKey();$('pilot-key').value=key;
    fields.name.value='';fields.password.value='';
    $('pilot-live-result').textContent='';$('pilot-live-status').textContent='Completa una nueva intención. La VM anterior conserva sus operaciones.';
    try {sessionStorage.removeItem(storageKey);}catch(_){}
    $('copy-status').textContent='Nueva clave preparada. La intención anterior y su VM no se cancelan.';render();
  });
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-copy-target]');
    if(!button || !['pilot-json','pilot-curl'].includes(button.dataset.copyTarget))return;
    event.preventDefault();event.stopImmediatePropagation();
    try {
      const body=frozen || draft();const text=button.dataset.copyTarget==='pilot-json'?JSON.stringify(body,null,2):curl(body);
      await navigator.clipboard.writeText(text);
      $('copy-status').textContent=(button.dataset.copyTarget==='pilot-json'?'JSON':'cURL')+' copiado completo con los valores introducidos. Contiene la contraseña: no compartir.';
    } catch(error){$('copy-status').textContent='No se pudo copiar. Comprueba el formulario y los permisos del portapapeles.';}
  },true);
  render();
  if(accepted?.instance_id){$('pilot-live-result').textContent=JSON.stringify(accepted,null,2);$('pilot-live-status').textContent='Intención recuperada de esta sesión. Actualiza estado; para reintentar introduce la misma contraseña.';render();}
})();
