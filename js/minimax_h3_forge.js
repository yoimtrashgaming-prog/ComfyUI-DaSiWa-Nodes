// MiniMax H3 Forge: the overlay behind the Director's "Forge" button.
//
// Write an idea, pick a local model, get a prompt in the Director's own
// fields. Runs through /dasiwa/h3/forge (nodes/h3_forge.py), outside the
// ComfyUI queue: the LLM writes, unloads, and only then is the workflow run.
// The Director exposes node.__dasiwaH3Forge for reading the timeline and
// writing the result back.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Server addresses live in ComfyUI Settings, never in the workflow, so a
// downloaded workflow cannot point this machine at a server of its choosing.
const SETTING_OLLAMA = "DaSiWa.H3Forge.OllamaURL";
const SETTING_OPENAI = "DaSiWa.H3Forge.OpenAIURL";
const SETTING_OPENAI_KEY = "DaSiWa.H3Forge.OpenAIKey";
app.registerExtension({
  name: "DaSiWa.H3Forge",
  settings: [
    { id: SETTING_OLLAMA, category: ["DaSiWa", "H3 Forge", "Ollama address"], name: "Ollama address", type: "text", defaultValue: "", tooltip: "Leave empty for Ollama on this computer (http://127.0.0.1:11434). Set it to use Ollama on another machine." },
    { id: SETTING_OPENAI, category: ["DaSiWa", "H3 Forge", "OpenAI-compatible server"], name: "OpenAI-compatible server address", type: "text", defaultValue: "", tooltip: "Optional: a llama.cpp server, llama-swap, LM Studio or koboldcpp, e.g. http://127.0.0.1:8080. Empty = off." },
    { id: SETTING_OPENAI_KEY, category: ["DaSiWa", "H3 Forge", "OpenAI-compatible API key"], name: "OpenAI-compatible API key", type: "text", defaultValue: "", tooltip: "Only if that server asks for one (llama-server --api-key, llama-swap apiKeys, LM Studio with authentication). Sent only to the address above. Stored in ComfyUI's settings file like every other setting." },
  ],
});
function settingValue(id) {
  try { return app.extensionManager?.setting?.get(id) ?? app.ui?.settings?.getSettingValue(id) ?? ""; } catch { return ""; }
}
const forgeSettings = () => ({ ollama_url: settingValue(SETTING_OLLAMA) || "", openai_url: settingValue(SETTING_OPENAI) || "", openai_api_key: settingValue(SETTING_OPENAI_KEY) || "" });
const SOURCE_NAME = { local: "ComfyUI models/llm (loads inside ComfyUI)", ollama: "Ollama", openai: "OpenAI-compatible server" };
const NO_MODELS = "No models found. Easiest fix: put a vision model folder (for example Qwen3-VL-8B-Instruct from Hugging Face) in ComfyUI/models/llm and reopen Forge. Or install Ollama and run: ollama pull qwen3-vl:8b. Other servers: Settings > DaSiWa > H3 Forge.";

