import { app } from "../../scripts/app.js";

// Prompt Editor: the server sends back a new writer prompt (only when the
// writer sent one) and the last writer prompt it saw. The text goes in the box; the writer prompt goes in
// the node's properties, which travel with the next queued workflow so the
// server can tell a new prompt from a cached one.
const LAST_SEEN = "dasiwa_last_llm_prompt";

app.registerExtension({
  name: "DaSiWa.LLMPromptEditor",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "DaSiWa_LLMPromptEditor") return;
    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      const seen = message?.llm_prompt?.[0];
      if (typeof seen === "string") {
        this.properties ??= {};
        this.properties[LAST_SEEN] = seen;
      }
      const text = message?.prompt?.[0];
      const box = this.widgets?.find(w => w.name === "prompt");
      if (typeof text === "string" && box && box.value !== text) {
        box.value = text;
        box.callback?.(text);
        this.setDirtyCanvas?.(true, true);
      }
    };
  },
});
