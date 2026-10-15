"""Prompt Editor: the Prompt Writer's text in an editable box, encoded with CLIP.

A new prompt from the writer replaces the box. When the writer did not run
again (cached, bypassed, or held back by run_writer off) the box is encoded as
the user left it. The last writer text seen is kept in the node's properties,
which reach the server in the workflow saved with the prompt.
"""

LAST_SEEN = "dasiwa_last_llm_prompt"


def _workflow_node(extra_pnginfo, unique_id):
    workflow = (extra_pnginfo or {}).get("workflow") if isinstance(extra_pnginfo, dict) else None
    if not isinstance(workflow, dict) or unique_id is None:
        return None
    # A node inside a subgraph runs as "outer:inner"; its properties live in
    # the subgraph definition under the inner id.
    uid = str(unique_id)
    inner = uid.rsplit(":", 1)[-1]
    if ":" not in uid:
        pools = [workflow.get("nodes") or []]
    else:
        pools = [s.get("nodes") or [] for s in (workflow.get("definitions") or {}).get("subgraphs") or []]
    for nodes in pools:
        for node in nodes:
            if isinstance(node, dict) and str(node.get("id")) == inner:
                return node
    return None


def last_seen(extra_pnginfo, unique_id):
    node = _workflow_node(extra_pnginfo, unique_id)
    value = ((node or {}).get("properties") or {}).get(LAST_SEEN)
    return value if isinstance(value, str) else None


def writer_linked(dynprompt, unique_id):
    try:
        value = dynprompt.get_node(unique_id)["inputs"].get("llm_prompt")
    except Exception:
        return False
    return isinstance(value, list)


class DaSiWa_LLMPromptEditor:
    DESCRIPTION = (
        "Prompt Editor: shows the LLM Prompt Writer's prompt in a box you can edit, and encodes the box with CLIP. "
        "A new prompt from the writer replaces the box; otherwise your edits are encoded. Turning run_writer off stops "
        "the writer running at all and keeps your edits, as does bypassing the writer (Ctrl+B)."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP", {"description": "The text encoder, as on CLIP Text Encode."}),
                "prompt": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": False,
                                      "placeholder": "The Prompt Writer's prompt appears here. Edit it freely.",
                                      "description": "What gets encoded. Filled by each new prompt from the writer; your edits stay until the writer sends a different one."}),
                "run_writer": ("BOOLEAN", {"default": True, "label_on": "on: new prompts replace the box",
                                           "label_off": "off: writer skipped, edits kept",
                                           "description": "On: the writer runs and a new prompt from it replaces the box. Off: the writer does not run and the box is encoded as it is. An empty box always asks the writer."}),
            },
            "optional": {
                "llm_prompt": ("STRING", {"forceInput": True, "lazy": True,
                                          "description": "Connect the LLM Prompt Writer's prompt output. Optional: without it, or with the writer bypassed, this is a plain text encode."}),
            },
            "hidden": {"unique_id": "UNIQUE_ID", "extra_pnginfo": "EXTRA_PNGINFO", "dynprompt": "DYNPROMPT"},
        }

    RETURN_TYPES = ("CONDITIONING", "STRING")
    RETURN_NAMES = ("conditioning", "prompt")
    FUNCTION = "encode"
    CATEGORY = "DaSiWa/LLM"

    def check_lazy_status(self, prompt, run_writer, clip=None, llm_prompt=None, unique_id=None,
                          extra_pnginfo=None, dynprompt=None):
        if not run_writer and str(prompt or "").strip():
            return []
        # Asking for an input with no link is an error in ComfyUI, and a
        # bypassed writer leaves no link.
        return ["llm_prompt"] if writer_linked(dynprompt, unique_id) else []

    def encode(self, clip, prompt, run_writer, llm_prompt=None, unique_id=None, extra_pnginfo=None, dynprompt=None):
        if clip is None:
            raise RuntimeError("ERROR: clip input is invalid: None\n\nIf the clip is from a checkpoint loader node "
                               "your checkpoint does not contain a valid clip or text encoder model.")
        text = str(prompt or "")
        seen = last_seen(extra_pnginfo, unique_id)
        replaced = llm_prompt is not None and llm_prompt != seen
        if replaced:
            text = llm_prompt
        if llm_prompt is not None:
            seen = llm_prompt
        tokens = clip.tokenize(text)
        conditioning = clip.encode_from_tokens_scheduled(tokens)
        # Only a new writer prompt is sent back for the box, so typing done
        # while the queue runs is never overwritten by the text queued.
        ui = {"prompt": [text] if replaced else []}
        if seen is not None:
            ui["llm_prompt"] = [seen]
        return {"ui": ui, "result": (conditioning, text)}
