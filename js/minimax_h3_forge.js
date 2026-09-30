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
app.registerExtension({
  name: "DaSiWa.H3Forge",
  settings: [
    { id: SETTING_OLLAMA, category: ["DaSiWa", "H3 Forge", "Ollama address"], name: "Ollama address", type: "text", defaultValue: "", tooltip: "Leave empty for Ollama on this computer (http://127.0.0.1:11434). Set it to use Ollama on another machine." },
    { id: SETTING_OPENAI, category: ["DaSiWa", "H3 Forge", "OpenAI-compatible server"], name: "OpenAI-compatible server address", type: "text", defaultValue: "", tooltip: "Optional: a llama.cpp server, llama-swap, LM Studio or koboldcpp, e.g. http://127.0.0.1:8080. Empty = off." },
  ],
});
function settingValue(id) {
  try { return app.extensionManager?.setting?.get(id) ?? app.ui?.settings?.getSettingValue(id) ?? ""; } catch { return ""; }
}
const forgeSettings = () => ({ ollama_url: settingValue(SETTING_OLLAMA) || "", openai_url: settingValue(SETTING_OPENAI) || "" });
const SOURCE_NAME = { local: "ComfyUI models/llm (loads inside ComfyUI)", ollama: "Ollama", openai: "OpenAI-compatible server" };
const NO_MODELS = "No models found. Easiest fix: put a vision model folder (for example Qwen3-VL-8B-Instruct from Hugging Face) in ComfyUI/models/llm and reopen Forge. Or install Ollama and run: ollama pull qwen3-vl:8b. Other servers: Settings > DaSiWa > H3 Forge.";

