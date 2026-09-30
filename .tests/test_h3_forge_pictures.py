"""H3 Forge: subject groups, picture scanning and the descriptions it reuses."""
from nodes import h3_forge as forge


def pic(path, group="", **extra):
    ref = {"kind": "image", "role": "subject", "path": path, **extra}
    if group:
        ref["subject_group"] = group
    return ref


FIVE = [pic("a.png", "A"), pic("square.png"), pic("b.png", "A"), pic("other.png"), pic("c.png", "A")]
READER = {"system": "READER", "temperature": 0.2, "rescan_temperature": 0.6, "max_tokens": 320}


class FakeBackend:
    """Records every chat call and answers from a queue."""
    kind = "ollama"
    base = "http://example.invalid"
    labels_pictures = True

    def __init__(self, replies):
        self.replies, self.calls, self.unloads = list(replies), [], 0

    def can_see(self, name):
        return True

    def loaded(self):
        return set()

    def unload(self, name):
        self.unloads += 1
        return True

    def chat(self, name, system, user, images_b64, sampling, num_ctx, timeout, cancel=None,
             image_labels=None, keep_loaded=False, max_tokens=None):
        self.calls.append({"system": system, "user": user, "images": list(images_b64 or []), "labels": image_labels,
                           "keep_loaded": keep_loaded, "max_tokens": max_tokens, "temperature": sampling.get("temperature")})
        return self.replies.pop(0), {"prompt_tokens": 1, "output_tokens": 1}


def test_only_explicit_groups_combine_subject_pictures():
    assert forge.picture_groups(FIVE) == [[1, 3, 5]]
    assert forge.picture_groups([pic("a.png", "A"), pic("b.png")]) == []
    # A keyframe or style picture never joins a subject group.
    assert forge.picture_groups([pic("a.png", "A"), {"kind": "image", "role": "keyframe", "path": "k.png", "subject_group": "A"}]) == []


def test_a_group_gets_one_reference_line_and_every_picture_keeps_its_number():
    lines, pictures = forge.format_references(FIVE, "REF2VA")
    assert len(lines) == 3
    assert lines[0].startswith("- <Picture 1>, <Picture 3>, <Picture 5> · subject — ONE subject shown in three pictures")
    assert lines[1].startswith("- <Picture 2> ·") and lines[2].startswith("- <Picture 4> ·")
    assert [tag for _ref, tag in pictures] == [f"<Picture {n}>" for n in range(1, 6)]


def test_base_modes_never_group():
    lines, _pictures = forge.format_references([pic("a.png", "A"), pic("b.png", "A")], "I2VA")
    assert len(lines) == 2


def test_scan_units_are_a_group_together_and_other_pictures_alone():
    units = forge.reading_units(FIVE, "REF2VA")
    assert [u["labels"] for u in units] == [["<Picture 1>", "<Picture 3>", "<Picture 5>"], ["<Picture 2>"], ["<Picture 4>"]]
    assert [u["pictures"] for u in units] == [[1, 3, 5], [2], [4]]
    assert "same subject" in forge.reading_message(units[0])
    assert "keep: red coat" in forge.reading_message({"role": "subject", "refs": [{"keep": "red coat"}]})


def test_a_description_the_overlay_holds_stands_in_for_a_scan():
    assert forge.given_reading({"refs": [{"reading": " A girl. "}]}) == "A girl."
    assert forge.given_reading({"refs": [{}]}) is None
    # A group counts only when every picture carries the same text.
    assert forge.given_reading({"refs": [{"reading": "A girl."}, {"reading": "A girl."}]}) == "A girl."
    assert forge.given_reading({"refs": [{"reading": "A girl."}, {}]}) is None
    assert forge.usable_reading("UNAVAILABLE") is None
    assert forge.usable_reading("<think>hm</think>A square.") == "A square."


def test_scanning_skips_described_pictures_and_keeps_the_model_loaded():
    refs = [pic("a.png", "A", reading="A girl."), pic("square.png"), pic("b.png", "A", reading="A girl.")]
    units = forge.reading_units(refs, "REF2VA")
    backend = FakeBackend(["A town square."])
    b64 = {"<Picture 1>": "a", "<Picture 2>": "s", "<Picture 3>": "b"}
    readings, scanned = forge.scan_units(backend, "m", units, b64, READER, 8192, 60, None)
    assert readings == {"<Picture 1>, <Picture 3>": "A girl.", "<Picture 2>": "A town square."}
    assert scanned == 1 and len(backend.calls) == 1
    call = backend.calls[0]
    assert call["images"] == ["s"] and call["keep_loaded"] is True and call["max_tokens"] == 320
    assert call["system"] == "READER" and call["temperature"] == 0.2


def test_a_rescan_looks_again_and_runs_looser():
    refs = [pic("a.png", reading="Old."), pic("b.png")]
    units = [u for u in forge.reading_units(refs, "REF2VA") if 1 in u["pictures"]]
    backend = FakeBackend(["New."])
    readings, scanned = forge.scan_units(backend, "m", units, {"<Picture 1>": "a", "<Picture 2>": "b"}, READER, 8192, 60, None, force=True)
    assert readings == {"<Picture 1>": "New."} and scanned == 1
    assert backend.calls[0]["temperature"] == 0.6


