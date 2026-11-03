#!/usr/bin/env node
/*
 * Ejecuta la matriz de 16 casos desde el portal visual /docs.
 * El navegador envía las acciones de creación y lifecycle; la API de solo
 * lectura se usa para seguimiento, validación de red/SSH y comprobar que no
 * queden recursos físicos después de cada caso.
 */

"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);
const playwrightModule = process.env.IACP_PLAYWRIGHT_MODULE || "/home/despinoza/.local/share/mise/installs/npm-playwright/1.64.1/node_modules/playwright";
const { chromium } = require(playwrightModule);

const BASE_URL = (process.env.IACP_E2E_BASE_URL || "https://iac.cyberfuel.com").replace(/\/$/, "");
const API_TOKEN = process.env.IACP_E2E_API_TOKEN || process.env.IACP_API_TOKEN || "";
const PROXMOX_ENDPOINT = (process.env.IACP_E2E_MNG_PROXMOX_ENDPOINT || process.env.IACP_PROXMOX_MNG_ENDPOINT || "").replace(/\/$/, "");
const PROXMOX_TOKEN = process.env.IACP_E2E_MNG_PROXMOX_TOKEN || process.env.IACP_PROXMOX_MNG_API_TOKEN || "";
const POOL_ID = process.env.IACP_E2E_POOL_ID || "iacp-lab";
const KEY_DIR = process.env.IACP_E2E_KEY_DIR || "";
const PUBLIC_KEY = (process.env.IACP_E2E_SSH_PUBLIC_KEY || "").trim();
const PRIVATE_KEY = process.env.IACP_E2E_SSH_PRIVATE_KEY || "/home/despinoza/.ssh/iacp-lab-kvm";
const NETWORK_PROFILE = process.env.IACP_E2E_NETWORK_PROFILE || "lab_dhcp";
const RUN_ID = process.env.IACP_E2E_RUN_ID || new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
const OPERATION_TIMEOUT = Number(process.env.IACP_E2E_OPERATION_TIMEOUT || 1800);
const NETWORK_TIMEOUT = Number(process.env.IACP_E2E_NETWORK_TIMEOUT || 360);
const SSH_TIMEOUT = Number(process.env.IACP_E2E_SSH_TIMEOUT || 360);
const SSH_ATTEMPT_TIMEOUT = Number(process.env.IACP_E2E_SSH_ATTEMPT_TIMEOUT || 45);
const PING_TIMEOUT = Number(process.env.IACP_E2E_PING_TIMEOUT || 8);
const MAX_WORKERS = Number(process.env.IACP_E2E_MAX_WORKERS || 2);

const IMAGES = ["ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10"];
const PLANS = ["kvm-1", "kvm-2", "kvm-3", "kvm-4"];
const WEAK_PASSWORDS = [
  // Deliberately predictable lab values. They remain weak operationally,
  // but satisfy the AlmaLinux Golden Image policy (15 chars and four
  // character classes) so this matrix can exercise the complete lifecycle.
  "IacpLab!2026A001", "CyberFuel!2026A1", "TestKvm!2026A01", "VmLab!2026A0001", "Control!2026A01", "QemuLab!2026A01", "Cloud!2026A0001", "Provision!2026A1",
  "Network!2026A01", "Reboot!2026A001", "Shutdown!2026A1", "Destroy!2026A01", "AlmaLab!2026A01", "Ubuntu!2026A001", "Iacp!Test2026A1", "Lab!Kvm2026A0001",
];
const KEY_VARIANTS = ["ed25519-a", "rsa-3072", "ecdsa-p256", "ed25519-b"];
const terminalStatuses = new Set(["completed", "failed", "uncertain"]);

class E2EError extends Error {
  constructor(message, details = null) {
    super(message);
    this.details = details;
  }
}

function redact(value) {
  let text = String(value ?? "");
  for (const secret of [API_TOKEN, PROXMOX_TOKEN, ...WEAK_PASSWORDS]) {
    if (secret) text = text.split(secret).join("<redacted>");
  }
  if (KEY_DIR) {
    for (const variant of KEY_VARIANTS) {
      const publicPath = path.join(KEY_DIR, `${variant}.pub`);
      if (fs.existsSync(publicPath)) text = text.split(fs.readFileSync(publicPath, "utf8").trim()).join("<redacted>");
    }
  }
  return text.replaceAll("root_password", "<secret>");
}

