/* Presentación de documentación; no conserva ni preautoriza credenciales. */
(() => {
  "use strict";
  const priority = ["listAuthorizedProfiles", "createInstance", "listInstances", "getInstance", "listOperations", "getOperation", "getInstancePower", "startInstancePower", "rebootInstancePower", "shutdownInstancePower", "deleteDisposableInstance"];
  const search = document.getElementById("endpoint-search");
  function filter() {
    const term = search.value.trim().toLocaleLowerCase("es");
    let total = 0, visible = 0;
    document.querySelectorAll("#swagger-ui .opblock").forEach(block => {
      total++;
      const matches = !term || block.textContent.toLocaleLowerCase("es").includes(term);
      block.hidden = !matches;
      if (matches) visible++;
    });
    document.querySelectorAll("#swagger-ui .opblock-tag-section").forEach(section => {
      section.hidden = !!term && !section.querySelector(".opblock:not([hidden])");
    });
    document.getElementById("search-result").textContent = term ? `${visible} de ${total} endpoints coinciden con la búsqueda` : "";
  }
  window.ui = SwaggerUIBundle({
    url: document.body.dataset.schema,
    dom_id: "#swagger-ui",
    deepLinking: true,
    docExpansion: "list",
    displayRequestDuration: true,
    persistAuthorization: false,
    tryItOutEnabled: false,
    validatorUrl: null,
    defaultModelsExpandDepth: -1,
    presets: [SwaggerUIBundle.presets.apis],
    layout: "BaseLayout",
    operationsSorter: (a, b) => {
      const rank = o => { const i = priority.indexOf(o.getIn(["operation", "operationId"]) || o.get("operationId")); return i < 0 ? 100 : i; };
      return rank(a) - rank(b) || a.get("path").localeCompare(b.get("path"));
    },
    onComplete: () => {
      new MutationObserver(filter).observe(document.getElementById("swagger-ui"), {childList: true, subtree: true});
      filter();
    }
  });
  search.addEventListener("input", filter);
  document.addEventListener("keydown", event => {
    if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault(); search.focus();
    }
  });
})();
