import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

const MAX_FEED_ROWS = 400;
const MAX_CONTRACT_ISSUES = 200;
const BLOODBANK_PREFIX = "bloodbank.v1.";

const treeEl = document.getElementById("tree");
const logEl = document.getElementById("log");
const connEl = document.getElementById("conn");
const statsEl = document.getElementById("stats");
const allOnBtn = document.getElementById("all-on");
const allOffBtn = document.getElementById("all-off");
const testDingBtn = document.getElementById("test-ding");
const defaultVolumeEl = document.getElementById("default-volume");
const soundEnabledEl = document.getElementById("sound-enabled");
const schemaStatusEl = document.getElementById("schema-status");
const selectedPillEl = document.getElementById("selected-pill");
const streamPillEl = document.getElementById("stream-pill");

const kpiReceivedEl = document.getElementById("kpi-received");
const kpiVisibleEl = document.getElementById("kpi-visible");
const kpiFilteredEl = document.getElementById("kpi-filtered");
const kpiSelectedTypesEl = document.getElementById("kpi-selected-types");
const detailEl = document.getElementById("detail");

const contractSubjectKindEl = document.getElementById("contract-subject-kind");
const contractOrderingEl = document.getElementById("contract-ordering");
const contractSnakeEl = document.getElementById("contract-snake");
const contractTotalEl = document.getElementById("contract-total");
const contractIssuesEl = document.getElementById("contract-issues");

let knownTypes = [];
let schemaTypeCount = 0;
let selectedTypes = new Set();
let followAllTypes = true;

let feedEvents = [];
let totalReceived = 0;
let filteredCount = 0;
let soundSuppressedCount = 0;

let activeKind = "all";
let selectedEventId = null;
let activeInspectorTab = "investigate";

let soundEnabled = true;
let defaultVolume = 0.2;
let audioCtx = null;

let eventSeq = 0;
let issueSeq = 0;

const expandedNodes = new Set();
const contractCounters = {
  subjectKindMismatch: 0,
  missingOrderingKey: 0,
  snakeCaseAliases: 0,
};
let contractIssues = [];

function matchesPrefix(type, prefix) {
  return type === prefix || type.startsWith(`${prefix}.`);
}

function markerToKind(marker) {
  if (marker === "evt") return "event";
  if (marker === "cmd") return "command";
  if (marker === "rpy") return "reply";
  return null;
}

function kindFromSubject(subject) {
  if (!subject || typeof subject !== "string") return null;
  const parts = subject.split(".");
  if (parts.length < 2) return null;
  return markerToKind(parts[1]);
}

function normalizeKind(kind, subject) {
  if (kind === "event" || kind === "command" || kind === "reply") return kind;
  return kindFromSubject(subject) || "event";
}

function normalizeEvent(payload) {
  if (!payload || typeof payload !== "object") return null;
  const type = typeof payload.type === "string" ? payload.type : "";
  if (!type) return null;

  const subject = typeof payload.subject === "string" ? payload.subject : null;
  const kind = normalizeKind(payload.kind, subject);
  const time = typeof payload.time === "string" ? payload.time : new Date().toISOString();

  return {
    id: ++eventSeq,
    type,
    kind,
    subject,
    time,
    raw: payload.raw && typeof payload.raw === "object" ? payload.raw : payload,
  };
}

function sortTypes(values) {
  return Array.from(new Set(values)).sort((a, b) => a.localeCompare(b));
}

function setConnectionStatus(connected) {
  connEl.textContent = connected ? "connected" : "disconnected";
  connEl.classList.toggle("ok", connected);
  connEl.classList.toggle("bad", !connected);
}

function updateSchemaStatus() {
  if (schemaTypeCount > 0) {
    const liveOnlyCount = Math.max(0, knownTypes.length - schemaTypeCount);
    schemaStatusEl.textContent =
      liveOnlyCount > 0
        ? `${schemaTypeCount} schema types loaded + ${liveOnlyCount} live-only types.`
        : `${schemaTypeCount} schema types loaded.`;
  } else {
    schemaStatusEl.textContent =
      "Schema root not found; taxonomy is building from live stream types.";
  }
}