const STORE_KEY = "dasiwa.h3forge";
const openDialogs = new WeakMap();
const briefs = new Map(); // node id -> last brief, for a reroll after closing
const HISTORY_KEY = "dasiwaH3ForgeHistory";
const GROUPS_KEY = "dasiwaH3ForgeSubjectGroups";
// REF2VA picture labels, saved on the node by timeline item id. Subject
// groups (GROUPS_KEY) are still read, to label pictures saved before labels.
const EASY_ROLES_KEY = "dasiwaH3ForgeEasyRoles";
// A picture's label is picked in two steps: what it is, then - for characters
// only - which one, or which ones left to right. The saved value is one string
// ("character-2", "group-21", "place"), which is what the server reads.
const PICTURE_KINDS = [["character", "Character"], ["group", "Several characters"], ["place", "Place"], ["style", "Style"], ["first-frame", "First frame"], ["last-frame", "Last frame"]];
const PICTURE_WHO = {
  character: [["character-1", "Character 1"], ["character-2", "Character 2"], ["character-3", "Character 3"], ["character-4", "Character 4"]],
  group: [["group-12", "1 + 2 (1 on the left)"], ["group-21", "2 + 1 (2 on the left)"], ["group-13", "1 + 3 (1 on the left)"], ["group-31", "3 + 1 (3 on the left)"],
    ["group-23", "2 + 3 (2 on the left)"], ["group-32", "3 + 2 (3 on the left)"], ["group-123", "1 + 2 + 3 (left to right)"]],
};
const pictureKind = label => label.startsWith("character-") ? "character" : label.startsWith("group-") ? "group" : label;
function forgeHistory(node) {
  const saved = node.properties?.[HISTORY_KEY];
  return Array.isArray(saved) ? saved.filter(entry => entry && typeof entry.simple_prompt === "string" && entry.simple_prompt.trim() && typeof entry.mode === "string" && entry.fields && typeof entry.fields === "object").slice(0, 3) : [];
}
function saveForgeResult(node, result, brief) {
  const entry = {
    mode: result.mode, model: result.model, simple_prompt: result.simple_prompt,
    draftOptions: result.draftOptions, fields: result.fields, continuity: !!result.continuity, contextKey: result.contextKey, brief, createdAt: Date.now(),
  };
  node.properties ||= {};
  node.properties[HISTORY_KEY] = [entry, ...forgeHistory(node)].slice(0, 3);
  node.graph?.setDirtyCanvas(true, true);
  node.__dasiwaH3Render?.(); // the Director Clear button must enable even when only drafts exist
  return entry;
}

function clearForgeHistory(node) {
  if (node.properties && HISTORY_KEY in node.properties) {
    delete node.properties[HISTORY_KEY];
    node.graph?.setDirtyCanvas(true, true);
  }
  briefs.delete(node.id);
}

function remembered() { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch { return {}; } }
function remember(patch) { try { localStorage.setItem(STORE_KEY, JSON.stringify({ ...remembered(), ...patch })); } catch { /* private window */ } }

function installStyles() {
  if (document.getElementById("ds-h3-forge-styles")) return;
  const style = document.createElement("style");
  style.id = "ds-h3-forge-styles";
  style.textContent = `
  .ds-h3-modebar .ds-h3-forge-btn,.ds-h3-forge-btn{background:rgba(151,91,255,.14)!important;color:#e6d9ff!important;border-color:rgba(177,128,255,.7)!important}
  .ds-h3-modebar .ds-h3-forge-btn:hover,.ds-h3-forge-btn:hover{box-shadow:0 0 10px rgba(151,91,255,.6)}
  .ds-forge-overlay{position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
  .ds-forge{width:min(760px,94vw);max-height:90vh;overflow:auto;background:#111820;color:#e5eef4;border:1px solid #40515e;border-radius:8px;padding:14px;font:13px system-ui,sans-serif;display:flex;flex-direction:column;gap:10px;box-shadow:0 10px 40px rgba(0,0,0,.6)}
  .ds-forge h3{margin:0;font-size:15px;display:flex;justify-content:space-between;align-items:center}
  .ds-forge label{color:#9fb3c2;font-weight:600;font-size:12px}
  .ds-forge textarea,.ds-forge select,.ds-forge input[type=text]{width:100%;box-sizing:border-box;background:#0d1217;color:#e5eef4;border:1px solid #40515e;border-radius:4px;padding:7px;font:inherit}
  .ds-forge textarea{min-height:90px;resize:vertical}
  .ds-forge .row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .ds-forge .field{display:flex;flex-direction:column;gap:4px}
  .ds-forge input[type=range]{width:100%}
  .ds-forge button{background:#202b35;color:#dbe7f0;border:1px solid #40515e;border-radius:4px;padding:6px 12px;cursor:pointer;font:inherit}
  .ds-forge button:hover{background:#2c3c49}
  .ds-forge button.primary{background:rgba(151,91,255,.3);border-color:rgba(177,128,255,.8);color:#fff;font-weight:600}
  .ds-forge button:disabled{opacity:.45;cursor:default}
  .ds-forge .actions{display:flex;gap:8px;justify-content:flex-end;align-items:center}
  .ds-forge .status{flex:1;color:#f3c67a;min-height:16px}
  .ds-forge .status.error{color:#ff8a8a}
  .ds-forge .muted{color:#8fa3b2;font-size:12px}
  .ds-forge .refs{display:flex;flex-direction:column;gap:6px}
  .ds-forge .ref{display:grid;grid-template-columns:48px 80px minmax(130px,auto) 1fr;gap:8px;align-items:center}
  .ds-forge .pick{display:flex;gap:4px}
  .ds-forge .pick select{width:auto}
  .ds-forge .ref img{width:48px;height:36px;object-fit:cover;border-radius:3px;background:#090d11}
  .ds-forge pre{white-space:pre-wrap;background:#0b1015;border:1px solid #344452;border-radius:4px;padding:8px;margin:0;max-height:320px;overflow:auto;font:12px/1.45 ui-monospace,monospace}
  .ds-forge .history{display:flex;flex-direction:column;gap:5px;border-top:1px solid #344452;padding-top:9px}
  .ds-forge .history-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .ds-forge .history-head button{padding:3px 8px;font-size:11px}
  .ds-forge .history button{text-align:left;display:flex;flex-direction:column;gap:3px;min-width:0}
  .ds-forge .history button.selected{border-color:#b180ff;background:rgba(151,91,255,.18)}
  .ds-forge .history .excerpt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#9fb3c2;font-size:11px}
  `;
  document.head.append(style);
}

