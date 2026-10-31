(() => {
  const state = { token: "", selectedId: null, planOperationId: null, timer: null };
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

  async function api(path, options = {}) {
    const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const response = await fetch(path, { ...options, headers });
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
    const body = await api("/v1/images");
    const select = $("image");
    select.innerHTML = "";
    (body.images || []).forEach((image) => {
      const option = document.createElement("option");
      option.value = image.image_id;
      option.textContent = `${image.image_id} · Golden ${image.template_vm_id}`;
      select.append(option);
    });
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
        <div><strong>${escapeHtml(instance.name)}</strong><small>${escapeHtml(instance.image)} · ${escapeHtml(instance.stack)} · VMID ${escapeHtml(instance.vmid ?? "pendiente")}</small></div>
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
        ["Nodo", instance.node || "mng01"], ["Imagen", instance.image],
        ["Stack", instance.stack], ["vCPU", instance.spec.vcpus],
        ["Memoria", `${instance.spec.memory_mb} MB`], ["Disco", `${instance.spec.disk_gb} GB`],
      ].map(([label, value]) => `<div class="fact"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
      $("operation-output").textContent = operationPayload(instance.latest_operation || { status: "sin operación" });
      renderPower(instance.power_state, "Estado persistido; actualizando Proxmox…", true);
      try {
        const power = await api(`/v1/instances/${state.selectedId}/power`);
        renderPower(power.power_state, power.blocked_reason || "Estado observado en Proxmox.", power.mutations_available);
      } catch (error) {
        renderPower(instance.power_state || "unknown", error.message, false);
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
    const disabled = instance.status === "deleted" || instance.status === "powering" || instance.status === "destroying";
    document.querySelectorAll(".power-action").forEach((button) => { button.disabled = disabled; });
    $("delete-button").disabled = disabled || instance.status === "pending" || instance.status === "planning" || instance.status === "applying";
  }

  async function pollOperation(operationId, instanceId = state.selectedId) {
    for (let attempt = 0; attempt < 180; attempt += 1) {
      const operation = await api(`/v1/operations/${operationId}`);
      $("operation-output").textContent = operationPayload(operation);
      if (["succeeded", "failed", "uncertain"].includes(operation.status)) {
        if (instanceId) state.selectedId = instanceId;
        await loadInstances();
        await refreshDetail();
        return operation;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error("La operación sigue activa; continúa observándola desde Operaciones.");
  }

  async function createPlan(event) {
    event.preventDefault();
    if (!state.token) return setMessage("create-message", "Autoriza primero la API.", true);
    const ssh = $("ssh-key").value.trim();
    const rootPassword = $("root-password").value;
    $("root-password").value = "";
    const tags = $("tags").value.split(",").map((value) => value.trim()).filter(Boolean);
    const body = {
      stack: $("stack").value,
      spec: { name: $("name").value.trim(), image: $("image").value, vcpus: Number($("vcpus").value), memory_mb: Number($("memory").value), disk_gb: Number($("disk").value), network_mode: $("network").value, started: $("started").checked, ssh_public_keys: ssh ? [ssh] : [], root_password: rootPassword || undefined, tags },
    };
    try {
      const result = await api("/v1/instances", { method: "POST", body: JSON.stringify(body) });
      state.selectedId = result.instance_id;
      state.planOperationId = result.operation_id;
      $("apply-button").disabled = true;
      setMessage("create-message", `Plan encolado: ${result.operation_id}`);
      const operation = await pollOperation(result.operation_id, result.instance_id);
      if (operation.status === "succeeded") {
        $("apply-button").disabled = false;
        setMessage("create-message", "Plan listo. Revisa la operación y pulsa Aplicar plan.");
      } else setMessage("create-message", `El plan terminó en ${operation.status}.`, true);
    } catch (error) { setMessage("create-message", error.message, true); }
  }

  async function applyPlan() {
    if (!state.selectedId) return;
    try {
      const result = await api(`/v1/instances/${state.selectedId}/provision`, { method: "POST", body: JSON.stringify({ plan_operation_id: state.planOperationId }) });
      $("apply-button").disabled = true;
      setMessage("create-message", `Apply encolado: ${result.operation_id}`);
      const operation = await pollOperation(result.operation_id, result.instance_id);
      setMessage("create-message", `Apply terminó en ${operation.status}.`, operation.status !== "succeeded");
    } catch (error) { setMessage("create-message", error.message, true); }
  }

  async function powerAction(action) {
    if (!state.selectedId) return;
    try {
      const result = await api(`/v1/instances/${state.selectedId}/power/${action}`, { method: "POST" });
      showToast(`${action} encolado.`);
      const operation = await pollOperation(result.operation_id);
      if (operation.status === "succeeded") showToast(`${action} completado.`);
      else showToast(`${action}: ${operation.error || operation.status}`, true);
    } catch (error) { showToast(error.message, true); }
  }

  async function destroyInstance() {
    if (!state.selectedId || !window.confirm("¿Destruir esta instancia mediante Terraform?")) return;
    try {
      const result = await api(`/v1/instances/${state.selectedId}`, { method: "DELETE" });
      showToast("Destroy encolado.");
      const operation = await pollOperation(result.operation_id);
      showToast(`Destroy terminó en ${operation.status}.`, operation.status !== "succeeded");
    } catch (error) { showToast(error.message, true); }
  }

  function statusClass(status) {
    if (["succeeded", "ready", "idle", "stopped", "running"].includes(status)) return "ok";
    if (["uncertain", "powering", "running"].includes(status)) return "warn";
    if (["failed", "deleted"].includes(status)) return "error";
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
  $("instance-form").addEventListener("submit", createPlan);
  $("apply-button").addEventListener("click", applyPlan);
  $("refresh-instances").addEventListener("click", loadInstances);
  $("refresh-observability").addEventListener("click", loadObservability);
  $("refresh-detail").addEventListener("click", refreshDetail);
  $("delete-button").addEventListener("click", destroyInstance);
  document.querySelectorAll(".power-action").forEach((button) => button.addEventListener("click", () => powerAction(button.dataset.action)));

  window.setInterval(() => { if (state.token) { loadObservability(); loadInstances(); if (state.selectedId) refreshDetail(); } }, 10000);
})();
