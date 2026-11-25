"use strict";
const $ = (id) => document.getElementById(id);
const axisNames = ["leftx", "lefty", "rightx", "righty", "lefttrigger", "righttrigger"];
const state = { buttons: [], axes: Object.fromEntries(axisNames.map((key) => [key, 0])) };
const pressed = new Map();
const inputEdges = [];
const stickOwners = new Map();
let invite = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
let token = sessionStorage.getItem("emuatlas-controller-token");
let sequence = Number(sessionStorage.getItem("emuatlas-controller-sequence") || 0);
let pending = false;
let dirty = true;
let connected = false;
let stopped = false;
let lastSent = 0;
let wakeLock;
let requestedPlayer = null;
let failures = 0;
let nextRetry = 0;

async function api(action, value) {
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 1500);
  try {
    const response = await fetch(`/api/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token || ""}` },
      body: JSON.stringify(value), signal: abort.signal, cache: "no-store"
    });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || "Não foi possível conectar.");
      error.status = response.status;
      throw error;
    }
    return result;
  } finally { clearTimeout(timeout); }
}

function status(text, online) {
  $("connection").textContent = text;
  $("connection").dataset.online = String(online);
}

function showController(result) {
  connected = true;
  $("pairing").hidden = true;
  $("gamepad").hidden = false;
  $("controller-name").textContent = result.name;
  if ($("player").options.length !== result.capacity + 1) {
    $("player").replaceChildren(new Option("Aguardando", ""));
    for (let n = 0; n < result.capacity; n++) $("player").add(new Option(`P${n + 1}`, String(n)));
  }
  $("player").value = result.player === null ? "" : String(result.player);
  if (result.player === requestedPlayer || result.error) requestedPlayer = null;
  $("game-status").textContent = result.error || (requestedPlayer !== null ? `Solicitando P${requestedPlayer + 1}…` : result.enabled ? "" : "Aguardando o jogo ou frontend ficar em foco.");
  status(result.player === null ? "Conectado · aguardando jogador" : `Conectado · P${result.player + 1}`, true);
}

function resetInputs() {
  pressed.clear();
  inputEdges.length = 0;
  stickOwners.clear();
  state.buttons = [];
  for (const key of axisNames) state.axes[key] = 0;
  document.querySelectorAll(".pressed").forEach((button) => button.classList.remove("pressed"));
  document.querySelectorAll(".stick span").forEach((knob) => { knob.style.transform = ""; });
  dirty = true;
}

function beaconRelease() {
  resetInputs();
  if (token) {
    // fetch keeps bearer authentication while the page is being backgrounded.
    sequence++;
    sessionStorage.setItem("emuatlas-controller-sequence", String(sequence));
    fetch("/api/state", { method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
      body: JSON.stringify({ ...state, sequence }) }).catch(() => {});
  }
}

async function sendState() {
  if (!token || stopped || pending || document.hidden || Date.now() < nextRetry) return;
  const now = Date.now();
  if ((!dirty && !inputEdges.length && now - lastSent < 250) || now - lastSent < 33) return;
  pending = true;
  const connectionToken = token;
  dirty = false;
  lastSent = now;
  sequence++;
  sessionStorage.setItem("emuatlas-controller-sequence", String(sequence));
  try {
    const snapshot = inputEdges.shift() || { buttons: [...state.buttons], axes: { ...state.axes } };
    const result = await api("state", { ...snapshot, sequence });
    if (stopped || token !== connectionToken) return;
    failures = 0;
    nextRetry = 0;
    showController(result);
  } catch (error) {
    if (stopped || token !== connectionToken) return;
    resetInputs();
    connected = false;
    failures++;
    nextRetry = Date.now() + Math.min(3000, 250 * 2 ** Math.min(failures - 1, 4));
    status("Reconectando…", false);
    $("game-status").textContent = "Verifique a rede. Se a sessão terminou, escaneie um novo QR Code.";
    if (error.status === 403) {
      token = null;
      sessionStorage.removeItem("emuatlas-controller-token");
      connected = false;
      $("pairing").hidden = false;
      $("gamepad").hidden = true;
      $("pair-error").textContent = error.message;
    }
  } finally { pending = false; }
}

function updateButtons() {
  const active = new Set(pressed.values());
  state.buttons = [...active].filter((key) => !key.endsWith("trigger"));
  state.axes.lefttrigger = active.has("lefttrigger") ? 1 : 0;
  state.axes.righttrigger = active.has("righttrigger") ? 1 : 0;
  document.querySelectorAll("[data-button]").forEach((button) => button.classList.toggle("pressed", active.has(button.dataset.button)));
  // Preserve both edges of a short tap while another snapshot is in flight.
  if (inputEdges.length >= 32) {
    beaconRelease();
    status("Conexão lenta · solte os comandos", false);
    return;
  }
  inputEdges.push({ buttons: [...state.buttons], axes: { ...state.axes } });
  dirty = true;
  sendState();
}