function updateStats() {
  const visibleCount = getVisibleEvents().length;
  statsEl.textContent = `${totalReceived} received • ${visibleCount} visible • ${filteredCount} type-filtered • ${soundSuppressedCount} sound-muted`;

  selectedPillEl.textContent = `${selectedTypes.size}/${knownTypes.length || 0} selected`;
  streamPillEl.textContent = `${visibleCount} in feed`;

  kpiReceivedEl.textContent = String(totalReceived);
  kpiVisibleEl.textContent = String(visibleCount);
  kpiFilteredEl.textContent = String(filteredCount);
  kpiSelectedTypesEl.textContent = String(selectedTypes.size);

  contractSubjectKindEl.textContent = String(contractCounters.subjectKindMismatch);
  contractOrderingEl.textContent = String(contractCounters.missingOrderingKey);
  contractSnakeEl.textContent = String(contractCounters.snakeCaseAliases);
  contractTotalEl.textContent = String(contractIssues.length);
}

function typeSegments(type) {
  if (type.startsWith(BLOODBANK_PREFIX)) {
    return type.slice(BLOODBANK_PREFIX.length).split(".").filter(Boolean);
  }
  return type.split(".").filter(Boolean);
}

function buildTreeModel(types) {
  const root = { id: "root", label: "root", children: new Map(), leafTypes: new Set() };

  for (const type of types) {
    const parts = typeSegments(type);
    root.leafTypes.add(type);

    let node = root;
    let path = "";
    for (const part of parts) {
      path = path ? `${path}.${part}` : part;
      if (!node.children.has(part)) {
        node.children.set(part, {
          id: path,
          label: part,
          children: new Map(),
          leafTypes: new Set(),
        });
      }
      node = node.children.get(part);
      node.leafTypes.add(type);
    }
  }

  return root;
}

function sortedChildren(node) {
  return Array.from(node.children.values()).sort((a, b) => a.label.localeCompare(b.label));
}

function countSelected(node) {
  let selectedCount = 0;
  for (const type of node.leafTypes) {
    if (selectedTypes.has(type)) selectedCount += 1;
  }
  return selectedCount;
}

function toggleNodeSelection(node, checked) {
  for (const type of node.leafTypes) {
    if (checked) selectedTypes.add(type);
    else selectedTypes.delete(type);
  }
  followAllTypes = knownTypes.length === 0 || selectedTypes.size === knownTypes.length;
}

async function onTreeSelectionChanged() {
  feedEvents = [];
  selectedEventId = null;
  renderTree();
  renderFeed();
  updateStats();
  await saveConfig();
}

function createNodeCheckbox(node, selectedCount, totalCount) {
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = totalCount > 0 && selectedCount === totalCount;
  checkbox.indeterminate = selectedCount > 0 && selectedCount < totalCount;

  checkbox.addEventListener("click", (event) => event.stopPropagation());
  checkbox.addEventListener("change", async (event) => {
    toggleNodeSelection(node, event.target.checked);
    await onTreeSelectionChanged();
  });

  return checkbox;
}

function renderTreeNode(node, depth) {
  const selectedCount = countSelected(node);
  const totalCount = node.leafTypes.size;
  const hasChildren = node.children.size > 0;

  const container = document.createElement("div");
  container.className = "tree-node";

  if (hasChildren) {
    const details = document.createElement("details");
    details.open = expandedNodes.has(node.id) || depth < 1;

    const summary = document.createElement("summary");
    summary.className = "tree-summary";

    summary.appendChild(createNodeCheckbox(node, selectedCount, totalCount));

    const label = document.createElement("span");
    label.className = "node-label";
    label.textContent = node.label;
    summary.appendChild(label);

    const count = document.createElement("span");
    count.className = "node-count";
    count.textContent = `${selectedCount}/${totalCount}`;
    summary.appendChild(count);

    details.appendChild(summary);

    const childrenWrap = document.createElement("div");
    childrenWrap.className = "tree-children";
    for (const child of sortedChildren(node)) {
      childrenWrap.appendChild(renderTreeNode(child, depth + 1));
    }
    details.appendChild(childrenWrap);

    details.addEventListener("toggle", () => {
      if (details.open) expandedNodes.add(node.id);
      else expandedNodes.delete(node.id);
    });

    container.appendChild(details);
    return container;
  }

  const row = document.createElement("label");
  row.className = "leaf-row";

  row.appendChild(createNodeCheckbox(node, selectedCount, totalCount));

  const label = document.createElement("span");
  label.className = "node-label";
  label.textContent = node.label;
  row.appendChild(label);

  const count = document.createElement("span");
  count.className = "node-count";
  count.textContent = selectedCount === totalCount ? "on" : "off";
  row.appendChild(count);

  container.appendChild(row);
  return container;
}

