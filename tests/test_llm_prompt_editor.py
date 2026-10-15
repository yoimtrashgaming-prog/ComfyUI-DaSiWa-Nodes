"""The Prompt Editor encodes its box, and a new writer prompt replaces the box."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nodes import nodes_llm_prompt_editor as editor

Node = editor.DaSiWa_LLMPromptEditor


class FakeClip:
    def __init__(self):
        self.encoded = []

    def tokenize(self, text):
        return {"text": text}

    def encode_from_tokens_scheduled(self, tokens):
        self.encoded.append(tokens["text"])
        return [["cond", {"text": tokens["text"]}]]


class FakeDynPrompt:
    def __init__(self, inputs):
        self.inputs = inputs

    def get_node(self, unique_id):
        return {"inputs": self.inputs}


def pnginfo(seen, node_id=7):
    props = {} if seen is None else {editor.LAST_SEEN: seen}
    return {"workflow": {"nodes": [{"id": node_id, "properties": props}]}}


def run(prompt, llm_prompt=None, seen=None, prompt_edit=False):
    clip = FakeClip()
    out = Node().encode(clip, prompt, prompt_edit, llm_prompt=llm_prompt, unique_id="7", extra_pnginfo=pnginfo(seen))
    return out, clip


def test_schema():
    schema = Node.INPUT_TYPES()
    assert list(schema["required"]) == ["clip", "prompt", "prompt_edit"]
    assert schema["required"]["prompt_edit"][1]["default"] is False
    assert schema["optional"]["llm_prompt"][1]["lazy"] is True
    assert schema["optional"]["llm_prompt"][1]["forceInput"] is True
    assert schema["required"]["prompt"][1]["dynamicPrompts"] is False
    assert Node.RETURN_TYPES == ("CONDITIONING",)


def test_new_writer_prompt_replaces_the_box():
    out, clip = run("my old edit", llm_prompt="a fresh prompt", seen="an older prompt")
    assert clip.encoded == ["a fresh prompt"]
    assert out["ui"] == {"prompt": ["a fresh prompt"], "llm_prompt": ["a fresh prompt"]}


def test_first_writer_prompt_replaces_the_box():
    out, clip = run("", llm_prompt="first prompt", seen=None)
    assert clip.encoded == ["first prompt"]
    assert out["ui"]["prompt"] == ["first prompt"]


def test_cached_writer_prompt_keeps_the_edit():
    out, clip = run("first prompt, edited", llm_prompt="first prompt", seen="first prompt")
    assert clip.encoded == ["first prompt, edited"]
    # Nothing comes back for the box, so typing during the run survives.
    assert out["ui"] == {"prompt": [], "llm_prompt": ["first prompt"]}


def test_no_writer_text_encodes_the_box_and_keeps_last_seen():
    out, clip = run("typed by hand", llm_prompt=None, seen="first prompt", prompt_edit=True)
    assert clip.encoded == ["typed by hand"]
    assert out["ui"] == {"prompt": [], "llm_prompt": ["first prompt"]}


def test_box_is_encoded_exactly_as_typed():
    out, clip = run("  (red:1.2), {a|b}\n", seen=None)
    assert clip.encoded == ["  (red:1.2), {a|b}\n"]
    assert out["ui"] == {"prompt": []}


def test_missing_clip_is_a_clear_error():
    with pytest.raises(RuntimeError, match="clip input is invalid"):
        Node().encode(None, "x", False)


@pytest.mark.parametrize("prompt_edit,box,linked,wanted", [
    (False, "", True, ["llm_prompt"]),
    (False, "edited", True, ["llm_prompt"]),
    (True, "edited", True, []),
    (True, "   ", True, ["llm_prompt"]),
    (False, "", False, []),
    (True, "", False, []),
])
def test_lazy_status(prompt_edit, box, linked, wanted):
    inputs = {"prompt": box, "prompt_edit": prompt_edit}
    if linked:
        inputs["llm_prompt"] = ["3", 0]
    got = Node().check_lazy_status(box, prompt_edit, dynprompt=FakeDynPrompt(inputs), unique_id="7")
    assert got == wanted


def test_constant_llm_prompt_is_not_requested():
    # Only a link can be requested; ComfyUI raises on a constant.
    dyn = FakeDynPrompt({"llm_prompt": "typed"})
    assert Node().check_lazy_status("", False, dynprompt=dyn, unique_id="7") == []


def test_last_seen_inside_a_subgraph():
    info = {"workflow": {"nodes": [{"id": 5, "properties": {}}],
                         "definitions": {"subgraphs": [{"nodes": [{"id": 5, "properties": {editor.LAST_SEEN: "inner"}}]}]}}}
    assert editor.last_seen(info, "12:5") == "inner"
    assert editor.last_seen(info, "5") is None
    assert editor.last_seen(None, "5") is None
    assert editor.last_seen({"workflow": {"nodes": [{"id": 5, "properties": {editor.LAST_SEEN: 3}}]}}, "5") is None