document.querySelectorAll("[data-button]").forEach((button) => {
  button.addEventListener("pointerdown", (event) => {
    if (!connected) return;
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    pressed.set(event.pointerId, button.dataset.button);
    updateButtons();
  });
  const release = (event) => {
    if (pressed.delete(event.pointerId)) updateButtons();
  };
  button.addEventListener("pointerup", release);
  button.addEventListener("pointercancel", release);
  button.addEventListener("lostpointercapture", release);
  // Keyboard activation remains available to switch users and desktop browsers.
  button.addEventListener("keydown", (event) => {
    if (!connected) return;
    if ((event.key === " " || event.key === "Enter") && !event.repeat) {
      event.preventDefault(); pressed.set(`key-${button.dataset.button}`, button.dataset.button); updateButtons();
    }
  });
  button.addEventListener("keyup", (event) => {
    if (event.key === " " || event.key === "Enter") { pressed.delete(`key-${button.dataset.button}`); updateButtons(); }
  });
});

document.querySelectorAll("[data-stick]").forEach((stick) => {
  const side = stick.dataset.stick;
  const knob = stick.querySelector("span");
  const move = (event) => {
    if (stickOwners.get(side) !== event.pointerId) return;
    const box = stick.getBoundingClientRect();
    const radius = box.width * .4;
    let x = (event.clientX - box.left - box.width / 2) / radius;
    let y = (event.clientY - box.top - box.height / 2) / radius;
    const length = Math.hypot(x, y);
    if (length > 1) { x /= length; y /= length; }
    state.axes[`${side}x`] = Math.abs(x) < .08 ? 0 : x;
    state.axes[`${side}y`] = Math.abs(y) < .08 ? 0 : y;
    knob.style.transform = `translate(${x * radius * .55}px, ${y * radius * .55}px)`;
    dirty = true;
    sendState();
  };
  stick.addEventListener("pointerdown", (event) => {
    if (!connected || stickOwners.has(side)) return;
    event.preventDefault(); stickOwners.set(side, event.pointerId); stick.setPointerCapture(event.pointerId); move(event);
  });
  stick.addEventListener("pointermove", move);
  const release = (event) => {
    if (stickOwners.get(side) !== event.pointerId) return;
    stickOwners.delete(side);
    state.axes[`${side}x`] = state.axes[`${side}y`] = 0;
    knob.style.transform = ""; dirty = true; sendState();
  };
  stick.addEventListener("pointerup", release);
  stick.addEventListener("pointercancel", release);
  stick.addEventListener("lostpointercapture", release);
});

$("join-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("join").disabled = true;
  $("pair-error").textContent = "";
  try {
    const result = await api("join", { invite, name: $("device-name").value.trim() });
    token = result.token; sequence = 0;
    failures = 0; nextRetry = 0;
    sessionStorage.setItem("emuatlas-controller-token", token);
    sessionStorage.setItem("emuatlas-controller-sequence", "0");
    stopped = false; resetInputs(); showController(result); sendState();
    if (navigator.wakeLock) wakeLock = await navigator.wakeLock.request("screen").catch(() => undefined);
  } catch (error) { $("pair-error").textContent = error.message; }
  finally { $("join").disabled = false; }
});

$("player").addEventListener("change", async () => {
  if ($("player").value === "") return;
  resetInputs(); beaconRelease();
  try {
    requestedPlayer = Number($("player").value);
    await api("player", { player: requestedPlayer });
    $("game-status").textContent = "Solicitando a troca de jogador…";
  } catch (error) { requestedPlayer = null; $("game-status").textContent = error.message; }
});
$("fullscreen").addEventListener("click", async () => {
  if (document.fullscreenElement) await document.exitFullscreen();
  else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen().catch(() => {});
});
$("disconnect").addEventListener("click", async () => {
  beaconRelease(); stopped = true;
  try { await api("leave", {}); } catch (_) {}
  token = null; connected = false;
  sessionStorage.removeItem("emuatlas-controller-token");
  $("pairing").hidden = false; $("gamepad").hidden = true;
  $("pair-error").textContent = "Controle desconectado. Escaneie um QR Code para entrar novamente.";
  status("Desconectado", false);
  if (wakeLock) await wakeLock.release();
});
window.addEventListener("blur", beaconRelease);
window.addEventListener("pagehide", beaconRelease);
document.addEventListener("visibilitychange", () => { beaconRelease(); if (!document.hidden) sendState(); });
document.addEventListener("contextmenu", (event) => { if (event.target.closest(".controller")) event.preventDefault(); });
setInterval(sendState, 33);
$("fullscreen").hidden = !document.documentElement.requestFullscreen;
if (token) sendState();
else if (!invite) $("pair-error").textContent = "Abra Controles → Adicionar celular no EmuAtlas e escaneie o QR Code.";