function renderTree() {
  treeEl.textContent = "";
  if (!knownTypes.length) {
    treeEl.innerHTML = '<div class="small">No event types discovered yet.</div>';
    return;
  }

  const root = buildTreeModel(knownTypes);
  for (const child of sortedChildren(root)) {
    treeEl.appendChild(renderTreeNode(child, 0));
  }
}

function getVisibleEvents() {
  if (activeKind === "all") return feedEvents.slice();
  return feedEvents.filter((event) => event.kind === activeKind);
}

function findEventById(eventId) {
  if (!eventId) return null;
  for (let i = feedEvents.length - 1; i >= 0; i -= 1) {
    if (feedEvents[i].id === eventId) return feedEvents[i];
  }
  return null;
}

function renderInvestigatePanel() {
  const selectedEvent = findEventById(selectedEventId);
  if (!selectedEvent) {
    detailEl.textContent = "No event selected.";
    return;
  }

  detailEl.textContent = JSON.stringify(selectedEvent.raw, null, 2);
}

function renderContractIssues() {
  contractIssuesEl.textContent = "";
  if (!contractIssues.length) {
    contractIssuesEl.innerHTML = '<div class="small">No contract issues observed in this session.</div>';
    return;
  }

  for (const issue of contractIssues.slice().reverse().slice(0, 60)) {
    const row = document.createElement("div");
    row.className = `issue ${issue.severity}`;

    const title = document.createElement("div");
    title.className = "title";
    title.textContent = issue.title;

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${issue.time} • ${issue.type}`;

    row.appendChild(title);
    row.appendChild(meta);
    contractIssuesEl.appendChild(row);
  }
}

function renderFeed() {
  const visible = getVisibleEvents().slice().reverse();

  if (selectedEventId && !visible.some((event) => event.id === selectedEventId)) {
    selectedEventId = visible.length ? visible[0].id : null;
  }
  if (!selectedEventId && visible.length) {
    selectedEventId = visible[0].id;
  }

  logEl.textContent = "";
  for (const event of visible) {
    const row = document.createElement("div");
    row.className = "row";
    if (event.id === selectedEventId) row.classList.add("active");

    const body = document.createElement("div");

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${event.time} • ${event.subject || "(no subject)"}`;

    const type = document.createElement("div");
    type.className = "type";
    type.textContent = event.type;

    body.appendChild(meta);
    body.appendChild(type);

    const kind = document.createElement("div");
    kind.className = `kind ${event.kind}`;
    kind.textContent = event.kind;

    row.appendChild(body);
    row.appendChild(kind);

    row.addEventListener("click", () => {
      selectedEventId = event.id;
      renderFeed();
    });

    logEl.appendChild(row);
  }

  renderInvestigatePanel();
  renderContractIssues();
  updateStats();
}

function addKnownType(type) {
  if (!type || knownTypes.includes(type)) return false;
  knownTypes.push(type);
  knownTypes.sort((a, b) => a.localeCompare(b));
  return true;
}

function hasSnakeCaseAliases(raw) {
  if (!raw || typeof raw !== "object") return false;
  return Object.prototype.hasOwnProperty.call(raw, "correlation_id") ||
    Object.prototype.hasOwnProperty.call(raw, "causation_id");
}

function appendContractIssue(severity, title, event) {
  contractIssues.push({
    id: ++issueSeq,
    severity,
    title,
    type: event.type,
    time: event.time,
  });

  if (contractIssues.length > MAX_CONTRACT_ISSUES) {
    contractIssues = contractIssues.slice(contractIssues.length - MAX_CONTRACT_ISSUES);
  }
}

function inspectContract(event) {
  const subjectKind = kindFromSubject(event.subject);
  if (subjectKind && subjectKind !== event.kind) {
    contractCounters.subjectKindMismatch += 1;
    appendContractIssue(
      "high",
      `subject marker implies ${subjectKind} but envelope kind is ${event.kind}`,
      event
    );
  }

  if (event.kind === "event") {
    const orderingKey = event.raw?.ordering_key;
    if (typeof orderingKey !== "string" || orderingKey.trim().length === 0) {
      contractCounters.missingOrderingKey += 1;
      appendContractIssue("medium", "missing ordering_key on kind=event", event);
    }
  }

  if (hasSnakeCaseAliases(event.raw)) {
    contractCounters.snakeCaseAliases += 1;
    appendContractIssue(
      "low",
      "snake_case alias field detected (correlation_id or causation_id)",
      event
    );
  }
}

