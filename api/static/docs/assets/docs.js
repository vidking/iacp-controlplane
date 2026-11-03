(() => {
  const state = {
    token: "",
    route: "create",
    guide: null,
    plans: [],
    images: [],
    networkProfiles: [],
    instances: [],
    operations: [],
    selectedInstanceId: null,
    selectedOperationId: null,
    lastJson: "",
    lastCurl: "",
    liveTimer: null,
    liveRequest: false,
    liveActive: false,
    pagination: {
      instances: { limit: 20, offset: 0, total: 0 },
      operations: { limit: 20, offset: 0, total: 0 },
      events: { limit: 20, offset: 0, total: 0 },
    },
  };

  const $ = (id) => document.getElementById(id);
  const terminalStatuses = ["completed", "failed", "uncertain"];

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[character]));
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
  }

  const eventLabels = {
    "operation.created": "Solicitud creada",
    "operation.started": "Worker inició",
    "proxmox.pool.validated": "Pool de recursos validado",
    "resource.pool.validated": "Pool de recursos validado",
    "golden.observed": "Imagen base validada",
    "image.validated": "Imagen base validada",
    "vmid.reserved": "VMID reservado",
    "terraform.plan.started": "Preparando la configuración",
    "terraform.plan.completed": "Configuración validada",
    "terraform.apply.started": "Creando la KVM",
    "terraform.apply.completed": "KVM creada",
    "terraform.provision.started": "Aprovisionando la instancia",
    "terraform.provision.completed": "Instancia aprovisionada",
    "terraform.destroy.started": "Eliminando la KVM",
    "terraform.destroy.completed": "KVM eliminada",
    "terraform.apply.failed": "Aprovisionamiento fallido",
    "terraform.provision.failed": "Aprovisionamiento fallido",
    "terraform.destroy.failed": "Eliminación fallida",
    "provision.plan.started": "Preparando la configuración",
    "provision.plan.completed": "Configuración validada",
    "instance.create.started": "Creando la KVM",
    "instance.create.completed": "KVM creada",
    "instance.delete.started": "Eliminando la KVM",
    "instance.delete.completed": "KVM eliminada",
    "instance.operation.failed": "Operación fallida",
    "power.start.started": "Encendiendo",
    "power.start.completed": "Encendido completado",
    "power.shutdown.started": "Apagando",
    "power.shutdown.completed": "Apagado completado",
    "power.reboot.started": "Reiniciando",
    "power.reboot.completed": "Reinicio completado",
  };

  function eventLabel(kind, status) {
    if (eventLabels[kind]) return eventLabels[kind];
    if (kind?.endsWith(".failed") || status === "failed") return "Ejecución fallida";
    if (status === "uncertain") return "Requiere reconciliación";
    if (status === "succeeded" || status === "completed") return "Etapa completada";
    if (status === "running") return "En ejecución";
    if (status === "pending" || status === "queued") return "En cola";
    return kind || "Actividad técnica";
  }

  function operationLabel(operation) {
    const event = operation?.latest_event;
    if (event?.kind) return eventLabel(event.kind, event.status);
    const action = operation?.payload?.power_action || operation?.action;
    if (operation?.command === "power") {
      return { start: "Encendiendo", shutdown: "Apagando", reboot: "Reiniciando" }[action] || "Ejecutando lifecycle";
    }
    return {
      provision: "Aprovisionando",
      apply: "Aplicando cambios",
      plan: "Planificando",
      destroy: "Eliminando",
    }[operation?.command] || "Ejecutando operación";
  }

  function durationLabel(operationOrStartedAt, finishedAt = null) {
    const operation = typeof operationOrStartedAt === "object" ? operationOrStartedAt : null;
    const startedAt = operation?.started_at || operation?.created_at || operationOrStartedAt;
    const endAt = operation?.finished_at || finishedAt || (operation && terminalStatuses.includes(operation.status) ? operation.finished_at : null) || new Date().toISOString();
    if (!startedAt) return "duración no disponible";
    const elapsed = Math.max(0, Math.floor((new Date(endAt).valueOf() - new Date(startedAt).valueOf()) / 1000));
    if (!Number.isFinite(elapsed)) return "duración no disponible";
    if (operation && terminalStatuses.includes(operation.status)) {
      if (elapsed < 60) return `${elapsed}s de duración`;
      return `${Math.floor(elapsed / 60)}m ${elapsed % 60}s de duración`;
    }
    if (elapsed < 60) return `${elapsed}s en ejecución`;
    return `${Math.floor(elapsed / 60)}m ${elapsed % 60}s en ejecución`;
  }

  function metricDuration(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
    const value = Number(seconds);
    if (value < 60) return `${value.toFixed(1)}s`;
    return `${(value / 60).toFixed(1)}m`;
  }

  function statusClass(status) {
    if (status === "succeeded") return "success";
    if (["completed", "ok", "held", "ready", "observed", "online", "idle", "running"].includes(status)) return "ok";
    if (["queued", "provisioning", "pending", "unavailable", "uncertain", "stopped"].includes(status)) return "warn";
    if (["failed", "deleted", "deleting", "offline"].includes(status)) return "error";
    return "neutral";
  }

  function statusBadge(status) {
    return `<span class="status-badge ${statusClass(status)}">${escapeHtml(status || "unknown")}</span>`;
  }

  function renderPager(kind) {
    const page = state.pagination[kind];
    if (!page) return "";
    const pages = Math.max(1, Math.ceil(page.total / page.limit));
    const current = Math.min(pages, Math.floor(page.offset / page.limit) + 1);
    return `<div class="pagination" aria-label="Paginación">
      <span>${page.total} registros · página ${current} de ${pages}</span>
      <div class="pagination-actions">
        <button class="button secondary" type="button" data-page-kind="${kind}" data-page-action="prev" ${current <= 1 ? "disabled" : ""}>Anterior</button>
        <button class="button secondary" type="button" data-page-kind="${kind}" data-page-action="next" ${current >= pages ? "disabled" : ""}>Siguiente</button>
      </div>
    </div>`;
  }

  function setGuideBaseUrls() {
    const base = window.location.origin;
    ["guide-base-url", "guide-base-url-inline"].forEach((id) => {
      if ($(id)) $(id).textContent = base;
    });
  }

  function showToast(message, error = false) {
    const node = $("toast");
    node.textContent = message;
    node.className = `toast show${error ? " error" : ""}`;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => { node.className = "toast"; }, 4500);
  }

  function showAlert(message, error = true) {
    const node = $("global-alert");
    if (!node) return;
    node.textContent = message || "";
    node.classList.toggle("hidden", !message);
    if (message) node.style.borderColor = error ? "#76504b" : "#3f6f63";
  }

  function newIdempotencyKey(prefix) {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return `${prefix}-${window.crypto.randomUUID()}`;
    return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function api(path, options = {}) {
    const { idempotencyKey, ...requestOptions } = options;
    const headers = {
      ...(requestOptions.body ? { "Content-Type": "application/json" } : {}),
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      ...(requestOptions.headers || {}),
    };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const response = await fetch(path, { ...requestOptions, headers });
    const text = await response.text();
    let body = {};
    try { body = text ? JSON.parse(text) : {}; } catch { body = { detail: text }; }
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
      throw new Error(`${response.status}: ${detail}`);
    }
    return body;
  }

  function setAuth(authorized, message = "") {
    if (!authorized && state.liveTimer) {
      window.clearTimeout(state.liveTimer);
      state.liveTimer = null;
    }
    if (!authorized) state.liveActive = false;
    const dot = document.querySelector(".access-status .status-dot");
    const card = $("auth-status-card");
    const summary = $("auth-summary");
    const footer = $("footer-auth");
    dot.classList.toggle("ok", authorized);
    card.querySelector("strong").textContent = authorized ? "Autorizado" : "Sin autorización";
    card.querySelector("small").textContent = authorized ? "Catálogo y operaciones disponibles." : "Las acciones técnicas requieren Bearer.";
    summary.textContent = authorized ? "Autorizado" : "Pendiente";
    footer.textContent = authorized ? "Autorizado" : "Sin autorizar";
    if (message) $("auth-message").textContent = message;
  }

  function planById(id) {
    return state.plans.find((plan) => plan.plan_id === id);
  }

  function imageById(id) {
    return state.images.find((image) => image.image_id === id);
  }

  function networkProfileById(id) {
    return state.networkProfiles.find((profile) => profile.profile_id === id);
  }

  function collectTags(value) {
    const tags = [...new Set(value.split(",").map((tag) => tag.trim()).filter(Boolean))];
    const validTag = /^[a-z0-9][a-z0-9_-]{0,62}$/;
    if (tags.some((tag) => !validTag.test(tag))) {
      throw new Error("Cada tag debe usar minúsculas, números, guion o guion bajo y tener hasta 63 caracteres.");
    }
    return tags;
  }

  async function loadCatalog() {
    const [plansBody, imagesBody, profilesBody] = await Promise.all([
      api("/v1/plans"),
      api("/v1/images"),
      api("/v1/network-profiles"),
    ]);
    state.plans = plansBody.plans || [];
    state.images = imagesBody.images || [];
    state.networkProfiles = profilesBody.network_profiles || [];
    const planSelect = $("plan");
    const imageSelect = $("image");
    const currentPlan = planSelect.value;
    const currentImage = imageSelect.value;
    planSelect.innerHTML = `<option value="">Selecciona un plan certificado</option>`;
    state.plans.forEach((plan) => {
      const option = document.createElement("option");
      option.value = plan.plan_id;
      option.textContent = `${plan.plan_id} · ${plan.vcpus} vCPU · ${plan.memory_mb} MB · ${plan.disk_gb} GB`;
      planSelect.append(option);
    });
    imageSelect.innerHTML = `<option value="">Selecciona una Golden Image</option>`;
    state.images.forEach((image) => {
      const option = document.createElement("option");
      option.value = image.image_id;
      option.textContent = `${image.image_id} · Golden ${image.template_vm_id}`;
      imageSelect.append(option);
    });
    const profileSelect = $("network-profile");
    const currentProfile = profileSelect.value;
    profileSelect.innerHTML = "";
    state.networkProfiles.forEach((profile) => {
      const option = document.createElement("option");
      option.value = profile.profile_id;
      option.disabled = profile.enabled === false;
      option.textContent = `${profile.label} · ${profile.enabled === false ? "no configurado" : profile.network_mode}`;
      profileSelect.append(option);
    });
    planSelect.value = planById(currentPlan) ? currentPlan : (planById("kvm-2") ? "kvm-2" : "");
    imageSelect.value = imageById(currentImage) ? currentImage : (imageById("ubuntu-2404") ? "ubuntu-2404" : "");
    profileSelect.value = networkProfileById(currentProfile)?.enabled !== false ? currentProfile : "lab_dhcp";
    updateCatalogHelp();
    $("catalog-summary").textContent = `${state.plans.length} planes · ${state.images.length} Golden Images · ${state.networkProfiles.length} perfiles de red`;
  }

  function updateCatalogHelp() {
    const plan = planById($("plan").value);
    const image = imageById($("image").value);
    $("plan-help").textContent = plan ? `${plan.vcpus} vCPU · ${plan.memory_mb} MB · ${plan.disk_gb} GB certificados.` : "Los vCPU, memoria y disco provienen del plan certificado.";
    $("image-help").textContent = image ? `VMID ${image.template_vm_id} · origen ${image.source_node} · ${image.storage}.` : "La Golden Image se conserva sin mutaciones.";
    const profile = networkProfileById($("network-profile").value);
    $("network-profile-help").textContent = profile?.profile_id === "prod_static"
      ? "NetBox asignará una IP libre del /24 autorizado y usará x.x.x.1 como gateway."
      : "La VM recibe su configuración por DHCP en el entorno de laboratorio.";
  }

  function navigate(route, guide = null) {
    const next = route || "create";
    state.guide = guide;
    if (window.location.hash !== `#${next}`) window.location.hash = next;
    else setRoute(next);
  }

  function openGuide(name) {
    if (!name) return;
    const target = [...document.querySelectorAll("[data-guide-target]")].find((item) => item.dataset.guideTarget === name);
    if (!target) return;
    target.open = true;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function setRoute(route) {
    const allowed = ["create", "console", "instances", "operations", "lifecycle", "delete", "observability", "workers", "guides"];
    state.route = allowed.includes(route) ? route : "create";
    document.querySelectorAll("[data-view]").forEach((view) => view.classList.toggle("hidden", view.dataset.view !== state.route));
    document.querySelectorAll("[data-route]").forEach((link) => link.classList.toggle("active", link.dataset.route === state.route));
    if (state.route === "console" || state.route === "instances" || state.route === "lifecycle" || state.route === "delete") refreshInstances().catch(showError);
    if (state.route === "operations") refreshOperations().catch(showError);
    if (state.route === "observability") Promise.all([refreshObservability(), refreshEvents(), refreshWorkers()]).catch(showError);
    if (state.route === "workers") refreshWorkers().catch(showError);
    if (state.route === "console" && state.selectedInstanceId) refreshDetail().catch(showError);
    if (state.route === "guides" && state.guide) window.requestAnimationFrame(() => openGuide(state.guide));
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function showError(error) {
    const message = error instanceof Error ? error.message : String(error);
    showAlert(message, true);
    showToast(message, true);
  }

  function openStep(name) {
    const element = $(`step-${name}`);
    if (element) {
      element.open = true;
      element.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function renderPlanCatalog() {
    if (!state.plans.length) return;
    const plan = planById($("plan").value);
    if (plan) $("plan-help").textContent = `${plan.plan_id}: ${plan.vcpus} vCPU · ${plan.memory_mb} MB · ${plan.disk_gb} GB certificados.`;
  }

  function renderInstances(containerId, instances = state.instances) {
    const container = $(containerId);
    if (!container) return;
    const filter = ($("instance-filter")?.value || "").trim().toLowerCase();
    const filtered = instances.filter((instance) => !filter || String(instance.name || "").toLowerCase().includes(filter));
    if (!filtered.length) {
      container.innerHTML = `<div class="empty-state">No hay instancias para mostrar.</div>${containerId === "instances-table" ? renderPager("instances") : ""}`;
      return;
    }
    container.innerHTML = `<table><thead><tr><th>Nombre</th><th>Plan</th><th>Imagen</th><th>VMID</th><th>Nodo</th><th>Estado</th><th>Energía</th></tr></thead><tbody>${filtered.map((instance) => `
      <tr data-instance-id="${escapeHtml(instance.instance_id)}"><td><strong>${escapeHtml(instance.name)}</strong><br><small>${escapeHtml(instance.instance_id)}</small></td><td>${escapeHtml(instance.plan_id || "—")}</td><td>${escapeHtml(instance.image_id || instance.image)}</td><td>${escapeHtml(instance.vmid ?? "pendiente")}</td><td>${escapeHtml(instance.node || "—")}</td><td>${statusBadge(instance.status)}</td><td>${escapeHtml(instance.power_state || "unknown")}</td></tr>`).join("")}</tbody></table>${containerId === "instances-table" ? renderPager("instances") : ""}`;
    container.querySelectorAll("[data-instance-id]").forEach((row) => row.addEventListener("click", () => selectInstance(row.dataset.instanceId)));
  }

  async function refreshInstances() {
    if (!state.token) return;
    const query = new URLSearchParams({ limit: String(state.route === "delete" ? 200 : state.pagination.instances.limit) });
    if (state.route !== "delete") query.set("offset", String(state.pagination.instances.offset));
    if (["create", "console", "instances", "lifecycle"].includes(state.route)) {
      const status = state.route === "instances" ? ($("instance-status-filter")?.value || "ready") : "ready";
      query.set("status", status);
    } else if (state.route === "delete") {
      query.set("status", "all");
    }
    if ($("include-deleted")?.checked) query.set("include_deleted", "true");
    const body = await api(`/v1/instances?${query.toString()}`);
    state.instances = body.instances || [];
    state.pagination.instances.total = body.total ?? state.instances.length;
    renderInstances("create-instances-table");
    renderInstances("console-instances");
    renderInstances("instances-table");
    const select = $("delete-instance");
    if (select) {
      const current = select.value;
      select.innerHTML = '<option value="">Selecciona una instancia</option>';
      state.instances.filter((instance) => instance.status !== "deleted").forEach((instance) => {
        const option = document.createElement("option");
        option.value = instance.instance_id;
        option.textContent = `${instance.name} · ${instance.vmid ?? "pendiente"} · ${instance.status}`;
        select.append(option);
      });
      if (state.instances.some((instance) => instance.instance_id === current)) select.value = current;
      renderDeletePreview();
    }
    $("metric-instances").textContent = body.total ?? state.instances.length;
  }

  function renderOperationsTable() {
    const containers = [$("console-operations"), $("operations-table")].filter(Boolean);
    const filter = $("operation-filter")?.value || "";
    const operations = filter ? state.operations.filter((operation) => operation.status === filter) : state.operations;
    const html = operations.length ? `<table><thead><tr><th>Hora</th><th>Comando</th><th>Estado</th><th>Instance</th><th>Operación</th></tr></thead><tbody>${operations.map((operation) => `
      <tr data-operation-id="${escapeHtml(operation.operation_id)}"><td>${escapeHtml(formatTime(operation.created_at))}</td><td>${escapeHtml(operation.command)} / ${escapeHtml(operation.action)}</td><td>${statusBadge(operation.status)}</td><td>${escapeHtml(operation.instance_id || "—")}</td><td><small>${escapeHtml(operation.operation_id)}</small></td></tr>`).join("")}</tbody></table>` : '<div class="empty-state">No hay operaciones para mostrar.</div>';
    containers.forEach((container) => {
      container.innerHTML = html + (container.id === "operations-table" ? renderPager("operations") : "");
      container.querySelectorAll("[data-operation-id]").forEach((row) => row.addEventListener("click", () => selectOperation(row.dataset.operationId)));
    });
  }

  async function refreshOperations() {
    if (!state.token) return;
    const preview = state.route !== "operations";
    const query = new URLSearchParams({ limit: String(preview ? 8 : state.pagination.operations.limit), offset: String(preview ? 0 : state.pagination.operations.offset) });
    const filter = $("operation-filter")?.value || "";
    if (!preview && filter) query.set("status", filter);
    const body = await api(`/v1/operations?${query.toString()}`);
    state.operations = body.operations || [];
    state.pagination.operations.total = body.total ?? state.operations.length;
    renderOperationsTable();
    const active = state.operations.filter((operation) => ["queued", "running"].includes(operation.status)).length;
    $("metric-active").textContent = active;
    if ($("operations-live-status")) $("operations-live-status").textContent = `Actualizado ${formatTime(new Date().toISOString())}`;
    const focused = state.operations.find((operation) => ["queued", "running"].includes(operation.status));
    if (focused?.operation_id && focused.operation_id !== state.selectedOperationId) {
      selectOperation(focused.operation_id).catch(showError);
    }
  }

  async function selectOperation(operationId) {
    state.selectedOperationId = operationId;
    const query = new URLSearchParams({ operation_id: operationId, sort: "asc", limit: "200" });
    const [operation, eventsBody] = await Promise.all([
      api(`/v1/operations/${operationId}`),
      api(`/v1/events?${query.toString()}`),
    ]);
    const text = JSON.stringify(operation, null, 2);
    if ($("operation-detail")) $("operation-detail").textContent = text;
    if ($("console-operation-detail") && operation.instance_id === state.selectedInstanceId) $("console-operation-detail").textContent = text;
    if ($("worker-operation-detail")) $("worker-operation-detail").textContent = text;
    const events = eventsBody.events || [];
    renderOperationContext(operation, events, "operation-context");
    renderOperationContext(operation, events, "worker-operation-context");
    renderEventTimeline(events, "operation-timeline");
    renderEventTimeline(events, "worker-operation-timeline");
    if ($("operations-live-status")) {
      const liveNote = ["queued", "running"].includes(operation.status) ? " · actualización en vivo" : "";
      $("operations-live-status").textContent = `Actualizado ${formatTime(new Date().toISOString())}${liveNote}`;
    }
  }

  function renderOperationContext(operation, events, containerId) {
    const container = $(containerId);
    if (!container) return;
    const ordered = events || [];
    const latest = ordered.at(-1) || operation?.latest_event;
    const instanceName = operation?.instance_name || latest?.instance_name || operation?.payload?.spec?.name || operation?.instance_id || "instancia pendiente";
    const vmid = operation?.vmid ?? operation?.payload?.vmid ?? latest?.vmid;
    const worker = operation?.worker_id || latest?.worker_id || "worker pendiente";
    const stage = latest ? eventLabel(latest.kind, latest.status) : operationLabel(operation);
    const steps = ordered.length ? ordered.slice(-12) : [
      { kind: operation?.command || "operation", status: operation?.status, occurred_at: operation?.created_at },
    ];
    container.innerHTML = `<div class="operation-context-head"><div><p class="eyebrow">${["queued", "running"].includes(operation?.status) ? "PROCESO EN VIVO" : "PROCESO REGISTRADO"}</p><h3>${escapeHtml(stage)}</h3><small>${escapeHtml(instanceName)}${vmid != null ? ` · VMID ${escapeHtml(vmid)}` : ""}</small></div>${statusBadge(operation?.status)}</div><div class="operation-context-meta"><span><b>Worker</b>${escapeHtml(worker)}</span><span><b>Duración</b>${escapeHtml(durationLabel(operation))}</span><span><b>Operación</b>${escapeHtml(operation?.operation_id || "—")}</span></div><div class="process-rail" aria-label="Etapas de la operación">${steps.map((event) => `<div class="process-step ${statusClass(event.status)}"><span class="process-step-dot"></span><strong>${escapeHtml(eventLabel(event.kind, event.status))}</strong><small>${escapeHtml(formatTime(event.occurred_at))}</small></div>`).join("")}</div>`;
  }

  function renderEventTimeline(events, containerId) {
    const container = $(containerId);
    if (!container) return;
    if (!events.length) {
      container.innerHTML = '<div class="empty-state compact-empty">Sin etapas registradas.</div>';
      return;
    }
    container.innerHTML = `<div class="timeline-heading"><span>Línea de tiempo</span><small>${events.length} eventos</small></div>${events.map((event) => `
      <div class="timeline-row">
        <span class="timeline-dot ${statusClass(event.status)}"></span>
        <div class="timeline-content"><strong>${escapeHtml(eventLabel(event.kind, event.status))}</strong><small>${escapeHtml([event.command, event.action].filter(Boolean).join(" / ") || "actividad")} · ${escapeHtml(formatTime(event.occurred_at))}</small>${event.detail ? `<p>${escapeHtml(event.detail)}</p>` : ""}</div>
        ${statusBadge(event.status)}
      </div>`).join("")}`;
  }

  async function refreshObservability() {
    if (!state.token) return;
    const body = await api("/v1/observability");
    $("metric-active").textContent = body.active_operations ?? "—";
    $("metric-uncertain").textContent = body.uncertain_operations ?? "—";
    $("metric-workers").textContent = body.workers?.length ?? 0;
    $("metric-capacity").textContent = body.max_concurrent ?? "—";
    $("obs-liveness").textContent = body.worker_liveness || "unknown";
    $("obs-instances").textContent = body.managed_instances ?? "—";
    $("obs-active").textContent = body.active_operations ?? "—";
    $("obs-queue").textContent = body.queue_depth ?? "—";
    $("obs-uncertain").textContent = body.uncertain_operations ?? "—";
    $("obs-capacity").textContent = body.max_concurrent ?? "—";
    const performance = body.performance || {};
    $("obs-total-p95").textContent = metricDuration(performance.total?.p95_seconds);
    $("obs-queue-p95").textContent = metricDuration(performance.queue?.p95_seconds);
    $("obs-throughput").textContent = performance.throughput_operations_per_minute == null
      ? "—"
      : `${Number(performance.throughput_operations_per_minute).toFixed(2)}/min`;
    const instanceCounts = body.instance_status_counts || {};
    const operationCounts = body.operation_status_counts || {};
    $("obs-ready-count").textContent = instanceCounts.ready ?? 0;
    $("obs-stopped-count").textContent = instanceCounts.stopped ?? 0;
    $("obs-provisioning-count").textContent = instanceCounts.provisioning ?? 0;
    $("obs-failed-count").textContent = instanceCounts.failed ?? 0;
    $("obs-completed-count").textContent = operationCounts.completed ?? 0;
    $("obs-events-count").textContent = body.events_total ?? 0;
    $("obs-live-status").textContent = `Actualizado ${formatTime(new Date().toISOString())}`;
    state.liveActive = Number(body.active_operations || 0) > 0 || (body.workers || []).some((worker) => worker.current_operation);
    renderWorkers(body.workers || []);
  }

  function renderWorkers(workers) {
    const container = $("workers-table");
    if (!container) return;
    container.innerHTML = workers.length ? workers.map((worker) => {
      const current = worker.current_operation;
      const last = worker.last_operation;
      const operation = current || last;
      const activity = current
        ? `<div class="worker-activity"><strong>${escapeHtml(operationLabel(current))}</strong><span>${statusBadge(current.status)} ${escapeHtml(current.instance_name || current.instance_id || "sin instancia")}</span><small>Fase: ${escapeHtml(current.phase || "en curso")} · ${escapeHtml(current.latest_event ? eventLabel(current.latest_event.kind, current.latest_event.status) : "Etapa en curso")} · ${escapeHtml(durationLabel(current))}</small><button class="button secondary worker-operation-link" type="button" data-worker-operation="${escapeHtml(current.operation_id)}">Ver ejecución ↗</button></div>`
        : `<div class="worker-activity idle"><strong>Sin operación en ejecución</strong><span>Último heartbeat: ${escapeHtml(formatTime(worker.last_heartbeat))}</span>${operation ? `<small>Última: ${escapeHtml(operation.latest_event ? eventLabel(operation.latest_event.kind, operation.latest_event.status) : operationLabel(operation))} · ${escapeHtml(formatTime(operation.finished_at || operation.created_at))}</small><button class="button secondary worker-operation-link" type="button" data-worker-operation="${escapeHtml(operation.operation_id)}">Ver última ejecución ↗</button>` : ""}</div>`;
      const heartbeatAge = worker.heartbeat_age_seconds == null ? "sin dato" : `${Math.round(worker.heartbeat_age_seconds)}s`;
      const liveness = worker.liveness || "unknown";
      return `<div class="worker-row"><div class="worker-heading"><div><strong>${escapeHtml(worker.worker_id)}</strong><small>${statusBadge(worker.status)} · ${escapeHtml(liveness)} · heartbeat ${escapeHtml(heartbeatAge)}</small></div>${current && liveness === "healthy" ? '<span class="live-dot">● activo</span>' : `<span class="idle-label">${escapeHtml(liveness)}</span>`}</div>${activity}</div>`;
    }).join("") : '<div class="empty-state">No hay heartbeats registrados.</div>';
    container.querySelectorAll("[data-worker-operation]").forEach((button) => button.addEventListener("click", (event) => {
      event.stopPropagation();
      selectOperation(button.dataset.workerOperation).catch(showError);
    }));
  }

  async function refreshWorkers() {
    if (!state.token) return;
    const body = await api("/v1/workers");
    const workers = body.workers || [];
    renderWorkers(workers);
    $("metric-workers").textContent = body.workers?.length ?? 0;
    $("workers-live-status").textContent = `Actualizado ${formatTime(new Date().toISOString())}`;
    const focused = workers.find((worker) => worker.current_operation)?.current_operation
      || workers.find((worker) => worker.last_operation)?.last_operation;
    if (focused?.operation_id && focused.operation_id !== state.selectedOperationId) {
      await selectOperation(focused.operation_id);
    }
  }

  async function refreshEvents() {
    if (!state.token) return;
    const page = state.pagination.events;
    const query = new URLSearchParams({ after: "0", limit: String(page.limit), offset: String(page.offset), sort: "desc" });
    const body = await api(`/v1/events?${query.toString()}`);
    const container = $("events-table");
    const events = body.events || [];
    page.total = body.total ?? events.length;
    container.innerHTML = events.length ? events.map((event) => `<div class="event-row" data-event-operation="${escapeHtml(event.operation_id || "")}"><small>${escapeHtml(formatTime(event.occurred_at))}</small><span><strong>${escapeHtml(eventLabel(event.kind, event.status))}</strong><br><small>${escapeHtml(event.instance_name || event.operation_id || "sin operación")} · ${escapeHtml(event.worker_id || "sin worker")}</small>${event.detail ? `<br><small>${escapeHtml(event.detail)}</small>` : ""}</span>${statusBadge(event.status)}</div>`).join("") : '<div class="empty-state">No hay eventos registrados.</div>';
    container.innerHTML += renderPager("events");
    if ($("obs-live-status")) $("obs-live-status").textContent = `Actualizado ${formatTime(new Date().toISOString())}`;
    container.querySelectorAll("[data-event-operation]").forEach((row) => row.addEventListener("click", () => {
      if (!row.dataset.eventOperation) return;
      selectOperation(row.dataset.eventOperation)
        .then(() => { if (state.route === "observability") navigate("operations"); })
        .catch(showError);
    }));
  }

  async function refreshAll() {
    await Promise.all([refreshInstances(), refreshOperations(), refreshObservability(), refreshWorkers(), refreshEvents()]);
  }

  async function refreshSelectedOperation() {
    if (!state.selectedOperationId) return;
    await selectOperation(state.selectedOperationId);
  }

  function scheduleLiveRefresh() {
    if (state.liveTimer) window.clearTimeout(state.liveTimer);
    if (!state.token) return;
    const delay = state.liveActive ? 2000 : 10000;
    state.liveTimer = window.setTimeout(runLiveRefresh, delay);
  }

  async function runLiveRefresh() {
    if (!state.token) return;
    if (state.liveRequest) {
      scheduleLiveRefresh();
      return;
    }
    state.liveRequest = true;
    try {
      const tasks = [refreshObservability(), refreshWorkers(), refreshEvents()];
      if (["console", "operations"].includes(state.route)) tasks.push(refreshOperations());
      if (["create", "console", "instances", "lifecycle", "delete"].includes(state.route)) tasks.push(refreshInstances());
      if (state.selectedInstanceId && ["console", "lifecycle"].includes(state.route)) tasks.push(refreshDetail());
      if (state.selectedOperationId && ["operations", "workers"].includes(state.route)) tasks.push(refreshSelectedOperation());
      await Promise.allSettled(tasks);
    } finally {
      state.liveRequest = false;
      scheduleLiveRefresh();
    }
  }

  function startLiveUpdates() {
    if (state.liveTimer) window.clearTimeout(state.liveTimer);
    runLiveRefresh().catch(() => scheduleLiveRefresh());
  }

  async function loadNetwork(instanceId) {
    try {
      return await api(`/v1/instances/${instanceId}/network`);
    } catch (error) {
      return { status: "unavailable", reason: error.message, observed_ipv4: [] };
    }
  }

  function networkText(network) {
    if (!network) return "Sin datos de red.";
    const allocation = network.network_allocation;
    if (allocation?.status === "allocated" && allocation.address) {
      return `${allocation.address} · NetBox IPAM · gateway ${allocation.gateway || "pendiente"}`;
    }
    if (allocation?.status === "pending") return "Perfil Prod_Static seleccionado; esperando reserva de IP en NetBox.";
    if (network.observed_ipv4?.length) return `${network.observed_ipv4.join(", ")} · Guest Agent · observado ${formatTime(network.observed_at)}`;
    if (network.configured_ipv4_cidr) return `${network.configured_ipv4_cidr} · configuración declarada; conectividad no confirmada.`;
    if (network.status === "pending") return "VM pendiente de aprovisionamiento.";
    return `IP no confirmada (${network.reason || "Guest Agent no disponible"}).`;
  }

  function renderFacts(instance) {
    return [["Instance ID", instance.instance_id], ["Plan", instance.plan_id], ["Imagen", instance.image_id || instance.image], ["Perfil de red", instance.spec?.network_profile || "lab_dhcp"], ["VMID", instance.vmid ?? "pendiente"], ["Nodo", instance.node || "pendiente"], ["Cluster", instance.cluster || "mng"], ["vCPU", instance.spec?.vcpus], ["Memoria", `${instance.spec?.memory_mb ?? "—"} MB`], ["Disco", `${instance.spec?.disk_gb ?? "—"} GB`], ["IP asignada", instance.network_allocation?.address || "pendiente"], ["Tag", instance.spec?.tags?.join(", ") || "—"]].map(([label, value]) => `<div class="fact"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
  }

  function setPowerButtons(enabled, selector = ".power-action") {
    document.querySelectorAll(selector).forEach((button) => { button.disabled = !enabled; });
  }

  async function refreshPower(instance) {
    if (!instance?.vmid || ["pending", "provisioning", "deleting", "deleted"].includes(instance.status)) {
      setPowerButtons(false);
      setPowerButtons(false, ".lifecycle-action");
      return;
    }
    try {
      const power = await api(`/v1/instances/${instance.instance_id}/power`);
      $("console-power-state").textContent = power.power_state || "unknown";
      $("console-power-note").textContent = power.blocked_reason || "Estado observado en Proxmox.";
      $("lifecycle-power").textContent = power.power_state || "unknown";
      $("lifecycle-note").textContent = power.blocked_reason || "Estado observado en Proxmox.";
      setPowerButtons(power.mutations_available !== false);
      setPowerButtons(power.mutations_available !== false, ".lifecycle-action");
    } catch (error) {
      $("console-power-note").textContent = error.message;
      $("lifecycle-note").textContent = error.message;
      setPowerButtons(false);
      setPowerButtons(false, ".lifecycle-action");
    }
  }

  async function refreshDetail() {
    if (!state.token || !state.selectedInstanceId) return;
    const instance = await api(`/v1/instances/${state.selectedInstanceId}`);
    const network = await loadNetwork(state.selectedInstanceId);
    $("console-detail-empty").classList.add("hidden");
    $("console-detail").classList.remove("hidden");
    $("console-detail-title").textContent = instance.name;
    $("console-detail-status").className = `status-badge ${statusClass(instance.status)}`;
    $("console-detail-status").textContent = instance.status;
    $("console-facts").innerHTML = renderFacts(instance);
    $("console-network-text").textContent = networkText(network);
    $("console-operation-detail").textContent = JSON.stringify(instance.latest_operation || { status: "sin operación" }, null, 2);
    $("lifecycle-title").textContent = instance.name;
    $("lifecycle-subtitle").textContent = `${instance.plan_id || "sin plan"} · ${instance.node || "nodo pendiente"} · VMID ${instance.vmid ?? "pendiente"}`;
    $("lifecycle-status").className = `status-badge ${statusClass(instance.status)}`;
    $("lifecycle-status").textContent = instance.status;
    $("lifecycle-operation").textContent = JSON.stringify(instance.latest_operation || { status: "sin operación" }, null, 2);
    $("delete-preview").textContent = `${instance.name} · ${instance.plan_id || "—"} · VMID ${instance.vmid ?? "pendiente"} · ${instance.status}`;
    $("delete-instance").value = instance.instance_id;
    await refreshPower(instance);
  }

  async function selectInstance(instanceId) {
    state.selectedInstanceId = instanceId;
    navigate("console");
    await refreshDetail();
  }

  function buildCreateBody() {
    const ssh = $("ssh-key").value.trim();
    const password = $("root-password").value;
    const values = ["customer-id", "customer-tag", "order-id", "product-id"].map((id) => $(id).value.trim());
    const body = {
      name: $("name").value.trim(),
      plan_id: $("plan").value,
      image_id: $("image").value,
      network_profile: $("network-profile").value,
      tags: collectTags($("vm-tags").value),
      access: { root_password: password, ssh_public_keys: ssh ? [ssh] : [] },
      start_on_create: $("started").checked,
    };
    if (values.some(Boolean)) {
      if (values.some((value) => !value)) throw new Error("Completa los cuatro campos de trazabilidad o déjalos vacíos.");
      body.commercial_context = { customer_id: values[0], customer_tag: values[1], order_id: values[2], product_id: values[3] };
    }
    return body;
  }

  function redactCreateBody(body) {
    const printable = JSON.parse(JSON.stringify(body));
    if (printable.access && Object.prototype.hasOwnProperty.call(printable.access, "root_password")) {
      printable.access.root_password = "<REDACTED>";
    }
    return printable;
  }

  function renderJson(body) {
    state.lastJson = JSON.stringify(redactCreateBody(body), null, 2);
    $("json-preview").textContent = state.lastJson;
  }

  function renderCurl(body, idempotencyKey) {
    const printable = redactCreateBody(body);
    renderJson(body);
    state.lastCurl = `curl -X POST ${window.location.origin}/v1/instances \\\n  -H 'Authorization: Bearer <TOKEN>' \\\n  -H 'Idempotency-Key: ${idempotencyKey}' \\\n  -H 'Content-Type: application/json' \\\n  --data '${JSON.stringify(printable)}'`;
    $("curl-preview").textContent = state.lastCurl;
    openStep("curl");
  }

  function previewCreateJson() {
    try {
      renderJson(buildCreateBody());
    } catch {
      // El formulario puede estar incompleto mientras el consumidor escribe.
    }
  }

  function updateProgress(operation) {
    const progress = { queued: 12, running: 48, completed: 100, failed: 100, uncertain: 100 }[operation?.status] || 4;
    $("progress-bar").style.width = `${progress}%`;
    $("progress-status").textContent = operation?.status || "Sin operación";
    $("progress-summary").textContent = operation?.status || "Pendiente";
    $("progress-operation").textContent = operation ? JSON.stringify(operation, null, 2) : "Sin eventos de operación.";
  }

  async function pollOperation(operationId, instanceId) {
    openStep("progress");
    for (let attempt = 0; attempt < 180; attempt += 1) {
      const operation = await api(`/v1/operations/${operationId}`);
      updateProgress(operation);
      $("create-response").textContent = JSON.stringify({ operation_id: operation.operation_id, job_id: operation.job_id, instance_id: operation.instance_id, status: operation.status, command: operation.command, action: operation.action }, null, 2);
      if (instanceId && attempt % 3 === 0) {
        const network = await loadNetwork(instanceId);
        $("create-network-text").textContent = networkText(network);
      }
      if (terminalStatuses.includes(operation.status)) {
        await refreshAll();
        if (instanceId) await refreshDetail();
        return operation;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error("La operación sigue activa; revisa Operaciones para continuar el seguimiento.");
  }

  async function createInstance(event) {
    event.preventDefault();
    if (!state.token) return showError(new Error("Autoriza la API antes de crear una máquina."));
    let body;
    try { body = buildCreateBody(); } catch (error) { return showError(error); }
    const idempotencyKey = newIdempotencyKey("portal-create");
    renderCurl(body, idempotencyKey);
    try {
      $("create-button").disabled = true;
      const response = await api("/v1/instances", { method: "POST", body: JSON.stringify(body), idempotencyKey });
      state.selectedInstanceId = response.instance_id;
      $("response-summary").textContent = response.status;
      $("create-response").textContent = JSON.stringify(response, null, 2);
      $("create-message").textContent = `Operación encolada: ${response.job_id || response.operation_id}`;
      openStep("response");
      const operation = await pollOperation(response.operation_id, response.instance_id);
      $("create-message").textContent = `Aprovisionamiento terminó en ${operation.status}.`;
      $("ssh-summary").textContent = operation.status === "completed" ? "La instancia está lista para el acceso inicial configurado." : `La operación terminó en ${operation.status}.`;
    } catch (error) {
      showError(error);
      $("create-message").textContent = error.message;
    } finally {
      $("create-button").disabled = false;
    }
  }

  async function powerAction(action) {
    if (!state.selectedInstanceId) return showError(new Error("Selecciona una instancia primero."));
    try {
      const response = await api(`/v1/instances/${state.selectedInstanceId}/power/${action}`, { method: "POST", idempotencyKey: newIdempotencyKey(`portal-${action}`) });
      showToast(`${action} encolado.`);
      await pollOperation(response.operation_id, state.selectedInstanceId);
    } catch (error) { showError(error); }
  }

  async function destroyInstance(instanceId = state.selectedInstanceId) {
    if (!instanceId) return showError(new Error("Selecciona una instancia primero."));
    const instance = state.instances.find((item) => item.instance_id === instanceId);
    if (!window.confirm(`¿Confirmar destroy de ${instance?.name || instanceId}?`)) return;
    try {
      const response = await api(`/v1/instances/${instanceId}`, { method: "DELETE", idempotencyKey: newIdempotencyKey("portal-destroy") });
      $("delete-message").textContent = `Destroy encolado: ${response.job_id || response.operation_id}`;
      showToast("Destroy encolado.");
      await pollOperation(response.operation_id, instanceId);
      await refreshInstances();
    } catch (error) { showError(error); $("delete-message").textContent = error.message; }
  }

  function renderDeletePreview() {
    const instance = state.instances.find((item) => item.instance_id === $("delete-instance")?.value);
    $("delete-preview").textContent = instance ? `${instance.name} · ${instance.plan_id || "—"} · VMID ${instance.vmid ?? "pendiente"} · ${instance.status}` : "Sin selección.";
    $("delete-submit").disabled = !instance;
  }

  async function authorize(event) {
    event.preventDefault();
    const token = $("api-token").value.trim();
    if (!token) return showError(new Error("Introduce un Bearer token."));
    state.token = token;
    try {
      await loadCatalog();
      setAuth(true, "API autorizada. Catálogo disponible.");
      await refreshAll();
      startLiveUpdates();
    } catch (error) {
      state.token = "";
      setAuth(false, error.message);
      showError(error);
    }
  }

  async function copyCurl() {
    if (!state.lastCurl) return showToast("Genera primero una solicitud.", true);
    try {
      await navigator.clipboard.writeText(state.lastCurl);
      showToast("cURL copiado.");
    } catch (error) { showToast("El navegador no permitió copiar automáticamente.", true); }
  }

  async function copyJson() {
    if (!state.lastJson) return showToast("Completa primero el formulario.", true);
    try {
      await navigator.clipboard.writeText(state.lastJson);
      showToast("JSON copiado.");
    } catch (error) { showToast("El navegador no permitió copiar automáticamente.", true); }
  }

  document.querySelectorAll("[data-route]").forEach((link) => link.addEventListener("click", (event) => {
    if (link.tagName === "A") event.preventDefault();
    navigate(link.dataset.route, link.dataset.guide || null);
  }));
  document.querySelectorAll("[data-step-link]").forEach((link) => link.addEventListener("click", () => openStep(link.dataset.stepLink)));
  $("auth-form").addEventListener("submit", authorize);
  $("load-catalog").addEventListener("click", () => loadCatalog().then(() => setAuth(true, "Catálogo actualizado.")).catch(showError));
  $("create-form").addEventListener("submit", createInstance);
  $("create-form").addEventListener("input", previewCreateJson);
  $("create-form").addEventListener("change", previewCreateJson);
  $("copy-json").addEventListener("click", copyJson);
  $("copy-curl").addEventListener("click", copyCurl);
  $("plan").addEventListener("change", () => { updateCatalogHelp(); renderPlanCatalog(); });
  $("image").addEventListener("change", updateCatalogHelp);
  $("network-profile").addEventListener("change", updateCatalogHelp);
  $("refresh-console").addEventListener("click", () => refreshAll().catch(showError));
  $("refresh-create-instances").addEventListener("click", () => refreshInstances().catch(showError));
  $("refresh-instances").addEventListener("click", () => refreshInstances().catch(showError));
  $("refresh-operations").addEventListener("click", () => refreshOperations().catch(showError));
  $("refresh-events").addEventListener("click", () => refreshEvents().catch(showError));
  $("refresh-workers").addEventListener("click", () => refreshWorkers().catch(showError));
  $("refresh-detail").addEventListener("click", () => refreshDetail().catch(showError));
  $("instance-filter").addEventListener("input", () => renderInstances("instances-table"));
  $("instance-status-filter").addEventListener("change", () => { state.pagination.instances.offset = 0; refreshInstances().catch(showError); });
  $("instance-page-size").addEventListener("change", (event) => { state.pagination.instances.limit = Number(event.target.value) || 20; state.pagination.instances.offset = 0; refreshInstances().catch(showError); });
  $("include-deleted").addEventListener("change", () => { state.pagination.instances.offset = 0; refreshInstances().catch(showError); });
  $("operation-filter").addEventListener("change", () => { state.pagination.operations.offset = 0; refreshOperations().catch(showError); });
  $("operation-page-size").addEventListener("change", (event) => { state.pagination.operations.limit = Number(event.target.value) || 20; state.pagination.operations.offset = 0; refreshOperations().catch(showError); });
  $("event-page-size").addEventListener("change", (event) => { state.pagination.events.limit = Number(event.target.value) || 20; state.pagination.events.offset = 0; refreshEvents().catch(showError); });
  $("delete-instance").addEventListener("change", renderDeletePreview);
  $("delete-submit").addEventListener("click", () => destroyInstance($("delete-instance").value));
  $("console-delete").addEventListener("click", () => destroyInstance());
  document.querySelectorAll(".power-action, .lifecycle-action").forEach((button) => button.addEventListener("click", () => powerAction(button.dataset.action)));
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-page-kind][data-page-action]");
    if (!button || button.disabled) return;
    const kind = button.dataset.pageKind;
    const page = state.pagination[kind];
    if (!page) return;
    const pages = Math.max(1, Math.ceil(page.total / page.limit));
    const current = Math.floor(page.offset / page.limit) + 1;
    if (button.dataset.pageAction === "prev" && current > 1) page.offset -= page.limit;
    if (button.dataset.pageAction === "next" && current < pages) page.offset += page.limit;
    if (kind === "instances") refreshInstances().catch(showError);
    if (kind === "operations") refreshOperations().catch(showError);
    if (kind === "events") refreshEvents().catch(showError);
  });
  window.addEventListener("hashchange", () => setRoute(window.location.hash.slice(1)));

  setGuideBaseUrls();
  setAuth(false);
  setRoute(window.location.hash.slice(1) || "create");
})();
