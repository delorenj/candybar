import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

const treeEl = document.getElementById("tree");
const logEl = document.getElementById("log");
const connEl = document.getElementById("conn");
const statsEl = document.getElementById("stats");
const allOn = document.getElementById("all-on");
const allOff = document.getElementById("all-off");
const testDingBtn = document.getElementById("test-ding");
const defaultVolumeEl = document.getElementById("default-volume");

let knownTypes = [];
let logPrefixes = new Set();
let dingPrefixes = new Set();
let mutedPrefixes = new Set();
let defaultVolume = 0.05;

let shownCount = 0;
let suppressedLogCount = 0;
let suppressedDingCount = 0;

let audioCtx = null;

function setStatus(msg) { connEl.textContent = msg; }

function updateStats() {
  statsEl.textContent = `${shownCount} shown • ${suppressedLogCount} log-suppressed • ${suppressedDingCount} ding-suppressed`;
}

function matchPrefix(type, set) {
  for (const p of set) {
    if (type === p || type.startsWith(p + ".")) return true;
  }
  return false;
}

function isMuted(type) {
  return matchPrefix(type, mutedPrefixes);
}

async function unlockAudio() {
  try {
    if (!audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      audioCtx = new Ctx();
    }
    if (audioCtx.state === "suspended") await audioCtx.resume();
    return audioCtx.state === "running";
  } catch {
    return false;
  }
}

async function ding(volume = defaultVolume) {
  const ok = await unlockAudio();
  if (!ok || !audioCtx) return;
  const o = audioCtx.createOscillator();
  const g = audioCtx.createGain();
  o.type = "square";
  o.frequency.value = 1200;
  g.gain.value = Math.max(0, Math.min(1, volume));
  o.connect(g).connect(audioCtx.destination);
  o.start();
  o.stop(audioCtx.currentTime + 0.06);
}

async function saveConfig() {
  try {
    await invoke("set_filters", {
      prefixes: Array.from(logPrefixes),
      log_prefixes: Array.from(logPrefixes),
      ding_prefixes: Array.from(dingPrefixes),
      muted_prefixes: Array.from(mutedPrefixes),
      default_volume: defaultVolume
    });
  } catch (e) {
    console.error("set_filters failed", e);
  }
}

function addRow(evt) {
  shownCount += 1;
  updateStats();
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = `<div class=\"meta\">${evt.time || ""} • ${evt.subject || ""}</div><div class=\"type\">${evt.type}</div>`;
  logEl.prepend(row);
  while (logEl.children.length > 500) logEl.removeChild(logEl.lastChild);
}

function node(prefix, title, children = []) {
  const l = logPrefixes.has(prefix) ? "checked" : "";
  const s = dingPrefixes.has(prefix) ? "checked" : "";
  const m = mutedPrefixes.has(prefix) ? "checked" : "";
  const controls = `
    <span class="small" style="margin-left:8px;white-space:nowrap;">
      <label><input type=\"checkbox\" data-mode=\"log\" data-prefix=\"${prefix}\" ${l}/>L</label>
      <label><input type=\"checkbox\" data-mode=\"ding\" data-prefix=\"${prefix}\" ${s}/>S</label>
      <label><input type=\"checkbox\" data-mode=\"mute\" data-prefix=\"${prefix}\" ${m}/>M</label>
    </span>`;

  if (!children.length) {
    return `<div><span>${title}</span>${controls}</div>`;
  }
  return `<details open><summary>${title}${controls}</summary>${children.join("")}</details>`;
}

function buildTree(types) {
  const root = {};
  for (const t of types) {
    const parts = t.split(".");
    let cur = root;
    let path = "";
    for (const part of parts) {
      path = path ? `${path}.${part}` : part;
      cur[part] = cur[part] || { __path: path, __children: {} };
      cur = cur[part].__children;
    }
  }

  const render = (obj) => Object.entries(obj)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `<div>${node(v.__path, k, render(v.__children))}</div>`);

  const html = render(root).join("");
  treeEl.innerHTML = html || '<div class="small">No schemas found. Set BLOODBANK_SCHEMA_ROOT if needed.</div>';

  treeEl.querySelectorAll("input[type=checkbox]").forEach((box) => {
    box.addEventListener("click", (ev) => ev.stopPropagation());
    box.addEventListener("change", async (e) => {
      const p = e.target.dataset.prefix;
      const mode = e.target.dataset.mode;
      if (!p || !mode) return;

      const targetSet = mode === "log" ? logPrefixes : mode === "ding" ? dingPrefixes : mutedPrefixes;
      if (e.target.checked) targetSet.add(p);
      else targetSet.delete(p);

      await saveConfig();
    });
  });
}

allOn.onclick = async () => {
  logPrefixes = new Set(["bloodbank.v1"]);
  dingPrefixes = new Set(["bloodbank.v1"]);
  mutedPrefixes = new Set();
  await saveConfig();
  buildTree(knownTypes);
};

allOff.onclick = async () => {
  logPrefixes = new Set();
  dingPrefixes = new Set();
  mutedPrefixes = new Set();
  await saveConfig();
  buildTree(knownTypes);
};

testDingBtn.onclick = async () => {
  await unlockAudio();
  await ding();
};

defaultVolumeEl.oninput = async (e) => {
  defaultVolume = Number(e.target.value || 0.05);
  await saveConfig();
};

document.addEventListener("pointerdown", () => {
  unlockAudio();
}, { once: true });

async function init() {
  try {
    setStatus("starting");
    const cfg = await invoke("get_filters");

    logPrefixes = new Set((cfg && (cfg.log_prefixes || cfg.logPrefixes)) || (cfg && cfg.prefixes) || ["bloodbank.v1"]);
    dingPrefixes = new Set((cfg && (cfg.ding_prefixes || cfg.dingPrefixes)) || (cfg && cfg.prefixes) || ["bloodbank.v1"]);
    mutedPrefixes = new Set((cfg && (cfg.muted_prefixes || cfg.mutedPrefixes)) || []);
    defaultVolume = Number((cfg && (cfg.default_volume ?? cfg.defaultVolume)) || 0.05);
    defaultVolumeEl.value = String(defaultVolume);

    knownTypes = await invoke("list_registered_event_types");
    buildTree(knownTypes);
    updateStats();

    await invoke("start_event_stream");

    await listen("bloodbank://status", (e) => {
      setStatus(e.payload.connected ? "connected" : "disconnected");
    });

    await listen("bloodbank://event", async (e) => {
      const evt = e.payload;

      const logAllowed = matchPrefix(evt.type, logPrefixes);
      const dingAllowed = matchPrefix(evt.type, dingPrefixes) && !isMuted(evt.type);

      if (logAllowed) addRow(evt);
      else {
        suppressedLogCount += 1;
      }

      if (dingAllowed) await ding();
      else suppressedDingCount += 1;

      updateStats();
    });

    setStatus("connected?");
  } catch (e) {
    console.error("init failed", e);
    setStatus("error");
    treeEl.innerHTML = `<div class="small">Init failed: ${String(e)}</div>`;
  }
}

init();