const STORE_KEY = "dasiwa.h3forge";
const briefs = new Map(); // node id -> last brief, for a reroll after closing
const HISTORY_KEY = "dasiwaH3ForgeHistory";
function forgeHistory(node) {
  const saved = node.properties?.[HISTORY_KEY];
  return Array.isArray(saved) ? saved.filter(entry => entry && typeof entry.simple_prompt === "string" && entry.simple_prompt.trim() && typeof entry.mode === "string" && entry.fields && typeof entry.fields === "object").slice(0, 3) : [];
}
function saveForgeResult(node, result, brief) {
  const entry = {
    mode: result.mode, model: result.model, simple_prompt: result.simple_prompt,
    fields: result.fields, brief, createdAt: Date.now(), duration: Number(result.duration) || null,
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
  .ds-forge .ref{display:grid;grid-template-columns:48px 78px 104px 96px 1fr;gap:8px;align-items:center}
  .ds-forge .ref img{width:48px;height:36px;object-fit:cover;border-radius:3px;background:#090d11;display:block}
  .ds-forge .ref .thumb{position:relative;width:48px;height:36px}
  .ds-forge .ref .eye{position:absolute;top:-5px;right:-5px;width:18px;height:18px;padding:0;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#0d1217;color:#b180ff;border:1px solid #b180ff}
  .ds-forge .ref .eye[hidden]{display:none}
  .ds-forge .ref .eye:hover{background:#2c3c49;color:#e6d9ff}
  .ds-forge-card{position:fixed;z-index:10001;width:min(320px,calc(100vw - 16px));max-height:60vh;overflow:auto;background:#0d1217;color:#e5eef4;border:1px solid #b180ff;border-radius:6px;padding:9px 11px 11px;font:13px system-ui,sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.6)}
  .ds-forge-card .head{display:flex;gap:8px;align-items:flex-start;color:#b180ff;font:11px ui-monospace,monospace;margin-bottom:6px}
  .ds-forge-card .head span{flex:1}
  .ds-forge-card p{margin:0;font-size:12px;line-height:1.5}
  .ds-forge-card textarea{width:100%;box-sizing:border-box;min-height:120px;resize:vertical;background:#111820;color:#e5eef4;border:1px solid #40515e;border-radius:4px;padding:6px;font:12px/1.5 system-ui,sans-serif}
  .ds-forge-card .foot{display:flex;justify-content:flex-end;gap:6px;margin-top:8px}
  .ds-forge-card button{background:#202b35;color:#dbe7f0;border:1px solid #40515e;border-radius:4px;padding:4px 10px;cursor:pointer;font:12px system-ui,sans-serif}
  .ds-forge-card button:hover{background:#2c3c49}
  .ds-forge-card button:disabled{opacity:.45;cursor:default}
  .ds-forge-card .x{border:0;background:transparent;color:#8fa3b2;font-size:16px;line-height:1;padding:0 2px}
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

// What Forge knows about each picture: role, subject group, keep note, and the
// description the model wrote when it looked at it. Saved with the node, keyed
// by the picture's file, because hook.items() hands out copies: anything
// written on an item is gone when the overlay closes.
const REFS_KEY = "dasiwaH3ForgeRefs";
function refStore(node) {
  node.properties ||= {};
  const saved = node.properties[REFS_KEY];
  return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : (node.properties[REFS_KEY] = {});
}
function saveRefMeta(node, path, patch) {
  if (!path) return;
  const store = refStore(node);
  const next = { ...(store[path] || {}), ...patch };
  for (const key of Object.keys(next)) if (next[key] === undefined || next[key] === null || next[key] === "") delete next[key];
  if (Object.keys(next).length) store[path] = next; else delete store[path];
  node.graph?.setDirtyCanvas(true, true);
}

function referencesFor(hook, node) {
  const mode = hook.mode();
  const laneOrder = { image: 0, video: 1, audio: 2 };
  const store = refStore(node);
  return hook.items()
    .sort((a, b) => (laneOrder[a.lane] - laneOrder[b.lane]) || (a.slot - b.slot))
    .map(item => {
      if (item.lane === "image") {
        const meta = store[item.value] || {};
        const ref = { item, kind: "image", path: item.value, role: mode === "REF2VA" ? (meta.role || item.forge_role || "subject") : "keyframe" };
        if (meta.keep) ref.keep = meta.keep;
        if (mode === "REF2VA") {
          if (meta.group) ref.subject_group = meta.group;
          if (meta.reading) Object.assign(ref, { reading: meta.reading, reading_labels: meta.reading_labels || "", reading_edited: !!meta.reading_edited });
        }
        return ref;
      }
      if (item.lane === "audio" && item.type === "audio") return { item, kind: "audio", duration_seconds: item.duration };
      return { item, kind: "video", role: "motion", stream: item.media_mode === "audio" ? "audio" : item.media_mode === "video_audio" ? "both" : "video", duration_seconds: item.duration };
    });
}

async function open(node) {
  const hook = node.__dasiwaH3Forge;
  if (!hook) return;
  installStyles();
  const mode = hook.mode();
  const prefs = remembered();

  const overlay = el("div", { className: "ds-forge-overlay" });
  const box = el("div", { className: "ds-forge" });
  overlay.append(box);
  // A run in flight, so Cancel and closing the pop-out can stop it.
  let running = null;
  const cancelRun = () => { if (running) { const id = running; running = null; api.fetchApi("/dasiwa/h3/forge/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: id }) }).catch(() => {}); } };
  const close = () => { cancelRun(); closeCard(); overlay.remove(); document.removeEventListener("keydown", onKey); document.removeEventListener("pointerdown", onCardOutside, true); };
  // Esc closes an open description card first, then the pop-out.
  const onKey = e => { if (e.key !== "Escape") return; if (card) closeCard(); else close(); };
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("pointerdown", e => { if (e.target === overlay) close(); });

  const closeBtn = el("button", { textContent: "×", title: "Close (Esc)", onclick: close });
  box.append(el("h3", {}, el("span", { textContent: `H3 Forge — ${mode}` }), closeBtn));

  const brief = el("textarea", { placeholder: "What should the clip be? A sentence or two is enough.", value: briefs.get(node.id) || forgeHistory(node)[0]?.brief || "" });
  box.append(el("div", { className: "field" }, el("label", { textContent: "Idea" }), brief));

  // References from the timeline. REF2VA pictures need a role; base-mode
  // pictures are frames by definition.
  const refs = referencesFor(hook, node);
  // Picture N is the Nth picture here, the same order the Director numbers them.
  const pictures = refs.filter(r => r.kind === "image");
  const scans = mode === "REF2VA" && pictures.length >= 2;

  // ── What the model saw in each picture ──
  // A description sits on its picture behind the eye. It is cleared when what
  // the scanner was told about the picture changes, and the next Generate or
  // Scan looks at it again.
  const syncEye = ref => { if (ref.eye) ref.eye.hidden = !ref.reading; };
  const setReading = (ref, labels, text, edited = false) => {
    Object.assign(ref, { reading: text, reading_labels: labels, reading_edited: edited });
    saveRefMeta(node, ref.path, { reading: text, reading_labels: labels, reading_edited: edited || undefined });
    syncEye(ref);
  };
  const clearReading = ref => {
    if (!ref.reading) return;
    delete ref.reading; delete ref.reading_labels; delete ref.reading_edited;
    saveRefMeta(node, ref.path, { reading: undefined, reading_labels: undefined, reading_edited: undefined });
    syncEye(ref);
    if (card?.ref === ref) closeCard();
  };
  const placeReadings = list => {
    for (const r of list || []) for (const n of r.pictures || []) {
      const p = pictures[n - 1];
      // The server echoes descriptions it was handed; those keep their "edited" mark.
      if (p && r.text && p.reading !== r.text) setReading(p, r.labels, r.text);
    }
  };
  // The pictures that share a description: a subject group, as it was scanned.
  const siblings = ref => pictures.filter(p => p === ref || (ref.reading_labels && p.reading_labels === ref.reading_labels));
  const payloadRefs = () => refs.map(({ item, eye, reading_labels, reading_edited, ...r }) => r);

  let card = null; // { el, ref }
  const closeCard = () => { if (card) { card.el.remove(); card = null; } };
  const openCard = ref => {
    if (card?.ref === ref) { closeCard(); return; }
    closeCard();
    if (!ref.reading) return;
    const title = el("span");
    const setTitle = () => {
      const labels = ref.reading_labels || "";
      title.textContent = (labels.includes(",") ? `What the model saw · ${labels}, read together` : "What the model saw") + (ref.reading_edited ? " · edited" : "");
    };
    setTitle();
    const text = el("p", { textContent: ref.reading });
    const foot = el("div", { className: "foot" });
    const rescan = el("button", { type: "button", textContent: "Rescan", title: "Have the model look at this picture again" });
    const edit = el("button", { type: "button", textContent: "Edit", title: "Fix the description yourself" });
    rescan.onclick = async () => {
      rescan.disabled = edit.disabled = true;
      await runScan({ pictures: [pictures.indexOf(ref) + 1], force: true }, s => { rescan.textContent = s; });
      rescan.textContent = "Rescan";
      rescan.disabled = edit.disabled = false;
      if (card?.ref === ref) { text.textContent = ref.reading || ""; setTitle(); }
    };
    edit.onclick = () => {
      const area = el("textarea", { value: ref.reading || "" });
      const editFoot = el("div", { className: "foot" });
      const back = () => { area.replaceWith(text); editFoot.replaceWith(foot); };
      const save = el("button", { type: "button", textContent: "Save" });
      save.onclick = () => {
        const next = area.value.trim();
        for (const s of siblings(ref)) { if (next) setReading(s, ref.reading_labels || "", next, true); else clearReading(s); }
        if (!next) { closeCard(); return; }
        text.textContent = next; setTitle(); back();
      };
      editFoot.append(el("button", { type: "button", textContent: "Cancel", onclick: back }), save);
      text.replaceWith(area); foot.replaceWith(editFoot);
      area.focus();
    };
    foot.append(rescan, edit);
    const cardEl = el("div", { className: "ds-forge-card" },
      el("div", { className: "head" }, title, el("button", { type: "button", className: "x", textContent: "×", title: "Close", onclick: closeCard })),
      text, foot);
    document.body.append(cardEl);
    // Over the thumbnail, kept inside the window.
    const at = ref.eye.parentElement.getBoundingClientRect();
    cardEl.style.left = `${Math.max(8, Math.min(at.left, window.innerWidth - cardEl.offsetWidth - 8))}px`;
    cardEl.style.top = `${Math.max(8, Math.min(at.top, window.innerHeight - cardEl.offsetHeight - 8))}px`;
    card = { el: cardEl, ref };
  };
  // A click anywhere else closes the card; the eye toggles it itself.
  const onCardOutside = e => { if (card && !card.el.contains(e.target) && !e.target.closest?.(".eye")) closeCard(); };
  document.addEventListener("pointerdown", onCardOutside, true);

  if (refs.length) {
    const list = el("div", { className: "refs" });
    let counts = { image: 0, video: 0, audio: 0 };
    for (const ref of refs) {
      counts[ref.kind] += 1;
      const name = `${ref.kind === "image" ? "Picture" : ref.kind === "video" ? "Video" : "Audio"} ${counts[ref.kind]}`;
      let thumb;
      if (ref.kind === "image") {
        ref.eye = el("button", { type: "button", className: "eye", hidden: !ref.reading, title: "What the model saw in this picture", onclick: () => openCard(ref) });
        ref.eye.innerHTML = '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.4" d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>';
        thumb = el("div", { className: "thumb" }, el("img", { src: viewUrl(ref.path) }), ref.eye);
      } else {
        thumb = el("span", { className: "muted", textContent: ref.kind });
      }
      let roleCell, groupCell = el("span");
      if (ref.kind === "image" && mode === "REF2VA") {
        const group = el("select", { title: "Give the same group to pictures of one person or object. Separate is the default.", disabled: ref.role !== "subject" });
        for (const [value, label] of [["", "Separate"], ["A", "Group A"], ["B", "Group B"], ["C", "Group C"], ["D", "Group D"]]) {
          group.append(el("option", { value, textContent: label, selected: (ref.subject_group || "") === value }));
        }
        group.onchange = () => {
          // Both the group it leaves and the one it joins are described as a unit.
          for (const s of siblings(ref)) clearReading(s);
          if (group.value) ref.subject_group = group.value; else delete ref.subject_group;
          saveRefMeta(node, ref.path, { group: group.value });
          for (const p of pictures) if (p !== ref && group.value && p.subject_group === group.value) clearReading(p);
        };
        roleCell = el("select", { onchange: e => {
          ref.role = e.target.value;
          saveRefMeta(node, ref.path, { role: ref.role });
          // Only subject pictures group.
          group.disabled = ref.role !== "subject";
          for (const s of siblings(ref)) clearReading(s);
        } });
        for (const r of ["subject", "style", "keyframe"]) roleCell.append(el("option", { value: r, textContent: r, selected: ref.role === r }));
        groupCell = group;
      } else {
        roleCell = el("span", { className: "muted", textContent: ref.kind === "image" ? BASE_ROLE[mode] || "frame" : ref.kind === "video" ? `motion · ${ref.stream}` : "voice" });
      }
      const keep = el("input", { type: "text", placeholder: "keep (optional)", value: ref.keep || "", oninput: e => {
        ref.keep = e.target.value.trim();
        if (ref.kind === "image") { saveRefMeta(node, ref.path, { keep: ref.keep }); for (const s of siblings(ref)) clearReading(s); }
      } });
      list.append(el("div", { className: "ref" }, thumb, el("span", { textContent: name }), roleCell, groupCell, ref.kind === "audio" ? el("span") : keep));
    }
    box.append(el("div", { className: "field" }, el("label", { textContent: "References on the timeline" }), list));
  } else if (mode !== "T2VA") {
    box.append(el("div", { className: "muted", textContent: `${mode} expects pictures on the timeline; none are loaded, so the model writes from the idea alone.` }));
  }

  const modelSel = el("select");
  const detail = el("input", { type: "range", min: 1, max: 10, step: 1 });
  const detailLabel = el("span", { className: "muted" });
  const creativity = el("select");
  box.append(el("div", { className: "row" },
    el("div", { className: "field" }, el("label", { textContent: "Model" }), modelSel),
    el("div", { className: "field" }, el("label", { textContent: "Creativity" }), creativity)));
  box.append(el("div", { className: "field" }, el("label", {}, "Detail ", detailLabel), detail));

  const status = el("span", { className: "status" });
  const setStatus = (msg, err = false) => { status.textContent = msg; status.classList.toggle("error", err); };
  const genBtn = el("button", { className: "primary", textContent: "Generate" });
  const applyBtn = el("button", { textContent: "Apply to node", disabled: true });
  // Optional: look at the pictures now, to check or fix what the model saw
  // before writing. Generate scans whatever has no description yet.
  const scanBtn = el("button", { textContent: "Scan pictures", hidden: !scans, title: "Have the model look at each picture now, so you can check what it saw before generating" });
  box.append(el("div", { className: "actions" }, status, scanBtn, genBtn, applyBtn));

  // One scan at a time, and never alongside a write: both load the model.
  const runScan = async (extra, onProgress = () => {}) => {
    if (running) return;
    const requestId = `forge-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    running = requestId;
    genBtn.disabled = scanBtn.disabled = true;
    const started = Date.now();
    const say = () => { const s = `Scanning… ${Math.round((Date.now() - started) / 1000)}s`; onProgress(s); setStatus(`${s} (the model unloads when it is done)`); };
    say();
    const tick = setInterval(say, 500);
    try {
      const res = await api.fetchApi("/dasiwa/h3/forge/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, model: modelSel.value, references: payloadRefs(), settings: forgeSettings(), ...extra }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || res.statusText);
      placeReadings(data.readings);
      const n = data.scanned || 0;
      setStatus(`Looked at ${n} picture${n === 1 ? "" : "s or groups"} in ${data.stats.seconds}s · open the eye on a picture to check what it saw${data.unloaded ? " · model unloaded" : " · WARNING: model still loaded"}`, !data.unloaded);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      clearInterval(tick);
      running = null;
      genBtn.disabled = scanBtn.disabled = false;
    }
  };
  scanBtn.onclick = () => runScan({});
  const output = el("pre", { hidden: true });
  box.append(output);
  const historyBox = el("div", { className: "history" });
  box.append(historyBox);
  let result = null;
  const showResult = entry => {
    result = entry;
    output.hidden = false;
    output.textContent = entry.simple_prompt;
    applyBtn.disabled = !!running || entry.mode !== hook.mode();
    if (entry.mode !== hook.mode()) setStatus(`This draft is for ${entry.mode}; switch the Director to that mode before applying.`, true);
    // Timestamps, and FL2VA/L2VA's end-frame line, are written for the length the
    // draft was made at. Still appliable; the person may be about to change it back.
    else if (entry.duration && hook.duration() && entry.duration !== hook.duration()) setStatus(`This draft was written for a ${entry.duration} s clip and the Director is set to ${hook.duration()} s, so its timing won't fit. Set the duration back or regenerate.`, true);
    renderHistory();
  };
  const renderHistory = () => {
    const entries = forgeHistory(node);
    const clear = el("button", { type: "button", textContent: "Clear history", disabled: !entries.length, title: "Remove the three saved Forge prompts from this node" });
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
      const button = el("button", { type: "button", className: result === entry ? "selected" : "", title: "Show this prompt; Apply to node to use it" },
        el("span", { textContent: `${index + 1}. ${entry.mode} · ${entry.model || "model"} · ${date}` }),
        el("span", { className: "excerpt", textContent: entry.simple_prompt.replace(/\s+/g, " ").slice(0, 150) }));
      button.onclick = () => showResult(entry);
      historyBox.append(button);
    });
  };
  const latest = forgeHistory(node)[0];
  if (latest) showResult(latest); else renderHistory();
  document.body.append(overlay);
  brief.focus();

  const notes = el("div", { className: "muted" });
  box.insertBefore(notes, status.parentElement);
  let levels = {};
  try {
    const res = await api.fetchApi("/dasiwa/h3/forge/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: forgeSettings() }) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || res.statusText);
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
  } catch (err) {
    setStatus(err.message, true);
    genBtn.disabled = true;
  }
  const syncDetail = () => { detailLabel.textContent = `${detail.value} of 10 — ${levels[detail.value] || ""}`; };
  detail.oninput = syncDetail; syncDetail();

  genBtn.onclick = async () => {
    if (running) { cancelRun(); genBtn.disabled = true; setStatus("Cancelling… the model stops at its next token, then unloads."); return; }
    const text = brief.value.trim();
    if (!text) { setStatus("Write the idea first.", true); return; }
    briefs.set(node.id, text);
    remember({ model: modelSel.value, creativity: creativity.value, detail: Number(detail.value) });
    applyBtn.disabled = true;
    const requestId = `forge-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    running = requestId;
    scanBtn.disabled = true;
    genBtn.textContent = "Cancel";
    const started = Date.now();
    const tick = setInterval(() => setStatus(`Writing with ${modelSel.selectedOptions[0]?.textContent || modelSel.value}… ${Math.round((Date.now() - started) / 1000)}s (Cancel stops it; the model unloads either way)`), 500);
    try {
      const res = await api.fetchApi("/dasiwa/h3/forge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: requestId, brief: text, mode, duration: hook.duration(), model: modelSel.value,
          detail: Number(detail.value), creativity: creativity.value,
          references: payloadRefs(), settings: forgeSettings(),
        }),
      });
      const data = await res.json();
      if (!res.ok) { output.hidden = !data.raw; output.textContent = data.raw || ""; throw new Error(data.message || res.statusText); }
      // What it saw in each picture, behind the eyes. Pictures it scanned just
      // now get theirs; ones already described keep what they had.
      placeReadings(data.readings);
      const saved = saveForgeResult(node, data, text);
      showResult(saved);
      // A .gguf in models/llm is text-only here even when the model itself can see,
      // because its vision lives in a separate mmproj file. Say so, not just "cannot see".
      const blind = /^local:.*\.gguf$/i.test(data.model || "")
        ? " · .gguf models in ComfyUI/models/llm can't see pictures (vision needs the model's mmproj file, run through Ollama); it wrote from your idea only"
        : " · this model cannot see images, so it wrote from your idea only";
      const seen = data.saw_images ? ` · looked at ${data.saw_images} picture${data.saw_images === 1 ? "" : "s"}` : refs.some(r => r.kind === "image") && data.vision === false ? blind : "";
      const warned = [...(data.warnings || []), ...(data.unloaded ? [] : ["WARNING: model still loaded"])];
      setStatus(`Done in ${data.stats.seconds}s${data.stats.output_tokens ? ` · ${data.stats.output_tokens} tokens` : ""}${seen}${warned.length ? " · " + warned.join(" · ") : " · model unloaded"}`, warned.length > 0);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      clearInterval(tick);
      running = null;
      applyBtn.disabled = !result || result.mode !== hook.mode();
      genBtn.disabled = scanBtn.disabled = false;
      genBtn.textContent = "Regenerate";
    }
  };
  applyBtn.onclick = () => {
    if (!result) return;
    hook.apply(result);
    hook.setStatus(`Forge prompt applied (${result.model}).`);
    close();
  };
}

// At load, not on first open: the toolbar button's style lives here too.
installStyles();
window.DaSiWaH3Forge = { open, clearHistory: clearForgeHistory };