_real_bundle = forge.load_bundle


def _bundle():
    bundle = dict(_real_bundle())
    bundle["picture_reader"] = READER
    return bundle


REF_OUTPUT = "\n".join(f"===SEGMENT: {label}===\n{label} body" for label in
                       ["Subject definitions", "Summary", "Retention analysis", "Detailed description", "Soundscape", "Music"])


def _generate(monkeypatch, backend, references, mode="REF2VA"):
    monkeypatch.setattr(forge, "load_bundle", _bundle)
    monkeypatch.setattr(forge, "backends", lambda settings: {"ollama": backend})
    monkeypatch.setattr(forge, "_load_pictures", lambda refs, mode, directory: {
        tag: ref["path"] for ref, tag in forge.format_references(refs, mode)[1] if ref.get("path")})
    return forge.generate({"mode": mode, "brief": "Two girls have tea.", "model": "ollama:m", "duration": 10,
                           "references": references}, input_directory="in")


def test_generate_scans_what_is_missing_then_writes_with_labelled_pictures(monkeypatch):
    refs = [pic("a.png", "A", reading="A girl."), pic("square.png"), pic("b.png", "A", reading="A girl.")]
    backend = FakeBackend(["A town square.", REF_OUTPUT])
    result = _generate(monkeypatch, backend, refs)

    scan, write = backend.calls
    assert scan["system"] == "READER" and scan["images"] == ["square.png"]
    assert write["images"] == ["a.png", "square.png", "b.png"]
    assert write["labels"] == ["<Picture 1>", "<Picture 2>", "<Picture 3>"]
    assert "<Picture 1>, <Picture 3>: A girl." in write["user"]
    assert "<Picture 2>: A town square." in write["user"]
    assert "each one right after its label" in write["user"]
    assert write["keep_loaded"] is False
    assert result["readings"] == [
        {"labels": "<Picture 1>, <Picture 3>", "pictures": [1, 3], "text": "A girl."},
        {"labels": "<Picture 2>", "pictures": [2], "text": "A town square."},
    ]
    assert backend.unloads == 1


def test_the_context_grows_to_fit_the_pictures_and_the_answer():
    # A text-only prompt keeps the bundle's window.
    assert forge.context_for(16384, 22000, 0) == 16384
    # REF2VA's instructions with five pictures do not fit 16k with room to answer.
    assert forge.context_for(16384, 40000, 5) == 24576
    # Never past the ceiling, whatever is asked.
    assert forge.context_for(16384, 400000, 9) == forge.CONTEXT_MAX


def test_the_scans_and_the_write_share_one_context(monkeypatch):
    seen = []
    backend = FakeBackend(["A town square.", REF_OUTPUT])
    real = backend.chat
    backend.chat = lambda name, system, user, images, sampling, num_ctx, *a, **k: (seen.append(num_ctx), real(name, system, user, images, sampling, num_ctx, *a, **k))[1]
    refs = [pic("a.png", "A", reading="A girl."), pic("square.png"), pic("b.png", "A", reading="A girl.")]
    result = _generate(monkeypatch, backend, refs)
    assert len(set(seen)) == 1 and seen[0] >= 16384
    assert result["stats"]["num_ctx"] == seen[0]


def test_one_picture_is_not_scanned(monkeypatch):
    backend = FakeBackend([REF_OUTPUT])
    result = _generate(monkeypatch, backend, [pic("a.png")])
    assert len(backend.calls) == 1 and result["readings"] == []


def test_scan_route_returns_descriptions_and_unloads(monkeypatch):
    monkeypatch.setattr(forge, "load_bundle", _bundle)
    backend = FakeBackend(["A girl.", "A town square."])
    monkeypatch.setattr(forge, "backends", lambda settings: {"ollama": backend})
    monkeypatch.setattr(forge, "_load_pictures", lambda refs, mode, directory: {
        tag: ref["path"] for ref, tag in forge.format_references(refs, mode)[1]})
    result = forge.scan({"model": "ollama:m", "references": [pic("a.png", "A"), pic("square.png"), pic("b.png", "A")]}, input_directory="in")
    assert [r["pictures"] for r in result["readings"]] == [[1, 3], [2]]
    assert result["scanned"] == 2 and backend.unloads == 1
    assert backend.calls[0]["images"] == ["a.png", "b.png"]

    # A rescan of one picture touches only the unit that holds it.
    backend2 = FakeBackend(["A plaza."])
    monkeypatch.setattr(forge, "backends", lambda settings: {"ollama": backend2})
    again = forge.scan({"model": "ollama:m", "pictures": [2], "force": True,
                        "references": [pic("a.png", "A", reading="A girl."), pic("square.png", reading="Old."), pic("b.png", "A", reading="A girl.")]},
                       input_directory="in")
    assert again["readings"] == [{"labels": "<Picture 2>", "pictures": [2], "text": "A plaza."}]
