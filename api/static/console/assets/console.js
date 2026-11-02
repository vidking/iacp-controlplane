(() => {
  const state = { token: "", selectedId: null };
  const $ = (id) => document.getElementById(id);

  function showToast(message, error = false) {
    const node = $("toast");
    node.textContent = message;
    node.className = `toast show${error ? " error" : ""}`;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => { node.className = "toast"; }, 4200);
  }

  function setMessage(id, message, error = false) {
    const node = $(id);
    node.textContent = message || "";
    node.classList.toggle("hidden", !message);
    node.classList.toggle("error", error);
  }

  function newIdempotencyKey(prefix) {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return `${prefix}-${window.crypto.randomUUID()}`;
    }
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

  function setAuthorized(authorized) {
    $("runtime-badge").textContent = authorized ? "Autorizado" : "Sin autorizar";
    $("runtime-badge").className = `badge ${authorized ? "ok" : "muted"}`;
    $("auth-dot").className = `status-dot ${authorized ? "ok" : ""}`;
  }

  function operationPayload(operation) {
    return JSON.stringify(operation, null, 2);
  }

  async function loadCatalog() {
    const [imagesBody, plansBody, profilesBody] = await Promise.all([
      api("/v1/images"),
      api("/v1/plans"),
      api("/v1/network-profiles"),
    ]);
    const imageSelect = $("image");
    imageSelect.innerHTML = "";
    (imagesBody.images || []).forEach((image) => {
      const option = document.createElement("option");
      option.value = image.image_id;
      option.textContent = `${image.image_id} · Golden ${image.template_vm_id}`;
      imageSelect.append(option);
    });
    const planSelect = $("plan");
    planSelect.innerHTML = "";
    (plansBody.plans || []).forEach((plan) => {
      const option = document.createElement("option");
      option.value = plan.plan_id;
      option.textContent = `${plan.plan_id} · ${plan.vcpus} vCPU · ${plan.memory_mb} MB · ${plan.disk_gb} GB`;
      planSelect.append(option);
    });
    if ([...planSelect.options].some((option) => option.value === "kvm-2")) planSelect.value = "kvm-2";
    const profileSelect = $("network-profile");
    profileSelect.innerHTML = "";
    (profilesBody.network_profiles || []).forEach((profile) => {
      const option = document.createElement("option");
      option.value = profile.profile_id;
      option.disabled = profile.enabled === false;
      option.textContent = `${profile.label} · ${profile.enabled === false ? "no configurado" : profile.network_mode}`;
      profileSelect.append(option);
    });
    profileSelect.value = "lab_dhcp";
    updateNetworkProfileHelp();
  }

  function updateNetworkProfileHelp() {
    const profile = $("network-profile").value;
    $("network-profile-help").textContent = profile === "prod_static"
      ? "NetBox asignará la IP libre del /24 autorizado y x.x.x.1 será el gateway."
      : "DHCP de laboratorio; no se envía una IP desde el consumidor.";
  }

  function renderWorkers(body) {
    const node = $("workers");
    if (!body.workers || !body.workers.length) {
      node.innerHTML = '<p class="muted-text">No hay heartbeats registrados.</p>';
      return;
    }
    node.innerHTML = body.workers.map((worker) => `
      <div class="worker-row">
        <div><strong>${escapeHtml(worker.worker_id)}</strong><small>${escapeHtml(worker.current_operation_id || "sin operación")}</small></div>
        <span class="badge ${worker.status === "idle" ? "ok" : "warn"}">${escapeHtml(worker.status)}</span>
      </div>`).join("");
  }

  async function loadObservability() {
    if (!state.token) return;
    try {
      const body = await api("/v1/observability");
      renderWorkers(body);
    } catch (error) { setMessage("auth-message", error.message, true); }
  }

  function renderInstances(body) {
    const node = $("instances");
    if (!body.instances || !body.instances.length) {
      node.innerHTML = '<p class="muted-text">No hay instancias activas.</p>';
      return;
    }
    node.innerHTML = body.instances.map((instance) => `
      <div class="instance-row ${instance.instance_id === state.selectedId ? "selected" : ""}" data-instance-id="${escapeHtml(instance.instance_id)}">
        <div><strong>${escapeHtml(instance.name)}</strong><small>${escapeHtml(instance.image_id || instance.image)} · ${escapeHtml(instance.stack)} · VMID ${escapeHtml(instance.vmid ?? "pendiente")}</small></div>
        <div class="instance-meta"><span class="badge ${statusClass(instance.status)}">${escapeHtml(instance.status)}</span><span class="pill">${escapeHtml(instance.power_state || "unknown")}</span></div>
      </div>`).join("");
    node.querySelectorAll("[data-instance-id]").forEach((item) => item.addEventListener("click", () => selectInstance(item.dataset.instanceId)));
  }

  async function loadInstances() {
    if (!state.token) return;
    try {
      renderInstances(await api("/v1/instances"));
    } catch (error) { showToast(error.message, true); }
  }

  async function selectInstance(instanceId) {
    state.selectedId = instanceId;
    await refreshDetail();
    await loadInstances();
  }

  async function refreshDetail() {
    if (!state.selectedId || !state.token) return;
    try {
      const instance = await api(`/v1/instances/${state.selectedId}`);
      $("detail-empty").classList.add("hidden");
      $("detail-content").classList.remove("hidden");
      $("detail-title").textContent = instance.name;
      $("detail-status").textContent = instance.status;
      $("detail-status").className = `badge ${statusClass(instance.status)}`;
      $("detail-facts").innerHTML = [
        ["Instance ID", instance.instance_id], ["VMID", instance.vmid ?? "pendiente"],
        ["Nodo", instance.node || "pendiente"], ["Cluster", instance.cluster || "mng"],
        ["Plan", instance.plan_id], ["Imagen", instance.image_id || instance.image],
        ["vCPU", instance.spec.vcpus], ["Memoria", `${instance.spec.memory_mb} MB`],
        ["Disco", `${instance.spec.disk_gb} GB`],
      ].map(([label, value]) => `<div class="fact"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
      $("operation-output").textContent = operationPayload(instance.latest_operation || { status: "sin operación" });
      if (instance.vmid && !["pending", "provisioning", "deleting", "deleted"].includes(instance.status)) {
        renderPower(instance.power_state, "Estado persistido; actualizando Proxmox…", true);
        try {
          const power = await api(`/v1/instances/${state.selectedId}/power`);
          renderPower(power.power_state, power.blocked_reason || "Estado observado en Proxmox.", power.mutations_available);
        } catch (error) {
          renderPower(instance.power_state || "unknown", error.message, false);
        }
      } else {
        renderPower(instance.power_state || "unknown", "La instancia todavía está aprovisionándose.", false);
      }
      updateActionButtons(instance);
    } catch (error) { showToast(error.message, true); }
  }

  function renderPower(powerState, note, enabled) {
    $("power-state").textContent = powerState || "unknown";
    $("power-note").textContent = note || "";
    document.querySelectorAll(".power-action").forEach((button) => { button.disabled = !enabled; });
  }

  function updateActionButtons(instance) {
    const busy = ["pending", "provisioning", "deleting"].includes(instance.status);
    document.querySelectorAll(".power-action").forEach((button) => { button.disabled = busy || instance.status === "deleted"; });
    $("delete-button").disabled = busy || instance.status === "deleted";
  }

  async function pollOperation(operationId, instanceId = state.selectedId) {
    for (let attempt = 0; attempt < 180; attempt += 1) {
      const operation = await api(`/v1/operations/${operationId}`);
      $("operation-output").textContent = operationPayload(operation);
      if (["completed", "failed", "uncertain"].includes(operation.status)) {
        if (instanceId) state.selectedId = instanceId;
        await loadInstances();
        await refreshDetail();
        return operation;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error("La operación sigue activa; continúa observándola desde Operaciones.");
  }

  async function createInstance(event) {
    event.preventDefault();
    if (!state.token) return setMessage("create-message", "Autoriza primero la API.", true);
    const ssh = $("ssh-key").value.trim();
    const password = $("root-password").value;
    const commercialValues = ["customer-id", "customer-tag", "order-id", "product-id"].map((id) => $(id).value.trim());
    const hasCommercialContext = commercialValues.some(Boolean);
    if (hasCommercialContext && commercialValues.some((value) => !value)) {
      return setMessage("create-message", "Completa los cuatro campos de trazabilidad comercial o déjalos vacíos.", true);
    }
    const body = {
      name: $("name").value.trim(),
      plan_id: $("plan").value,
      image_id: $("image").value,
      network_profile: $("network-profile").value,
      access: { ssh_public_keys: ssh ? [ssh] : [] },
      start_on_create: $("started").checked,
    };
    if (password) body.access.root_password = password;
    if (hasCommercialContext) {
      body.commercial_context = {
        customer_id: commercialValues[0], customer_tag: commercialValues[1],
        order_id: commercialValues[2], product_id: commercialValues[3],
      };
    }
    try {
      const result = await api("/v1/instances", {
        method: "POST",
        body: JSON.stringify(body),
        idempotencyKey: newIdempotencyKey("console-create"),
      });
      state.selectedId = result.instance_id;
      setMessage("create-message", `Instancia encolada: ${result.job_id || result.operation_id}`);
      const operation = await pollOperation(result.operation_id, result.instance_id);
      setMessage("create-message", `Aprovisionamiento terminó en ${operation.status}.`, operation.status !== "completed");
    } catch (error) { setMessage("create-message", error.message, true); }
  }

  async function powerAction(action) {
    if (!state.selectedId) return;
    try {
      const result = await api(`/v1/instances/${state.selectedId}/power/${action}`, {
        method: "POST",
        idempotencyKey: newIdempotencyKey(`console-${action}`),
      });
      showToast(`${action} encolado.`);
      const operation = await pollOperation(result.operation_id);
      if (operation.status === "completed") showToast(`${action} completado.`);
      else showToast(`${action}: ${operation.error_code || operation.status}`, true);
    } catch (error) { showToast(error.message, true); }
  }

  async function destroyInstance() {
    if (!state.selectedId || !window.confirm("¿Destruir esta instancia?")) return;
    try {
      const result = await api(`/v1/instances/${state.selectedId}`, {
        method: "DELETE",
        idempotencyKey: newIdempotencyKey("console-destroy"),
      });
      showToast("Destroy encolado.");
      const operation = await pollOperation(result.operation_id);
      showToast(`Destroy terminó en ${operation.status}.`, operation.status !== "completed");
    } catch (error) { showToast(error.message, true); }
  }

  function statusClass(status) {
    if (["completed", "ready", "idle", "running"].includes(status)) return "ok";
    if (["queued", "provisioning", "uncertain", "stopped"].includes(status)) return "warn";
    if (["failed", "deleted", "deleting"].includes(status)) return "error";
    return "muted";
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
  }

  async function authorize(event) {
    event.preventDefault();
    state.token = $("api-token").value.trim();
    if (!state.token) return setMessage("auth-message", "Introduce un token.", true);
    try {
      await loadCatalog();
      await Promise.all([loadInstances(), loadObservability()]);
      setAuthorized(true);
      setMessage("auth-message", "API autorizada.");
    } catch (error) {
      state.token = "";
      setAuthorized(false);
      setMessage("auth-message", error.message, true);
    }
  }

  $("auth-form").addEventListener("submit", authorize);
  $("instance-form").addEventListener("submit", createInstance);
  $("refresh-instances").addEventListener("click", loadInstances);
  $("refresh-observability").addEventListener("click", loadObservability);
  $("network-profile").addEventListener("change", updateNetworkProfileHelp);
  $("refresh-detail").addEventListener("click", refreshDetail);
  $("delete-button").addEventListener("click", destroyInstance);
  document.querySelectorAll(".power-action").forEach((button) => button.addEventListener("click", () => powerAction(button.dataset.action)));

  window.setInterval(() => { if (state.token) { loadObservability(); loadInstances(); if (state.selectedId) refreshDetail(); } }, 10000);
})();