function isSelectedForNotifications(event) {
  return selectedTypes.has(event.type);
}

function clampVolume(value) {
  return Math.max(0, Math.min(1, Number(value ?? defaultVolume)));
}

function createFallbackDingDataUrl() {
  const sampleRate = 44100;
  const durationSec = 0.14;
  const freq = 1046.5;
  const sampleCount = Math.floor(sampleRate * durationSec);
  const pcmDataBytes = sampleCount * 2;
  const buffer = new ArrayBuffer(44 + pcmDataBytes);
  const view = new DataView(buffer);

  const writeString = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) {
      view.setUint8(offset + i, text.charCodeAt(i));
    }
  };

  writeString(0, "RIFF");
  view.setUint32(4, 36 + pcmDataBytes, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, pcmDataBytes, true);

  for (let i = 0; i < sampleCount; i += 1) {
    const t = i / sampleRate;
    const envelope = Math.exp(-18 * t);
    const sample = Math.sin(2 * Math.PI * freq * t) * envelope;
    const int16 = Math.max(-1, Math.min(1, sample)) * 32767;
    view.setInt16(44 + i * 2, int16, true);
  }

  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }

  return `data:audio/wav;base64,${btoa(binary)}`;
}

async function playFallbackDing(volume = defaultVolume) {
  const audio = new Audio(createFallbackDingDataUrl());
  audio.volume = clampVolume(volume);
  await audio.play();
}

async function unlockAudio() {
  try {
    if (!audioCtx) {
      const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextCtor) return false;
      audioCtx = new AudioContextCtor();
    }
    if (audioCtx.state === "suspended") await audioCtx.resume();
    return audioCtx.state === "running";
  } catch (error) {
    console.error("WebAudio unlock failed", error);
    return false;
  }
}

async function ding(volume = defaultVolume) {
  const normalizedVolume = clampVolume(volume);

  try {
    const ready = await unlockAudio();
    if (ready && audioCtx) {
      const now = audioCtx.currentTime;
      const oscillator = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      oscillator.type = "triangle";
      oscillator.frequency.setValueAtTime(1046.5, now);
      oscillator.frequency.exponentialRampToValueAtTime(880, now + 0.14);
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(Math.max(0.0001, normalizedVolume), now + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.14);
      oscillator.connect(gain).connect(audioCtx.destination);
      oscillator.start(now);
      oscillator.stop(now + 0.14);
      return;
    }

    console.error("WebAudio unavailable for ding; using fallback");
  } catch (error) {
    console.error("WebAudio ding failed", error);
  }

  try {
    await playFallbackDing(normalizedVolume);
  } catch (error) {
    console.error("Fallback ding failed", error);
  }
}

async function saveConfig() {
  try {
    await invoke("set_filters", {
      selected_types: Array.from(selectedTypes),
      sound_enabled: soundEnabled,
      default_volume: defaultVolume,
    });
  } catch (error) {
    console.error("set_filters failed", error);
  }
}

function applyKindFilter(kind) {
  activeKind = kind;
  document.querySelectorAll("#kind-filters button").forEach((button) => {
    button.classList.toggle("active", button.dataset.kind === activeKind);
  });
  renderFeed();
}

