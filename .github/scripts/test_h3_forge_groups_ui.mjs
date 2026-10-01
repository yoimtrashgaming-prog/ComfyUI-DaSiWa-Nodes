// Exercise Forge's real reference mapping without loading the ComfyUI browser app.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../js/minimax_h3_forge.js", import.meta.url), "utf8")
  .replace(/^import .*;\s*$/gm, "")
  .replace(/\/\/ At load, not on first open:[\s\S]*$/, "");
const context = vm.createContext({
  app: { registerExtension() {} },
  api: {},
  document: { getElementById() { return true; } },
  window: {},
  WeakMap, Map,
});
vm.runInContext(source, context);
const refs = vm.runInContext(`referencesFor({ mode: () => "REF2VA", items: () => [
  { id: "p1", lane: "image", slot: 0, value: "a.png" },
  { id: "p2", lane: "image", slot: 1, value: "b.png", forge_role: "keyframe" },
  { id: "p3", lane: "image", slot: 2, value: "c.png" },
] }, { properties: { dasiwaH3ForgeSubjectGroups: { p1: "A", p2: "A", p3: "A" } } })`, context);
assert.equal(refs.length, 3);
assert.deepEqual(Array.from(refs, r => r.subject_group), ["A", "A", "A"]);
assert.deepEqual(Array.from(refs, r => r.role), ["subject", "keyframe", "subject"]);
const base = vm.runInContext(`referencesFor({ mode: () => "I2VA", items: () => [
  { id: "p1", lane: "image", slot: 0, value: "a.png" },
] }, { properties: { dasiwaH3ForgeSubjectGroups: { p1: "A" } } })`, context);
assert.equal(base[0].subject_group, "");
// Easy mode labels come off the node by item id; unlabelled pictures are Character 1.
const easy = vm.runInContext(`referencesFor({ mode: () => "REF2VA", items: () => [
  { id: "p1", lane: "image", slot: 0, value: "a.png" },
  { id: "p2", lane: "image", slot: 1, value: "b.png" },
] }, { properties: { dasiwaH3ForgeEasyRoles: { p2: "place" } } })`, context);
assert.deepEqual(Array.from(easy, r => r.easy_role), ["character-1", "place"]);
console.log("Forge reference mapping: PASS");