const el = (tag, props = {}, ...children) => { const n = Object.assign(document.createElement(tag), props); n.append(...children); return n; };
const viewUrl = path => api.apiURL(`/view?filename=${encodeURIComponent(path)}&type=input`);
const BASE_ROLE = { I2VA: "first frame", FL2VA: "first / last frame", L2VA: "last frame" };

// What each REF2VA picture is, by item id: the label the person picked, or one
// worked out for a picture never labelled. Subject groups saved before labels
// existed carry over (Group A -> one Character number), and an ungrouped
// picture gets the next free number, which is what "Separate" meant.
function pictureLabels(images, node) {
  const saved = node.properties?.[EASY_ROLES_KEY] || {};
  const groups = node.properties?.[GROUPS_KEY] || {};
  const used = new Set(Object.values(saved).flatMap(v => v.startsWith("character-") ? [Number(v.slice(10))] : v.startsWith("group-") ? [...v.slice(6)].map(Number) : []));
  const byGroup = new Map();
  const take = () => { let n = 1; while (used.has(n) && n < 4) n += 1; used.add(n); return n; };
  const labels = {};
  for (const item of images) {
    if (saved[item.id]) { labels[item.id] = saved[item.id]; continue; }
    const group = groups[item.id];
    if (group && !byGroup.has(group)) byGroup.set(group, take());
    labels[item.id] = `character-${group ? byGroup.get(group) : take()}`;
  }
  return labels;
}

function referencesFor(hook, node) {
  const mode = hook.mode();
  const groups = node.properties?.[GROUPS_KEY] || {};
  const laneOrder = { image: 0, video: 1, audio: 2 };
  const items = hook.items().sort((a, b) => (laneOrder[a.lane] - laneOrder[b.lane]) || (a.slot - b.slot));
  const labels = mode === "REF2VA" ? pictureLabels(items.filter(item => item.lane === "image"), node) : {};
  return items
    .map(item => {
      if (item.lane === "image") return { item, kind: "image", path: item.value, role: mode === "REF2VA" ? (item.forge_role || "subject") : "keyframe", subject_group: mode === "REF2VA" ? (groups[item.id] || "") : "", ...(labels[item.id] ? { easy_role: labels[item.id] } : {}) };
      if (item.lane === "audio" && item.type === "audio") return { item, kind: "audio", duration_seconds: item.duration };
      return { item, kind: "video", role: "motion", stream: item.media_mode === "audio" ? "audio" : item.media_mode === "video_audio" ? "both" : "video", duration_seconds: item.duration };
    });
}

