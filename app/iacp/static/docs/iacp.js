/* Presentación de documentación; no conserva ni preautoriza credenciales. */
(() => {
  "use strict";
  const priority = ["listAuthorizedProfiles", "createInstance", "listInstances", "getInstance", "listOperations", "getOperation", "getInstancePower", "startInstancePower", "rebootInstancePower", "shutdownInstancePower", "deleteDisposableInstance"];
  const search = document.getElementById("endpoint-search");
  const menu = document.getElementById("doc-navigation");
  const toggle = document.querySelector(".menu-toggle");
  const slug = operationId => `[id$="-${CSS.escape(operationId)}"]`;
  let lastRevealKey = "";
  let scrollFrame = 0;
  let internalNavigation = false;
  let pendingRoute = false;
  function show(keys) {
    internalNavigation = true;
    try { window.ui.getSystem().layoutActions.show(keys, true); } finally { internalNavigation = false; }
  }
  function filter() {
    const term = search.value.trim().toLocaleLowerCase("es"); let total = 0, visible = 0;
    document.querySelectorAll("#swagger-ui .opblock").forEach(block => { total++; const matches = !term || block.textContent.toLocaleLowerCase("es").includes(term); block.hidden = !matches; if (matches) visible++; });
    document.querySelectorAll("#swagger-ui .opblock-tag-section").forEach(section => { section.hidden = !!term && !section.querySelector(".opblock:not([hidden])"); });
    document.getElementById("search-result").textContent = term ? `${visible} de ${total} endpoints coinciden con la búsqueda` : "";
  }
  function closeMenu() { menu?.classList.remove("open"); toggle?.setAttribute("aria-expanded", "false"); }
  function operationForHash() { const match = location.hash.match(/^#\/([^/]+)\/(.+)$/); return match && match[1] !== "tag" ? decodeURIComponent(match[2]) : null; }
  function tagForHash() {
    const match = location.hash.match(/^#\/(?:tag\/)?([^/]+)$/);
    const tag = match ? decodeURIComponent(match[1]) : null;
    return tag && document.querySelector(`.nav-link[data-tag-target="${CSS.escape(tag)}"]`) ? tag : null;
  }
  function documentForHash() { const value = location.hash.replace(/^#\/?/, ""); return ["guia-crm", "guia-operaciones", "guia-despliegue"].includes(value) ? value : null; }
  function anchorForHash() { const value = location.hash.replace(/^#\/?/, ""); return value && !operationForHash() && !documentForHash() ? document.getElementById(value) : null; }
  function markActive(operationId, tag) {
    document.querySelectorAll(".nav-link[data-doc-target]").forEach(link => link.classList.remove("active"));
    document.querySelectorAll(".nav-link[data-operation-id]").forEach(link => link.classList.toggle("active", !!operationId && link.dataset.operationId === operationId));
    document.querySelectorAll(".nav-link[data-tag-target]").forEach(link => link.classList.toggle("active", !!tag && link.dataset.tagTarget === tag));
  }
  function scrollToTarget(target) {
    if (!target) return;
    cancelAnimationFrame(scrollFrame);
    scrollFrame = requestAnimationFrame(() => {
      const offset = document.querySelector('.masthead').getBoundingClientRect().height + 20;
      // Dejar recorrido suficiente para alinear también el último grupo del contrato.
      document.documentElement.style.setProperty('--navigation-tail', `${Math.max(0, window.innerHeight - offset)}px`);
      window.scrollTo({top: Math.max(0, target.getBoundingClientRect().top + window.scrollY - offset), behavior: 'instant'});
    });
  }
  function revealOperation(operationId, force = false) {
    const tag = location.hash.match(/^#\/([^/]+)\//)?.[1];
    if (tag) { show(['operations-tag', decodeURIComponent(tag)]); show(['operations', decodeURIComponent(tag), operationId]); }
    if (search.value) { search.value = ''; filter(); }
    const target = document.querySelector(slug(operationId));
    if (!target) { pendingRoute = true; return false; }
    const group = target.closest('.opblock-tag-section');
    group.hidden = false;
    scrollToTarget(target);
    markActive(operationId, group.querySelector(':scope > .opblock-tag')?.dataset.tag);
    closeMenu(); return true;
  }
  function revealTag(tag, force = false) {
    const header = document.getElementById(`operations-tag-${tag}`);
    const section = header?.closest('.opblock-tag-section');
    if (!header || !section) return false;
    const key = `tag:${tag}`;
    if (!force && lastRevealKey === key) return true;
    lastRevealKey = key;
    if (search.value) { search.value = ''; filter(); }
    section.hidden = false;
    // API de estado de Swagger: abrir el grupo, no solo localizar su encabezado.
    const system = window.ui.getSystem();
    if (!system.layoutSelectors.isShown(['operations-tag', tag], true)) {
      show(['operations-tag', tag]);
    }
    requestAnimationFrame(() => scrollToTarget(document.getElementById(`operations-tag-${tag}`)));
    markActive(null, tag); closeMenu(); return true;
  }
  function revealDocument(documentId) {
    lastRevealKey = "";
    const target = document.getElementById(documentId); if (!target) return false;
    const panel = target.tagName === "DETAILS" ? target : target.querySelector(":scope > details");
    if (panel) panel.open = true;
    requestAnimationFrame(() => {
      const current = document.getElementById(documentId); if (!current) return;
      scrollToTarget(current);
    });
    markActive(null, null);
    document.querySelectorAll('.nav-link[data-doc-target]').forEach(link => link.classList.toggle('active', link.dataset.docTarget === documentId));
    closeMenu(); return true;
  }
  function revealAnchor(target) {
    if (!target) return false;
    lastRevealKey = '';
    for (let panel = target.closest('details'); panel; panel = panel.parentElement.closest('details')) panel.open = true;
    const documentRoot = target.closest('#guia-crm, #guia-operaciones, #guia-despliegue');
    markActive(null, null);
    const active = documentRoot?.id || (target.closest('.pilot') ? 'inicio' : target.id);
    document.querySelectorAll('.nav-link[data-doc-target]').forEach(link => link.classList.toggle('active', link.dataset.docTarget === active));
    scrollToTarget(target); closeMenu(); return true;
  }
  function navigate(link) {
    const hash = link.getAttribute('href');
    if (location.hash !== hash) history.pushState(null, '', hash);
    routeFromLocation();
  }
  function routeFromLocation() {
    pendingRoute = false;
    const id = operationForHash(), tag = tagForHash(), doc = documentForHash();
    if (doc) revealDocument(doc);
    else if (id) revealOperation(id, true);
    else if (tag) { if (!revealTag(tag, true)) pendingRoute = true; }
    else if (anchorForHash()) revealAnchor(anchorForHash());
  }
  // Único propietario del historial y scroll. Las acciones soportadas de Swagger
  // siguen abriendo grupos/operaciones; el plugin nativo no añade entradas extra.
  const navigationPlugin = () => ({statePlugins: {layout: {wrapActions: {
    show: original => (keys, shown) => {
      const result = original(keys, shown);
      if (!internalNavigation && shown && ['operations', 'operations-tag'].includes(keys[0])) {
        const hash = `#/${keys.slice(1).map(encodeURIComponent).join('/')}`;
        if (location.hash !== hash) history.pushState(null, '', hash);
        routeFromLocation();
      }
      return result;
    }
  }}}});
  window.ui = SwaggerUIBundle({plugins: [navigationPlugin], url: document.body.dataset.schema,
    dom_id: '#swagger-ui', deepLinking: false, docExpansion: 'list', displayRequestDuration: true,
    persistAuthorization: false, tryItOutEnabled: false, validatorUrl: null,
    defaultModelsExpandDepth: -1, presets: [SwaggerUIBundle.presets.apis], layout: 'BaseLayout',
    operationsSorter: (a, b) => {
      const rank = o => { const i = priority.indexOf(o.getIn(['operation', 'operationId']) || o.get('operationId')); return i < 0 ? 100 : i; };
      return rank(a) - rank(b) || a.get('path').localeCompare(b.get('path'));
    },
    onComplete: () => {
      const observer = new MutationObserver(() => { filter(); if (pendingRoute) routeFromLocation(); });
      observer.observe(document.getElementById('swagger-ui'), {childList: true, subtree: true});
      filter(); routeFromLocation();
    }
  });
  document.addEventListener('click', async event => {
    if (event.target.closest('[data-authorize]')) {
      event.preventDefault(); document.querySelector('.swagger-ui .btn.authorize')?.click(); return;
    }
    const button = event.target.closest('[data-copy-target]');
    if (!button) return;
    const text = document.getElementById(button.dataset.copyTarget).textContent;
    try {
      await navigator.clipboard.writeText(text);
      document.getElementById('copy-status').textContent = button.dataset.copyTarget === 'pilot-json' ? 'JSON copiado completo.' : 'cURL copiado completo.';
    } catch (_) {
      const selection = window.getSelection(), range = document.createRange();
      range.selectNodeContents(document.getElementById(button.dataset.copyTarget));
      selection.removeAllRanges(); selection.addRange(range);
      document.getElementById('copy-status').textContent = 'Seleccionado completo: usa Ctrl+C o el menú Copiar de tu navegador.';
    }
  });
  search.addEventListener("input", filter);
  document.addEventListener("click", event => { const link = event.target.closest("a[data-operation-id], a[data-tag-target], a[data-doc-target]"); if (link) { event.preventDefault(); event.stopImmediatePropagation(); navigate(link); } });
  document.addEventListener("click", event => { const link = event.target.closest(".guide-toc a[href^='#']"); if (!link) return; event.preventDefault(); const targetId = link.getAttribute("href").slice(1); const hash = `#${targetId}`; if (location.hash !== hash) history.pushState({anchor: targetId}, "", hash); revealAnchor(document.getElementById(targetId)); });
  // hashchange y popstate pueden llegar juntos: el coordinador cancela el RAF anterior.
  window.addEventListener('hashchange', routeFromLocation);
  window.addEventListener('popstate', routeFromLocation);
  toggle?.addEventListener("click", () => { const open = menu.classList.toggle("open"); toggle.setAttribute("aria-expanded", String(open)); });
  document.addEventListener("keydown", event => { if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) { event.preventDefault(); search.focus(); } });
})();