async function jsonRequest(method, url, body = undefined, headers = {}, timeoutMs = 60000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const requestHeaders = { Accept: "application/json", ...headers };
    const options = { method, headers: requestHeaders, signal: controller.signal };
    if (body !== undefined) {
      requestHeaders["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const text = await response.text();
    let payload = {};
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text.slice(0, 500) }; }
    if (!response.ok) throw new E2EError(`HTTP ${response.status} ${method} ${url}: ${redact(payload.detail || text)}`, payload);
    return payload;
  } catch (error) {
    if (error instanceof E2EError) throw error;
    throw new E2EError(`${method} ${url}: ${redact(error.message)}`);
  } finally {
    clearTimeout(timer);
  }
}

async function api(method, endpoint, body = undefined, idempotencyKey = undefined) {
  const headers = { Authorization: `Bearer ${API_TOKEN}` };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return jsonRequest(method, `${BASE_URL}${endpoint}`, body, headers);
}

async function proxmox(endpoint) {
  if (!PROXMOX_ENDPOINT || !PROXMOX_TOKEN) throw new E2EError("faltan credenciales de observación Proxmox");
  const body = await jsonRequest("GET", `${PROXMOX_ENDPOINT}/api2/json${endpoint}`, undefined, {
    Authorization: `PVEAPIToken=${PROXMOX_TOKEN}`,
  });
  return body?.data ?? body;
}

function operationStatus(operation) {
  return String(operation?.status || operation?.normalized_status || "unknown");
}