async function open(node) {
  const hook = node.__dasiwaH3Forge;
  if (!hook) return;
  openDialogs.get(node)?.();
  installStyles();
  const mode = hook.mode();
  const prefs = remembered();
  const continuity = hook.continuity?.();
  const openedKey = hook.contextKey?.();
  const compatible = entry => entry.mode === hook.mode() && !!entry.continuity === !!hook.continuity?.() && (!entry.contextKey || entry.contextKey === hook.contextKey?.());

  const overlay = el("div", { className: "ds-forge-overlay" });
  const box = el("div", { className: "ds-forge" });
  overlay.append(box);
  // A run in flight, so Cancel and closing the pop-out can stop it.
  let running = null, closed = false, loadingModels = true, statusTimer = null;
  const cancelRun = () => { clearInterval(statusTimer); statusTimer = null; if (running) { const id = running; running = null; api.fetchApi("/dasiwa/h3/forge/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: id }) }).catch(() => {}); } };
  const close = () => { if (closed) return; closed = true; cancelRun(); overlay.remove(); document.removeEventListener("keydown", onKey); openDialogs.delete(node); };
  openDialogs.set(node, close);
  const onKey = e => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("pointerdown", e => { if (e.target === overlay) close(); });

  const closeBtn = el("button", { textContent: "×", title: "Close (Esc)", onclick: close });
  box.append(el("h3", {}, el("span", { textContent: `H3 Forge — ${mode}${continuity ? " · Continuity Active" : ""}` }), closeBtn));

  const brief = el("textarea", { placeholder: continuity ? "What happens next? Leave empty to continue naturally." : "What should the clip be? A sentence or two is enough.", value: continuity ? "" : briefs.get(node.id) || forgeHistory(node).find(e => !e.continuity)?.brief || "" });
  box.append(el("div", { className: "field" }, el("label", { textContent: continuity ? "Next action" : "Idea" }), brief));

  // References from the timeline. REF2VA pictures need a role; base-mode
  // pictures are frames by definition.
  const refs = continuity ? [] : referencesFor(hook, node);
  if (continuity) box.append(el("div", { className: "muted", textContent: "The source ending and Duration guide this draft. A vision model uses tail frames internally; audio is not analyzed. Review the result, then Apply to node." }));
  // REF2VA pictures: one label each - Character 1-4, several characters in one
  // picture, Place, Style, First or Last frame. Pictures with the same
  // Character number are one subject (what subject groups did), and the idea
  // names the labels. The node writes who is who; no picture goes to the model.
  // Base-mode pictures are frames by definition and need no label.
  const labelled = mode === "REF2VA" && refs.some(r => r.kind === "image");
  const refsBox = el("div", { className: "field" });
  if (!continuity && labelled) brief.placeholder = 'Name the labels: "Character 1 sits on the bed in the place. Character 2 walks in and waves."';
  if (refs.length) {
    box.append(refsBox);
    const list = el("div", { className: "refs" });
    let counts = { image: 0, video: 0, audio: 0 };
    for (const ref of refs) {
      counts[ref.kind] += 1;
      const name = `${ref.kind === "image" ? "Picture" : ref.kind === "video" ? "Video" : "Audio"} ${counts[ref.kind]}`;
      const thumb = ref.kind === "image" ? el("img", { src: viewUrl(ref.path) }) : el("span", { className: "muted", textContent: ref.kind });
      let roleCell;
      if (ref.kind === "image" && labelled) {
        const save = value => {
          ref.easy_role = value;
          node.properties ||= {};
          node.properties[EASY_ROLES_KEY] ||= {};
          node.properties[EASY_ROLES_KEY][ref.item.id] = value;
          node.graph?.setDirtyCanvas(true, true);
        };
        const kindSel = el("select", { title: "What this picture is." });
        for (const [value, label] of PICTURE_KINDS) kindSel.append(el("option", { value, textContent: label, selected: pictureKind(ref.easy_role) === value }));
        const whoSel = el("select", { title: "Which character. Pictures with the same Character number are one character." });
        const fillWho = () => {
          const choices = PICTURE_WHO[pictureKind(ref.easy_role)];
          whoSel.replaceChildren(...(choices || []).map(([value, label]) => el("option", { value, textContent: label, selected: value === ref.easy_role })));
          whoSel.hidden = !choices;
        };
        // Switching to Character picks a number no other picture uses.
        const freeCharacter = () => {
          const used = new Set(refs.filter(r => r !== ref && r.easy_role?.startsWith("character-")).map(r => r.easy_role));
          return PICTURE_WHO.character.find(([value]) => !used.has(value))?.[0] || "character-1";
        };
        kindSel.onchange = e => {
          const kind = e.target.value;
          save(kind === "character" ? freeCharacter() : PICTURE_WHO[kind]?.[0][0] || kind);
          fillWho();
        };
        whoSel.onchange = e => save(e.target.value);
        fillWho();
        roleCell = el("span", { className: "pick" }, kindSel, whoSel);
      } else {
        roleCell = el("span", { className: "muted", textContent: ref.kind === "image" ? BASE_ROLE[mode] || "frame" : ref.kind === "video" ? `motion · ${ref.stream}` : "voice" });
      }
      const keep = el("input", { type: "text", placeholder: "keep (optional)", value: ref.keep || "", oninput: e => { ref.keep = e.target.value.trim(); } });
      list.append(el("div", { className: "ref" }, thumb, el("span", { textContent: name }), roleCell, ref.kind === "audio" ? el("span") : keep));
    }
    refsBox.append(el("label", { textContent: "References on the timeline" }), list);
    if (labelled) {
      refsBox.append(el("span", { className: "muted", textContent: 'Pictures with the same Character number are one character. A picture with two or three of them: pick the "Characters" choice that matches who stands where, left to right. In the idea, write "Character 1", "Character 2" and "the place". The pictures are not sent to the model: H3 sees them itself.' }));
    }
  } else if (mode !== "T2VA" && !continuity) {
    box.append(el("div", { className: "muted", textContent: `${mode} expects pictures on the timeline; none are loaded, so the model writes from the idea alone.` }));
  }

  const modelSel = el("select");
  const detail = el("input", { type: "range", min: 1, max: 10, step: 1 });
  const detailLabel = el("span", { className: "muted" });
  const creativity = el("select");
  // Auto lets the model choose; a number is an instruction the server checks.
  const shots = el("select", { title: "How many shots. Auto lets the model choose." });
  box.append(el("div", { className: "row" },
    el("div", { className: "field" }, el("label", { textContent: "Model" }), modelSel),
    el("div", { className: "field" }, el("label", { textContent: "Creativity" }), creativity),
    el("div", { className: "field" }, el("label", { textContent: "Shots" }), shots)));
  box.append(el("div", { className: "field" }, el("label", {}, "Detail ", detailLabel), detail));
  const status = el("span", { className: "status" });
  const setStatus = (msg, err = false) => { status.textContent = msg; status.classList.toggle("error", err); };
  const genBtn = el("button", { className: "primary", textContent: "Generate", disabled: true });
  const applyBtn = el("button", { textContent: "Apply to node", disabled: true });
  box.append(el("div", { className: "actions" }, status, genBtn, applyBtn));
  const output = el("pre", { hidden: true });
  box.append(output);
  const historyBox = el("div", { className: "history" });
  box.append(historyBox);
  const setControlsDisabled = disabled => {
    [brief, modelSel, detail, creativity, shots, ...refsBox.querySelectorAll("input, select")]
      .forEach(c => { c.disabled = disabled; });
  };
  setControlsDisabled(true);
  let result = null;
  const showResult = entry => {
    if (closed) return;
    brief.value = entry.brief || "";
    if (entry.draftOptions) {
      const { model, detail: level, creativity: preset, shots: count } = entry.draftOptions;
      if (Array.from(modelSel.options).some(o => o.value === model)) modelSel.value = model;
      detail.value = level; creativity.value = preset;
      // Drafts saved before the Shots control have none: they were Auto.
      const shotsValue = String(count ?? "Auto");
      if (Array.from(shots.options).some(o => o.value === shotsValue)) shots.value = shotsValue;
      if (detail.oninput) detail.oninput();
    }
    result = entry;
    output.hidden = false;
    output.textContent = entry.simple_prompt;
    applyBtn.disabled = loadingModels || !!running || !compatible(entry);
    if (!compatible(entry)) setStatus("Draft belongs to a different source, duration, model or prompt. Generate again for the current context.", true);
    renderHistory();
  };
  const renderHistory = () => {
    const entries = forgeHistory(node);
    const clear = el("button", { type: "button", textContent: "Clear history", disabled: loadingModels || !!running || !entries.length, title: "Remove the three saved Forge prompts from this node" });
    clear.onclick = () => {
      clearForgeHistory(node);
      node.__dasiwaH3Render?.();
      result = null; output.hidden = true; output.textContent = ""; applyBtn.disabled = true;
      renderHistory();
    };
    historyBox.replaceChildren(el("div", { className: "history-head" }, el("label", { textContent: "Last 3 generated prompts (saved with this node)" }), clear));
    if (!entries.length) { historyBox.append(el("span", { className: "muted", textContent: "No prompts generated yet." })); return; }
    entries.forEach((entry, index) => {
      const date = Number.isFinite(entry.createdAt) ? new Date(entry.createdAt).toLocaleString() : "Saved draft";
      const button = el("button", { type: "button", disabled: loadingModels || !!running, className: result === entry ? "selected" : "", title: "Show this prompt; Apply to node to use it" },
        el("span", { textContent: `${index + 1}. ${entry.mode}${entry.continuity ? " · Continuity" : ""} · ${entry.model || "model"} · ${date}` }),
        el("span", { className: "excerpt", textContent: entry.simple_prompt.replace(/\s+/g, " ").slice(0, 150) }));
      button.onclick = () => showResult(entry);
      historyBox.append(button);
    });
  };
  const latest = forgeHistory(node).find(compatible);
  renderHistory();
  document.body.append(overlay);
  brief.focus();

  const notes = el("div", { className: "muted" });
  box.insertBefore(notes, status.parentElement);
  let levels = {};
  setStatus("Loading Forge models…");
  try {
    const res = await api.fetchApi("/dasiwa/h3/forge/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: forgeSettings() }) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || res.statusText);
    if (closed) return;
    notes.textContent = Object.values(data.errors || {}).join(" ");
    notes.style.color = notes.textContent ? "#ff8a8a" : "";
    const usable = data.models.filter(m => !m.disabled);
    if (!usable.length) throw new Error(NO_MODELS);
    for (const source of ["local", "ollama", "openai"]) {
      const group = data.models.filter(m => m.id.startsWith(source + ":"));
      if (!group.length) continue;
      const og = el("optgroup", { label: SOURCE_NAME[source] });
      for (const m of group) og.append(el("option", { value: m.id, textContent: m.label, disabled: !!m.disabled }));
      modelSel.append(og);
    }
    modelSel.value = usable.some(m => m.id === prefs.model) ? prefs.model : usable[0].id;
    for (const c of data.creativity) creativity.append(el("option", { value: c, textContent: c[0].toUpperCase() + c.slice(1) }));
    creativity.value = prefs.creativity && data.creativity.includes(prefs.creativity) ? prefs.creativity : data.default_creativity;
    levels = data.detail_levels;
    detail.value = prefs.detail || data.default_detail;
    const counts = (data.shot_counts || ["Auto"]).map(String);
    for (const c of counts) shots.append(el("option", { value: c, textContent: c }));
    shots.value = prefs.shots && counts.includes(String(prefs.shots)) ? String(prefs.shots) : String(data.default_shots || "Auto");
    genBtn.disabled = false;
    setStatus("Ready. Generate a draft, then review and Apply.");
  } catch (err) {
    setStatus(err.message, true);
    genBtn.disabled = true;
  }
  if (closed) return;
  loadingModels = false;
  setControlsDisabled(false);
  brief.focus();
  const syncDetail = () => { detailLabel.textContent = `${detail.value} of 10 — ${levels[detail.value] || ""}`; };
  detail.oninput = syncDetail; syncDetail();
  if (latest) showResult(latest); else renderHistory();
  const clearDraft = () => {
    if (running || closed) return;
    result = null; applyBtn.disabled = true; output.hidden = true; output.textContent = "";
    setStatus("Idea or options changed. Generate a new draft, or choose a saved draft.");
    renderHistory();
  };
  brief.addEventListener("input", clearDraft);
  modelSel.addEventListener("change", clearDraft);
  creativity.addEventListener("change", clearDraft);
  shots.addEventListener("change", clearDraft);
  detail.addEventListener("input", clearDraft);
  refsBox.addEventListener("change", clearDraft);
  refsBox.addEventListener("input", clearDraft);
  genBtn.onclick = async () => {
    if (closed) return;
    if (running) { cancelRun(); genBtn.disabled = true; setStatus("Cancelling… the model stops at its next token, then unloads."); return; }
    const text = brief.value.trim();
    if (openedKey !== hook.contextKey?.()) { setStatus("Director context changed. Close and reopen Forge.", true); return; }
    if (!text && !continuity) { setStatus("Write the idea first.", true); return; }
    if (!continuity) briefs.set(node.id, text);
    remember({ model: modelSel.value, creativity: creativity.value, detail: Number(detail.value), shots: shots.value });
    result = null; output.hidden = true; output.textContent = ""; renderHistory();
    applyBtn.disabled = true;
    setControlsDisabled(true);
    const draftOptions = { model: modelSel.value, detail: Number(detail.value), creativity: creativity.value, shots: shots.value };
    const requestId = `forge-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    running = requestId; renderHistory();
    genBtn.textContent = "Cancel";
    const started = Date.now();
    statusTimer = setInterval(() => setStatus(`Writing with ${modelSel.selectedOptions[0]?.textContent || modelSel.value}… ${Math.round((Date.now() - started) / 1000)}s (Cancel stops it; the model unloads either way)`), 500);
    try {
      const res = await api.fetchApi("/dasiwa/h3/forge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: requestId, brief: text, mode, duration: hook.duration(), model: modelSel.value,
          detail: Number(detail.value), creativity: creativity.value, shots: shots.value,
          references: refs.map(({ item, ...r }) => r), settings: forgeSettings(), continuity, easy: labelled,
        }),
      });
      const data = await res.json();
      if (!res.ok) { output.hidden = !data.raw; output.textContent = data.raw || ""; throw new Error(data.message || res.statusText); }
      if (closed || running !== requestId) throw new Error("Draft cancelled; no prompt was changed.");
      if (openedKey !== hook.contextKey?.()) throw new Error("Source, duration, model or prompt changed during drafting. Reopen Forge and generate again.");
      if (!!data.continuity !== !!continuity || (continuity && data.source_id !== continuity.clip_id)) throw new Error("Draft does not match the selected continuity source.");
      data.contextKey = openedKey; data.draftOptions = draftOptions;
      const saved = saveForgeResult(node, data, text);
      showResult(saved);
      const seen = data.saw_images ? ` · looked at ${data.saw_images} picture${data.saw_images === 1 ? "" : "s"}` : continuity ? " · text context only (no tail images)" : refs.some(r => r.kind === "image") && data.vision === false ? " · this model cannot see images, so it wrote from your idea only" : "";
      const warned = [...(data.warnings || []), ...(data.unloaded ? [] : ["WARNING: model still loaded"])];
      setStatus(`Done in ${data.stats.seconds}s${data.stats.output_tokens ? ` · ${data.stats.output_tokens} tokens` : ""}${seen}${warned.length ? " · " + warned.join(" · ") : " · model unloaded"}`, warned.length > 0);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      clearInterval(statusTimer); statusTimer = null;
      running = null;
      setControlsDisabled(false);
      applyBtn.disabled = !result || !compatible(result);
      genBtn.disabled = false;
      genBtn.textContent = "Regenerate";
      renderHistory();
    }
  };
  applyBtn.onclick = () => {
    if (!result) return;
    if (!compatible(result) || hook.apply(result) === false) { setStatus("Draft is stale. Generate again for the current source, duration and prompt.", true); return; }
    hook.setStatus(`Forge prompt applied (${result.model}).`);
    close();
  };
}

// At load, not on first open: the toolbar button's style lives here too.
installStyles();
window.DaSiWaH3Forge = { open, close: node => openDialogs.get(node)?.(), clearHistory: clearForgeHistory, settings: forgeSettings };
