/* Presentación de documentación; no conserva ni preautoriza credenciales. */
(() => {
  "use strict";
  const priority = ["listAuthorizedProfiles", "createInstance", "listInstances", "getInstance", "listOperations", "getOperation", "getInstancePower", "startInstancePower", "rebootInstancePower", "shutdownInstancePower", "deleteDisposableInstance"];
  const search = document.getElementById("endpoint-search");
  const menu = document.getElementById("doc-navigation");
  const toggle = document.querySelector(".menu-toggle");
  const slug = operationId => `[id$="-${CSS.escape(operationId)}"]`;
  function filter() {
    const term = search.value.trim().toLocaleLowerCase("es"); let total = 0, visible = 0;
    document.querySelectorAll("#swagger-ui .opblock").forEach(block => { total++; const matches = !term || block.textContent.toLocaleLowerCase("es").includes(term); block.hidden = !matches; if (matches) visible++; });
    document.querySelectorAll("#swagger-ui .opblock-tag-section").forEach(section => { section.hidden = !!term && !section.querySelector(".opblock:not([hidden])"); });
    document.getElementById("search-result").textContent = term ? `${visible} de ${total} endpoints coinciden con la búsqueda` : "";
  }
  function closeMenu() { menu?.classList.remove("open"); toggle?.setAttribute("aria-expanded", "false"); }
  function operationForHash() { const match = location.hash.match(/^#\/[^/]+\/(.+)$/); return match ? decodeURIComponent(match[1]) : null; }
  function documentForHash() { const value = location.hash.replace(/^#\/?/, ""); return ["guia-crm", "guia-operaciones", "guia-despliegue"].includes(value) ? value : null; }
  function markActive(operationId) { document.querySelectorAll(".nav-link[data-operation-id]").forEach(link => link.classList.toggle("active", link.dataset.operationId === operationId)); }
  function revealOperation(operationId) {
    const target = document.querySelector(slug(operationId)); if (!target) return false;
    const group = target.closest(".opblock-tag-section"); if (group) group.hidden = false;
    const summary = target.querySelector(".opblock-summary"); if (summary && !target.classList.contains("is-open")) summary.click();
    requestAnimationFrame(() => {
      const current = document.querySelector(slug(operationId));
      if (!current) return;
      const offset = (document.querySelector(".masthead")?.offsetHeight || 88) + 16;
      window.scrollTo({top: Math.max(0, current.getBoundingClientRect().top + window.scrollY - offset), behavior: "smooth"});
    });
    markActive(operationId); closeMenu(); return true;
  }
  function revealDocument(documentId) {
    const target = document.getElementById(documentId); if (!target) return false;
    const panel = target.tagName === "DETAILS" ? target : target.querySelector(":scope > details");
    if (panel) panel.open = true;
    const offset = (document.querySelector(".masthead")?.offsetHeight || 88) + 16;
    const heading = panel?.querySelector(".guide-content") || panel || target;
    window.scrollTo({top: Math.max(0, heading.getBoundingClientRect().top + window.scrollY - offset), behavior: "smooth"});
    closeMenu(); return true;
  }
  function navigate(link) {
    const operationId = link.dataset.operationId;
    if (operationId) { const hash = link.getAttribute("href"); if (location.hash !== hash) history.pushState({operationId}, "", hash); revealOperation(operationId); return; }
    const target = document.getElementById(link.dataset.docTarget); if (!target) return;
    const hash = `#${link.dataset.docTarget}`; if (location.hash !== hash) location.hash = hash; else revealDocument(link.dataset.docTarget);
  }
  window.ui = SwaggerUIBundle({url: document.body.dataset.schema, dom_id: "#swagger-ui", deepLinking: true, docExpansion: "list", displayRequestDuration: true, persistAuthorization: false, tryItOutEnabled: false, validatorUrl: null, defaultModelsExpandDepth: -1, presets: [SwaggerUIBundle.presets.apis], layout: "BaseLayout", operationsSorter: (a, b) => { const rank = o => { const i = priority.indexOf(o.getIn(["operation", "operationId"]) || o.get("operationId")); return i < 0 ? 100 : i; }; return rank(a) - rank(b) || a.get("path").localeCompare(b.get("path")); }, onComplete: () => { new MutationObserver(() => { filter(); const id = operationForHash(); if (id) revealOperation(id); const doc = documentForHash(); if (doc) revealDocument(doc); }).observe(document.getElementById("swagger-ui"), {childList: true, subtree: true}); filter(); const id = operationForHash(); if (id) revealOperation(id); const doc = documentForHash(); if (doc) revealDocument(doc); }});
  search.addEventListener("input", filter);
  document.addEventListener("click", event => { const link = event.target.closest(".nav-link[data-operation-id], .nav-link[data-doc-target]"); if (link) { event.preventDefault(); navigate(link); } });
  window.addEventListener("hashchange", () => { const id = operationForHash(); if (id) revealOperation(id); const doc = documentForHash(); if (doc) revealDocument(doc); }); window.addEventListener("popstate", () => { const id = operationForHash(); if (id) revealOperation(id); const doc = documentForHash(); if (doc) revealDocument(doc); });
  toggle?.addEventListener("click", () => { const open = menu.classList.toggle("open"); toggle.setAttribute("aria-expanded", String(open)); });
  document.addEventListener("keydown", event => { if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) { event.preventDefault(); search.focus(); } });
})();