function applyInspectorTab(tab) {
  activeInspectorTab = tab;
  document.querySelectorAll("#right-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });

  document.getElementById("tab-investigate").classList.toggle("hidden", tab !== "investigate");
  document.getElementById("tab-contract").classList.toggle("hidden", tab !== "contract");
}

function deriveSelectionFromLegacy(config, types) {
  const legacyPrefixes =
    (Array.isArray(config?.log_prefixes) && config.log_prefixes) ||
    (Array.isArray(config?.prefixes) && config.prefixes) ||
    [];

  if (!legacyPrefixes.length) {
    return new Set(types);
  }

  if (legacyPrefixes.some((prefix) => prefix === "bloodbank.v1" || prefix === "bloodbank")) {
    return new Set(types);
  }

  const derived = new Set();
  for (const type of types) {
    if (legacyPrefixes.some((prefix) => matchesPrefix(type, prefix))) {
      derived.add(type);
    }
  }

  return derived;
}

allOnBtn.addEventListener("click", async () => {
  selectedTypes = new Set(knownTypes);
  followAllTypes = true;
  feedEvents = [];
  selectedEventId = null;
  renderTree();
  renderFeed();
  updateStats();
  await saveConfig();
});

allOffBtn.addEventListener("click", async () => {
  selectedTypes = new Set();
  followAllTypes = false;
  feedEvents = [];
  selectedEventId = null;
  renderTree();
  renderFeed();
  updateStats();
  await saveConfig();
});

testDingBtn.addEventListener("click", async () => {
  await ding();
});

soundEnabledEl.addEventListener("change", async (event) => {
  soundEnabled = Boolean(event.target.checked);
  await saveConfig();
});

defaultVolumeEl.addEventListener("input", (event) => {
  defaultVolume = Number(event.target.value || 0.2);
});

defaultVolumeEl.addEventListener("change", async (event) => {
  defaultVolume = Number(event.target.value || 0.2);
  await saveConfig();
});

document.querySelectorAll("#kind-filters button").forEach((button) => {
  button.addEventListener("click", () => {
    applyKindFilter(button.dataset.kind || "all");
  });
});

document.querySelectorAll("#right-tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    applyInspectorTab(button.dataset.tab || "investigate");
  });
});

document.addEventListener(
  "pointerdown",
  () => {
    unlockAudio();
  },
  { once: true }
);

async function init() {
  try {
    setConnectionStatus(false);

    const config = await invoke("get_filters");
    soundEnabled = config?.sound_enabled ?? true;
    defaultVolume = Number(config?.default_volume ?? 0.2);
    defaultVolume = Math.max(0, Math.min(1, defaultVolume));
    soundEnabledEl.checked = soundEnabled;
    defaultVolumeEl.value = String(defaultVolume);

    const schemaTypes = await invoke("list_registered_event_types");
    knownTypes = sortTypes(Array.isArray(schemaTypes) ? schemaTypes : []);
    schemaTypeCount = knownTypes.length;

    if (Array.isArray(config?.selected_types) && config.selected_types.length > 0) {
      selectedTypes = new Set(config.selected_types);
      if (knownTypes.length > 0) {
        selectedTypes = new Set(Array.from(selectedTypes).filter((type) => knownTypes.includes(type)));
      }
      followAllTypes = knownTypes.length === 0 || knownTypes.every((type) => selectedTypes.has(type));
    } else {
      selectedTypes = deriveSelectionFromLegacy(config, knownTypes);
      followAllTypes = true;
    }

    if (knownTypes.length > 0 && selectedTypes.size === 0) {
      selectedTypes = new Set(knownTypes);
      followAllTypes = true;
    }

    updateSchemaStatus();
    renderTree();
    renderFeed();
    applyInspectorTab(activeInspectorTab);

    await saveConfig();
    await invoke("start_event_stream");

    await listen("bloodbank://status", (event) => {
      setConnectionStatus(Boolean(event.payload?.connected));
    });

    await listen("bloodbank://event", async (event) => {
      const normalized = normalizeEvent(event.payload);
      if (!normalized) return;

      const knownTypeCountBefore = knownTypes.length;
      const shouldAutoSelectNewType = followAllTypes;
      const addedType = addKnownType(normalized.type);
      if (addedType) {
        if (shouldAutoSelectNewType || knownTypeCountBefore === 0) {
          selectedTypes.add(normalized.type);
        }
        followAllTypes = knownTypes.length === 0 || selectedTypes.size === knownTypes.length;
        renderTree();
        updateSchemaStatus();
      }

      totalReceived += 1;
      inspectContract(normalized);

      const selected = isSelectedForNotifications(normalized);
      if (!selected) {
        filteredCount += 1;
      } else {
        feedEvents.push(normalized);
        if (feedEvents.length > MAX_FEED_ROWS) {
          const dropped = feedEvents.shift();
          if (dropped && dropped.id === selectedEventId) selectedEventId = null;
        }

        if (soundEnabled) await ding();
        else soundSuppressedCount += 1;
      }

      renderFeed();

      if (addedType) {
        await saveConfig();
      }
    });
  } catch (error) {
    console.error("init failed", error);
    setConnectionStatus(false);
    treeEl.innerHTML = `<div class="small">Init failed: ${String(error)}</div>`;
  }
}

init();