async function waitOperation(operationId, instanceId, telemetry) {
  const deadline = Date.now() + OPERATION_TIMEOUT * 1000;
  let last = null;
  let reconciled = false;
  while (Date.now() < deadline) {
    last = await api("GET", `/v1/operations/${encodeURIComponent(operationId)}`);
    const status = operationStatus(last);
    try {
      const [workers, observability] = await Promise.all([
        api("GET", "/v1/workers"),
        api("GET", "/v1/observability"),
      ]);
      const current = (workers.workers || []).find((worker) => worker.current_operation?.operation_id === operationId);
      if (current || Number(observability.active_operations || 0) > 0) telemetry.liveObserved = true;
      telemetry.workerSnapshots.push({
        at: new Date().toISOString(),
        active: observability.active_operations ?? null,
        worker: current?.worker_id || null,
        status,
      });
    } catch (error) {
      telemetry.observationErrors.push(redact(error.message));
    }
    if (status === "completed") return last;
    if (status === "uncertain" && instanceId && !reconciled) {
      const recovery = await api(
        "POST",
        `/v1/instances/${encodeURIComponent(instanceId)}/reconcile?source_operation_id=${encodeURIComponent(operationId)}`,
        undefined,
        `docs-${RUN_ID}-${operationId}-reconcile`,
      );
      reconciled = true;
      operationId = recovery.operation_id;
      continue;
    }
    if (terminalStatuses.has(status)) throw new E2EError(`operación ${operationId} terminó en ${status}`, last);
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  throw new E2EError(`timeout esperando operación ${operationId}`, last);
}

async function getPower(instanceId) {
  return api("GET", `/v1/instances/${encodeURIComponent(instanceId)}/power`);
}

async function waitPower(instanceId, expected) {
  const deadline = Date.now() + 240000;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await getPower(instanceId);
      if (last.power_state === expected) return last;
    } catch (error) {
      last = { error: redact(error.message) };
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  throw new E2EError(`la instancia no llegó a ${expected}`, last);
}

async function waitNetwork(instanceId) {
  const deadline = Date.now() + NETWORK_TIMEOUT * 1000;
  let last = null;
  while (Date.now() < deadline) {
    last = await api("GET", `/v1/instances/${encodeURIComponent(instanceId)}/network`);
    for (const value of last.observed_ipv4 || []) {
      if (/^(?!127\.)(?!169\.254\.)\d+\.\d+\.\d+\.\d+$/.test(value)) return value;
    }
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  throw new E2EError("no se observó IPv4 por Guest Agent", last);
}

async function command(file, args, options = {}) {
  try {
    return await execFileAsync(file, args, { timeout: options.timeout || 30000, env: options.env || process.env, maxBuffer: 1024 * 1024 });
  } catch (error) {
    throw new E2EError(`${file} falló: ${redact(error.stderr || error.message)}`);
  }
}

async function pingHost(ip) {
  try {
    await command("ping", ["-c", "2", "-W", "2", ip], { timeout: PING_TIMEOUT * 1000 });
    return "success";
  } catch {
    return "blocked_or_timeout";
  }
}

async function sshCommand(ip, mode, password, privateKey) {
  const args = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", `ConnectTimeout=${Math.min(20, SSH_ATTEMPT_TIMEOUT)}`, "-o", "ConnectionAttempts=1",
    "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", "-o", "NumberOfPasswordPrompts=1",
    "-o", "LogLevel=ERROR",
  ];
  const env = { ...process.env };
  let askpassPath = null;
  if (mode === "key") {
    if (!privateKey || !fs.existsSync(privateKey) || (fs.statSync(privateKey).mode & 0o077)) throw new E2EError(`llave privada insegura: ${privateKey}`);
    args.push("-i", privateKey, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes");
  } else {
    const askpassDir = fs.mkdtempSync(path.join(os.tmpdir(), "iacp-docs-ssh-"));
    askpassPath = path.join(askpassDir, "askpass");
    fs.writeFileSync(askpassPath, "#!/bin/sh\nprintf '%s\\n' \"$IACP_E2E_SSH_PASSWORD\"\n", { mode: 0o700 });
    args.push("-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no", "-o", "BatchMode=no");
    env.DISPLAY = "iacp-docs-e2e";
    env.SSH_ASKPASS = askpassPath;
    env.SSH_ASKPASS_REQUIRE = "force";
    env.IACP_E2E_SSH_PASSWORD = password;
  }
  args.push(`root@${ip}`, "id -u");
  try {
    const result = await command("setsid", ["ssh", ...args], { timeout: Math.min(SSH_TIMEOUT, SSH_ATTEMPT_TIMEOUT) * 1000, env });
    if (result.stdout.trim() !== "0") throw new E2EError(`SSH ${mode} ${ip} no devolvió root`);
    return result.stdout.trim();
  } finally {
    if (askpassPath) {
      try { fs.rmSync(path.dirname(askpassPath), { recursive: true, force: true }); } catch { /* evidence is unaffected */ }
    }
  }
}

async function waitSsh(instanceId, mode, password, privateKey) {
  const deadline = Date.now() + SSH_TIMEOUT * 1000;
  let last = null;
  let lastIp = "pending";
  while (Date.now() < deadline) {
    try {
      const network = await api("GET", `/v1/instances/${encodeURIComponent(instanceId)}/network`);
      for (const ip of network.observed_ipv4 || []) {
        if (!/^\d+\.\d+\.\d+\.\d+$/.test(ip) || ip.startsWith("127.") || ip.startsWith("169.254.")) continue;
        lastIp = ip;
        const ping = await pingHost(ip);
        try {
          await sshCommand(ip, mode, password, privateKey);
          return { ip, ping };
        } catch (error) {
          last = error;
        }
      }
    } catch (error) {
      last = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  throw new E2EError(`SSH ${mode} ${lastIp} no estuvo disponible: ${redact(last?.message || last)}`);
}

function keyMaterial(index) {
  if (!KEY_DIR) return { variant: "default", publicKey: PUBLIC_KEY, privateKey: PRIVATE_KEY };
  const variant = KEY_VARIANTS[(index - 1) % KEY_VARIANTS.length];
  const privateKey = path.join(KEY_DIR, variant);
  const publicKey = `${privateKey}.pub`;
  if (fs.existsSync(privateKey) && fs.existsSync(publicKey)) {
    return { variant, privateKey, publicKey: fs.readFileSync(publicKey, "utf8").trim() };
  }
  return { variant: "default", publicKey: PUBLIC_KEY, privateKey: PRIVATE_KEY };
}

async function ensureAuthorized(page) {
  await page.goto(`${BASE_URL}/docs#create`, { waitUntil: "domcontentloaded" });
  await page.locator("#step-access").evaluate((step) => { step.open = true; });
  await page.locator("#api-token").fill(API_TOKEN);
  await page.locator("#auth-form").evaluate((form) => form.requestSubmit());
  await page.locator("#catalog-summary").waitFor({ state: "visible", timeout: 30000 });
  await page.waitForFunction(() => document.querySelectorAll("#plan option").length >= 5 && document.querySelectorAll("#image option").length >= 5);
}

async function selectInstance(page, instanceId) {
  await page.evaluate(() => { window.location.hash = "#instances"; });
  await page.locator("#instance-status-filter").selectOption("all");
  const row = page.locator(`[data-view="instances"]:not(.hidden) tr[data-instance-id="${instanceId}"]`).first();
  await row.waitFor({ state: "visible", timeout: 30000 });
  await row.click();
  await page.locator('[data-view="console"]:not(.hidden) #console-detail').waitFor({ state: "visible", timeout: 30000 });
}

async function clickPower(page, instanceId, action, telemetry) {
  await selectInstance(page, instanceId);
  const requestPromise = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith(`/power/${action}`));
  const responsePromise = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith(`/power/${action}`));
  await page.locator(`[data-view="console"]:not(.hidden) button.power-action[data-action="${action}"]`).click();
  const request = await requestPromise;
  const response = await responsePromise;
  if (!response.ok()) throw new E2EError(`docs ${action} respondió HTTP ${response.status()}`);
  const body = await response.json();
  telemetry.uiRequests.push({ action, idempotencyKey: request.headers()["idempotency-key"] || null, operationId: body.operation_id });
  return body;
}

async function clickDestroy(page, instanceId, telemetry) {
  await selectInstance(page, instanceId);
  const requestPromise = page.waitForRequest((request) => request.method() === "DELETE" && request.url().endsWith(`/v1/instances/${instanceId}`)).catch((error) => ({ __error: error }));
  const responsePromise = page.waitForResponse((response) => response.request().method() === "DELETE" && response.url().endsWith(`/v1/instances/${instanceId}`)).catch((error) => ({ __error: error }));
  page.once("dialog", (dialog) => { dialog.accept().catch(() => {}); });
  await page.locator("#console-delete").click();
  const request = await requestPromise;
  const response = await responsePromise;
  if (request?.__error) throw new E2EError(`docs destroy no generó request: ${request.__error.message}`);
  if (response?.__error) {
    // The portal refreshes the instances view immediately after accepting a
    // DELETE. On a busy remote deployment Playwright can lose the response
    // event even though the request was accepted and the async operation
    // already completed. Recover the operation from the authoritative detail
    // endpoint before calling the case a failure.
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      const current = await api("GET", `/v1/instances/${encodeURIComponent(instanceId)}`);
      const operationId = current.latest_operation?.operation_id || current.operation_id;
      if (current.status === "deleted" && operationId) {
        telemetry.uiRequests.push({ action: "destroy", idempotencyKey: request.headers()["idempotency-key"] || null, operationId, responseObserved: false });
        return { operation_id: operationId };
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    throw new E2EError(`docs destroy no generó response: ${response.__error.message}`);
  }
  if (!response.ok()) throw new E2EError(`docs destroy respondió HTTP ${response.status()}`);
  const body = await response.json();
  telemetry.uiRequests.push({ action: "destroy", idempotencyKey: request.headers()["idempotency-key"] || null, operationId: body.operation_id });
  return body;
}

async function createFromDocs(page, testCase, telemetry) {
  const { image, plan, mode, index } = testCase;
  await page.evaluate(() => { window.location.hash = "#create"; });
  await page.locator('[data-view="create"]:not(.hidden) #create-form').waitFor({ state: "visible", timeout: 30000 });
  await page.locator("#plan option[value='" + plan + "']").waitFor({ state: "attached", timeout: 30000 });
  const password = WEAK_PASSWORDS[(index - 1) % WEAK_PASSWORDS.length];
  const key = mode === "key" ? keyMaterial(index) : { publicKey: "", privateKey: "" };
  const name = `e2e-docs-${RUN_ID}-${String(index).padStart(2, "0")}-${image.replaceAll("-", "")}-${plan.replaceAll("-", "")}-${mode}`;
  await page.locator("#plan").selectOption(plan);
  await page.locator("#image").selectOption(image);
  await page.locator("#environment").selectOption("lab");
  const networkProfile = page.locator("#network-profile");
  if (await networkProfile.count()) await networkProfile.selectOption(NETWORK_PROFILE);
  await page.locator("#name").fill(name);
  await page.locator("#root-password").fill(password);
  await page.locator("#ssh-key").fill(key.publicKey);
  await page.locator(".advanced-card").evaluate((details) => { details.open = true; });
  await page.locator("#tags").fill(`e2e, cl-e2e-${String(index).padStart(2, "0")}, kvm-${String(index).padStart(3, "0")}`);
  const startOnCreate = index % 2 === 0;
  const started = page.locator("#started");
  if ((await started.isChecked()) !== startOnCreate) await started.click();

  let createRequest = null;
  const onRequest = (request) => {
    if (request.method() === "POST" && request.url().endsWith("/v1/instances")) createRequest = request;
  };
  page.on("request", onRequest);
  const responsePromise = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/v1/instances"));
  try {
    await page.locator("#create-button").click();
    const response = await responsePromise;
    if (!response.ok()) throw new E2EError(`docs create respondió HTTP ${response.status()}`);
    const created = await response.json();
    const requestBody = createRequest?.postDataJSON() || null;
    const idempotencyKey = createRequest?.headers()["idempotency-key"] || null;
    if (!requestBody || !idempotencyKey) throw new E2EError("la creación visual no expuso cuerpo o Idempotency-Key");
    if (requestBody.environment !== "lab") throw new E2EError(`Docs envió un entorno inesperado: ${requestBody.environment}`);
    if (requestBody.network_profile !== NETWORK_PROFILE) throw new E2EError(`Docs envió un perfil de red inesperado: ${requestBody.network_profile}`);
    const replay = await api("POST", "/v1/instances", requestBody, idempotencyKey);
    if (replay.instance_id !== created.instance_id || replay.operation_id !== created.operation_id) throw new E2EError("replay de idempotencia divergente");
    telemetry.uiRequests.push({ action: "create", idempotencyKey, operationId: created.operation_id });
    return { ...testCase, name, body: requestBody, created, password, key, startOnCreate };
  } finally {
    page.off("request", onRequest);
  }
}

async function cleanup(instanceId, label) {
  if (!instanceId) return "no-instance";
  try {
    const current = await api("GET", `/v1/instances/${encodeURIComponent(instanceId)}`);
    if (current.status === "deleted") return "already-deleted";
    try {
      if ((await getPower(instanceId)).power_state === "running") {
        const shutdown = await api("POST", `/v1/instances/${encodeURIComponent(instanceId)}/power/shutdown`, undefined, `${label}-cleanup-shutdown`);
        await waitOperation(shutdown.operation_id, instanceId, { liveObserved: false, workerSnapshots: [], observationErrors: [] });
        await waitPower(instanceId, "stopped");
      }
    } catch (error) {
      console.error(`CLEANUP ${label} shutdown: ${redact(error.message)}`);
    }
    const destroy = await api("DELETE", `/v1/instances/${encodeURIComponent(instanceId)}`, undefined, `${label}-cleanup-destroy`);
    await waitOperation(destroy.operation_id, instanceId, { liveObserved: false, workerSnapshots: [], observationErrors: [] });
    return "destroyed";
  } catch (error) {
    console.error(`CLEANUP ${label}: ${redact(error.message)}`);
    return "cleanup-failed";
  }
}

async function runCase(page, testCase) {
  const label = `docs-${RUN_ID}-${String(testCase.index).padStart(2, "0")}-${testCase.image}-${testCase.plan}-${testCase.mode}`;
  const telemetry = { liveObserved: false, workerSnapshots: [], observationErrors: [], uiRequests: [], ping: [] };
  let instanceId = null;
  let vmid = null;
  let deleted = false;
  try {
    const createdCase = await createFromDocs(page, testCase, telemetry);
    instanceId = createdCase.created.instance_id;
    await waitOperation(createdCase.created.operation_id, instanceId, telemetry);
    const current = await api("GET", `/v1/instances/${instanceId}`);
    vmid = Number(current.vmid);
    if (vmid < 30000 || vmid > 39999) throw new E2EError(`VMID fuera de rango para iac.cyberfuel.com: ${vmid}`);
    if (current.image_id !== testCase.image || current.plan_id !== testCase.plan) throw new E2EError("catálogo divergente");
    if (current.spec?.network_profile !== NETWORK_PROFILE) throw new E2EError(`perfil de red divergente: ${current.spec?.network_profile}`);
    if (JSON.stringify(current.spec?.tags) !== JSON.stringify(createdCase.body.tags)) throw new E2EError("tags divergentes en el detalle de la instancia");
    const pool = await proxmox(`/pools/${encodeURIComponent(POOL_ID)}`);
    const inPool = (pool.members || []).some((member) => String(member.vmid) === String(vmid));
    if (!inPool) throw new E2EError(`VMID ${vmid} no pertenece a ${POOL_ID}`);

    if (!createdCase.startOnCreate) {
      const start = await clickPower(page, instanceId, "start", telemetry);
      await waitOperation(start.operation_id, instanceId, telemetry);
    }
    await waitPower(instanceId, "running");
    await api("GET", `/v1/instances/${instanceId}`);
    await api("GET", `/v1/instances/${instanceId}/network`);
    await api("GET", `/v1/instances/${instanceId}/power`);
    await api("GET", `/v1/events?operation_id=${createdCase.created.operation_id}&sort=asc&limit=200`);
    const firstSsh = await waitSsh(instanceId, testCase.mode, createdCase.password, createdCase.key.privateKey);
    telemetry.ping.push(firstSsh.ping);

    const reboot = await clickPower(page, instanceId, "reboot", telemetry);
    await waitOperation(reboot.operation_id, instanceId, telemetry);
    await waitPower(instanceId, "running");
    const rebootSsh = await waitSsh(instanceId, testCase.mode, createdCase.password, createdCase.key.privateKey);
    telemetry.ping.push(rebootSsh.ping);

    const shutdown = await clickPower(page, instanceId, "shutdown", telemetry);
    await waitOperation(shutdown.operation_id, instanceId, telemetry);
    await waitPower(instanceId, "stopped");
    await api("GET", `/v1/instances/${instanceId}`);
    await api("GET", `/v1/instances/${instanceId}/power`);
    await api("GET", "/v1/workers");
    await api("GET", "/v1/observability");

    const destroy = await clickDestroy(page, instanceId, telemetry);
    await waitOperation(destroy.operation_id, instanceId, telemetry);
    deleted = true;
    const final = await api("GET", `/v1/instances/${instanceId}`);
    if (final.status !== "deleted") throw new E2EError(`destroy dejó estado ${final.status}`);
    const inventory = await proxmox("/cluster/resources?type=vm");
    if ((inventory || []).some((item) => Number(item.vmid) === vmid)) throw new E2EError(`VMID ${vmid} sigue presente en Proxmox`);
    await page.evaluate(() => { window.location.hash = "#workers"; });
    await page.waitForTimeout(500);
    const workersText = await page.locator("#workers-table").innerText();
    if (!workersText.includes("Ver última ejecución") && !workersText.includes("Ver ejecución")) throw new E2EError("la vista Workers no mostró el enlace de ejecución");
    console.log(`PASS ${label} vmid=${vmid} ip=${rebootSsh.ip} live=${telemetry.liveObserved} ping=${telemetry.ping.join(",")}`);
    return { label, status: "passed", vmid, ip: rebootSsh.ip, live: telemetry.liveObserved, ping: telemetry.ping };
  } catch (error) {
    const cleanupStatus = deleted ? "already-deleted" : await cleanup(instanceId, label);
    console.error(`FAIL ${label} vmid=${vmid || "-"} cleanup=${cleanupStatus}: ${redact(error.message)}`);
    return { label, status: "failed", vmid, error: redact(error.message), cleanup: cleanupStatus };
  }
}

function buildCases() {
  const cases = [];
  let index = 1;
  for (let imageIndex = 0; imageIndex < IMAGES.length; imageIndex += 1) {
    for (let planIndex = 0; planIndex < PLANS.length; planIndex += 1) {
      const mode = (imageIndex + planIndex) % 2 === 0 ? "password" : "key";
      cases.push({ index: index++, image: IMAGES[imageIndex], plan: PLANS[planIndex], mode });
    }
  }
  return cases;
}

async function main() {
  if (!Number.isInteger(MAX_WORKERS) || MAX_WORKERS < 1 || MAX_WORKERS > 2) throw new E2EError("IACP_E2E_MAX_WORKERS debe estar entre 1 y 2");
  const baseUrl = new URL(BASE_URL);
  if (baseUrl.protocol !== "https:" || baseUrl.hostname !== "iac.cyberfuel.com" || baseUrl.pathname) throw new E2EError("IACP_E2E_BASE_URL debe ser exactamente https://iac.cyberfuel.com");
  if (!API_TOKEN) throw new E2EError("IACP_E2E_API_TOKEN es obligatorio");
  if (!PROXMOX_ENDPOINT || !PROXMOX_TOKEN) throw new E2EError("faltan credenciales Proxmox para validar destrucción física");
  if (!PUBLIC_KEY) throw new E2EError("IACP_E2E_SSH_PUBLIC_KEY es obligatorio para la matriz de llaves");
  if (NETWORK_PROFILE !== "lab_dhcp") throw new E2EError("esta matriz debe ejecutarse con lab_dhcp");
  const plans = await api("GET", "/v1/plans");
  const images = await api("GET", "/v1/images");
  if (JSON.stringify((plans.plans || []).map((item) => item.plan_id)) !== JSON.stringify(PLANS)) throw new E2EError("catálogo de planes inesperado");
  if (JSON.stringify((images.images || []).map((item) => item.image_id)) !== JSON.stringify(IMAGES)) throw new E2EError("catálogo de imágenes inesperado");
  const environments = await api("GET", "/v1/environments");
  const logicalEnvironments = (environments.environments || []).map((item) => item.environment);
  if (environments.default_environment !== "lab" || JSON.stringify(logicalEnvironments) !== JSON.stringify(["lab"])) throw new E2EError("el único entorno lógico habilitado debe ser lab");
  const profiles = await api("GET", "/v1/network-profiles");
  const profile = (profiles.network_profiles || []).find((item) => item.profile_id === NETWORK_PROFILE);
  if (!profile?.enabled || profile.network_mode !== "dhcp") throw new E2EError("lab_dhcp no está disponible como DHCP");
  console.log(`CATALOG PASS plans=${PLANS.join(",")} images=${IMAGES.join(",")}`);
  console.log(`NETWORK PROFILE PASS profile=${NETWORK_PROFILE}`);

  const browser = await chromium.launch({
    headless: process.env.IACP_DOCS_HEADLESS !== "0",
    executablePath: process.env.IACP_BROWSER_EXECUTABLE || "/usr/bin/chromium",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({ ignoreHTTPSErrors: false });
  const pages = await Promise.all(Array.from({ length: MAX_WORKERS }, () => context.newPage()));
  for (const page of pages) {
    page.setDefaultTimeout(30000);
    await ensureAuthorized(page);
  }
  const results = [];
  const cases = buildCases();
  try {
    for (let offset = 0, wave = 1; offset < cases.length; offset += MAX_WORKERS, wave += 1) {
      const batch = cases.slice(offset, offset + MAX_WORKERS);
      console.log(`WAVE ${wave}/8 start=${batch.map((item) => `${item.image}/${item.plan}/${item.mode}`).join(",")}`);
      const waveResults = await Promise.all(batch.map((testCase, index) => runCase(pages[index], testCase)));
      results.push(...waveResults);
      console.log(`WAVE ${wave}/8 done failed=${waveResults.filter((item) => item.status !== "passed").length}`);
      if (waveResults.some((item) => item.status !== "passed")) {
        console.error(`ABORT after wave ${wave}: se detiene para reparar sin crear más VMs.`);
        break;
      }
    }
  } finally {
    await context.close();
    await browser.close();
  }
  const failed = results.filter((item) => item.status !== "passed");
  console.log(`SUMMARY channel=docs total=${results.length} passed=${results.length - failed.length} failed=${failed.length}`);
  if (failed.length) {
    for (const result of failed) console.log(JSON.stringify(result));
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(`DOCS MATRIX ABORTED: ${redact(error.message)}`);
  process.exitCode = 1;
});

