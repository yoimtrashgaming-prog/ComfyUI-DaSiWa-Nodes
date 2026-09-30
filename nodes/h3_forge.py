"""MiniMax H3 Forge: write a Director prompt with a local LLM before the run.

The Forge button on the H3 Director opens an overlay; this module is the
server half. It runs outside the ComfyUI queue on purpose: the prompt is
written and reviewed first, the LLM is unloaded, and only then does the person
press Run. The LLM and the video model are never resident together.

The prompting is PromptForge's h3 prompting, exported by its
scripts/export-h3-forge.mjs into data/h3_forge.json. Every system prompt and
every ladder rule comes from that bundle; this file only assembles the user
message, calls the model, and splits its ===SEGMENT: output into the node's
builder fields. Prompt-only: no tool calls.

Models come from ComfyUI/models/llm (loaded in-process with nodes_llm.py's
loaders), an Ollama server, or an OpenAI-compatible server - see Backends.
"""

import asyncio
import base64
import io
import json
import os
import re
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

try:
    from .helper_logging import log_dasiwa
except ImportError:  # pragma: no cover - direct test import
    from helper_logging import log_dasiwa

BUNDLE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "h3_forge.json")
BASE_MODES = ("T2VA", "I2VA", "FL2VA", "L2VA")
IMAGE_MAX_EDGE = 1024
# Seven thousand characters is the H3 prompt ceiling. English prose runs
# 3.5-4 characters a token, so that is about 2,000 tokens with the segment
# markers; a complete REF2VA prompt measured 897 (27B) and 1,386 (9B) on
# 30 Sep 2026. 3,500 allowed twice H3's limit, and a model that had lost the
# thread used all of it. This is a ceiling, not a target: it cuts a runaway
# off sooner, and the reply is rejected either way.
NUM_PREDICT = 2500


_bundle_cache = {"mtime": None, "data": None}


def load_bundle(path=BUNDLE_PATH):
    mtime = os.path.getmtime(path)
    if _bundle_cache["mtime"] != mtime:
        with open(path, "r", encoding="utf-8") as fh:
            _bundle_cache["data"] = json.load(fh)
        _bundle_cache["mtime"] = mtime
    return _bundle_cache["data"]


def title_case(value):
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), str(value).replace("_", " "))


# ── References: a port of PromptForge's server/references.mjs ─────────────

_ROLE_NOTE = {
    "keyframe": lambda l: f"keyframe — {l} IS a frame of the video: give it its own line in subject_definitions and retention_analysis, naming the shot and moment it anchors",
    "motion": lambda l: f"motion — {l} gives structure only: pacing, cuts, camera",
    "subject": lambda l: f"subject — define a <Subject N> from it and cite {l} as its source; no standalone {l} line",
    "style": lambda l: f"style — rendering only, not its content: define a style <Subject N> citing {l}; no standalone {l} line",
}
_BASE_NOTE = {
    "keyframe": lambda l: f"keyframe — {l} is a frame of the video; describe it where it appears",
    "subject": lambda l: f"subject — who or what appears, taken from {l}",
    "style": lambda l: f"style — rendering only, taken from {l}",
}
_KIND_LABEL = {"image": "Picture", "video": "Video", "audio": "Audio"}
_STREAM_EMITS = {"video": ["Video"], "audio": ["Audio"], "both": ["Video", "Audio"]}


def _format_duration(seconds):
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    if s < 60:
        return f"{s:.1f}s"
    mins = int(s // 60)
    return f"{mins}m {round(s - mins * 60)}s"


def picture_groups(references):
    """Pictures the person marked as one subject: [[1, 3, 5], ...] by picture number.

    Explicit subject identity from the Group picker, never a guess from the
    idea. Only subject pictures group, and a group needs two members.
    """
    by_id, number = {}, 0
    for ref in references:
        if ref.get("kind") != "image":
            continue
        number += 1
        group = ref.get("subject_group")
        if (ref.get("role") or "subject") != "subject" or not isinstance(group, str) or not group:
            continue
        by_id.setdefault(group, []).append(number)
    return [pictures for pictures in by_id.values() if len(pictures) >= 2]


_COUNT_WORD = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def _group_line(pictures, references):
    """The one reference line a subject group gets."""
    tags = [f"<Picture {n}>" for n in pictures]
    count = _COUNT_WORD[len(tags)] if len(tags) < len(_COUNT_WORD) else str(len(tags))
    every = "both" if len(tags) == 2 else f"all {count}"
    bits = [", ".join(tags),
            f"subject — ONE subject shown in {count} pictures: define a single <Subject N> citing {every}; no standalone picture lines"]
    images = [r for r in references if r.get("kind") == "image"]
    for n in pictures:
        ref = images[n - 1]
        if ref.get("keep"):
            bits.append(f"keep (<Picture {n}>): {ref['keep']}")
        if ref.get("drop"):
            bits.append(f"drop (<Picture {n}>): {ref['drop']}")
    return "- " + " · ".join(bits)


def format_references(references, mode):
    """Director label lines for each reference, and the labels of the pictures."""
    counters = {"Picture": 0, "Video": 0, "Audio": 0}
    lines, pictures = [], []
    base_mode = mode in BASE_MODES
    grouped = {}
    if mode == "REF2VA":
        for group in picture_groups(references):
            for n in group:
                grouped[n] = group
    for ref in references:
        kind = ref.get("kind")
        labels = _STREAM_EMITS.get(ref.get("stream"), ["Video"]) if kind == "video" else [_KIND_LABEL.get(kind, "Picture")]
        video_index = counters["Video"] + 1 if "Video" in labels else None
        for n, label in enumerate(labels):
            counters[label] += 1
            tag = f"<{label} {counters[label]}>"
            first = n == 0
            if first and kind == "image":
                pictures.append((ref, tag))
                group = grouped.get(counters["Picture"])
                if group:
                    # One line for the whole group, where its first picture sits.
                    if counters["Picture"] == group[0]:
                        lines.append(_group_line(group, references))
                    continue
            bits = [tag]
            if label == "Audio" and kind == "video" and video_index:
                bits.append(f"synchronized audio track of <Video {video_index}>")
            elif label == "Audio" and kind == "video":
                bits.append("audio track only; the video picture is not referenced")
            elif label == "Audio":
                bits.append("voice — audio signal")
            else:
                role = ref.get("role")
                note = (_BASE_NOTE.get(role) if base_mode else None) or _ROLE_NOTE.get(role)
                bits.append(note(tag) if note else f"role: {role or 'UNLABELLED'}")
            length = _format_duration(ref.get("duration_seconds"))
            if length:
                bits.append(f"length: {length}")
            if first and ref.get("keep"):
                bits.append(f"keep: {ref['keep']}")
            if first and ref.get("drop"):
                bits.append(f"drop: {ref['drop']}")
            lines.append("- " + " · ".join(bits))
    return lines, pictures


# ── Picture descriptions: a port of PromptForge's server/picture-readings.mjs ──
#
# REF2VA with two or more pictures: the model looks at each picture on its own
# (a subject group together) and writes a short description, and the prompt is
# written from those plus the labelled pictures. With several pictures in one
# call, models lost track of which picture was which (PromptForge, 29 Sep 2026:
# an 8B described the background picture as a second copy of the character).
# The instructions come from the bundle (picture_reader), so both apps scan
# with the same words.

_ROLE_HINT = {
    "subject": "It shows a subject for the video: a person, creature, object or place that should appear in it.",
    "style": "It is a style reference: only how it is rendered matters, not what it shows.",
    "keyframe": "It is a keyframe: an exact frame the video will show at some point.",
}
READING_MAX = 2000
SCAN_TOKENS = 320

# Room for the write. The bundle's context is sized for a text-only prompt;
# REF2VA's instructions with five pictures measured 14,561 prompt tokens
# (30 Sep 2026, qwen3.5:9b), which left 1,800 of a 16,384 window for an answer
# that needs up to NUM_PREDICT - the model ran out and wrote word salad. A
# 1024 px picture costs roughly a thousand tokens.
PICTURE_TOKENS = 1100
CHARS_PER_TOKEN = 3.5
CONTEXT_STEP = 8192
CONTEXT_MAX = 40960


def context_for(base, text_chars, pictures):
    """The context window a write needs: the bundle's, or the next step up that fits."""
    need = int(text_chars / CHARS_PER_TOKEN) + pictures * PICTURE_TOKENS + NUM_PREDICT + 512
    ctx = int(base)
    while ctx < need and ctx < CONTEXT_MAX:
        ctx += CONTEXT_STEP
    return ctx


def reading_units(references, mode="REF2VA"):
    """What to scan, in Director order: each picture on its own, a group together.

    [{"labels": ["<Picture 1>", ...], "pictures": [1, ...], "refs": [ref, ...], "role": str}],
    only for pictures that have a file.
    """
    _lines, pictures = format_references(references, mode)
    numbered = [(i + 1, ref, tag) for i, (ref, tag) in enumerate(pictures)]
    group_of = {}
    if mode == "REF2VA":
        for group in picture_groups(references):
            for n in group:
                group_of[n] = group
    units, done = [], set()
    for n, ref, _tag in numbered:
        if n in done or not ref.get("path"):
            continue
        members = [m for m in numbered if m[0] in group_of[n] and m[1].get("path")] if n in group_of else [(n, ref, _tag)]
        done.update(m[0] for m in members)
        units.append({
            "labels": [m[2] for m in members],
            "pictures": [m[0] for m in members],
            "refs": [m[1] for m in members],
            "role": members[0][1].get("role") or "subject",
        })
    return units


def reading_message(unit):
    """What the scanner is told about a unit. Not the idea: a scanner told the
    story reports it back as something it saw."""
    lines = [_ROLE_HINT.get(unit["role"], _ROLE_HINT["subject"])]
    count = len(unit["refs"])
    if count > 1:
        lines.append(f"These {count} pictures show the same subject. Describe it once, as one subject, "
                     "and say briefly what differs between the pictures (outfit, pose, framing).")
    keep = [str(r.get("keep") or "").strip() for r in unit["refs"]]
    drop = [str(r.get("drop") or "").strip() for r in unit["refs"]]
    keep, drop = list(dict.fromkeys(k for k in keep if k)), list(dict.fromkeys(d for d in drop if d))
    if keep:
        lines.append(f"The person wants to keep: {'; '.join(keep)}.")
    if drop:
        lines.append(f"The person wants to leave out: {'; '.join(drop)}.")
    return "\n".join(lines)


def given_reading(unit):
    """The description the overlay already holds for a unit, or None.

    Scanned earlier or written by the person, it stands in for a scan. A group
    counts only when every picture in it carries the same text.
    """
    texts = [str(r.get("reading") or "").strip() for r in unit["refs"]]
    if not texts or any(not t or t != texts[0] for t in texts):
        return None
    return texts[0][:READING_MAX]


def usable_reading(text):
    """A description, or None. UNAVAILABLE is the scanner saying it saw nothing."""
    t = _THINK.sub("", str(text or "")).strip()
    if not t or re.fullmatch(r"unavailable\.?", t, re.I):
        return None
    return t


def reading_lines(units, readings):
    """The descriptions for the prompt writer, under the Director's labels."""
    kept = [(u, readings.get(", ".join(u["labels"]))) for u in units]
    kept = [(u, text) for u, text in kept if text]
    if not kept:
        return []
    lines = ["", "What is in each picture. Each was looked at on its own, so these say which picture shows what. "
                 "Cite every picture by exactly the label given here. These are notes to you: do not copy their wording."]
    lines += [f"{', '.join(u['labels'])}: {text}" for u, text in kept]
    return lines


# ── The user message: the h3 path of PromptForge's buildUserMessage ───────

# PromptForge's h3 detail ladder is written for a ~10 s clip (see its
# config/models.yaml). Forge scales the word range to the clip actually
# set on the node, so Detail 6 on a 5 s clip asks for half the words instead
# of cramming a 10 s clip's worth of shots into it. Only the first "N-M words"
# is the ladder's own number; "350-500 word range" later is a quote of
# MiniMax's guide and stays.
LADDER_SECONDS = 10
# ~5,000 characters of description, leaving room for the soundscape and music
# inside H3's 7,000-character prompt.
MAX_DESCRIPTION_WORDS = 800
_WORD_RANGE = re.compile(r"(\d+)-(\d+) words")


def scale_detail_rule(rule, duration):
    try:
        factor = float(duration) / LADDER_SECONDS
    except (TypeError, ValueError):
        return rule
    if factor <= 0 or abs(factor - 1) < 0.05:
        return rule

    def scaled(m):
        lo, hi = (max(10, int(round(int(n) * factor / 5.0) * 5)) for n in m.groups())
        hi = min(max(hi, lo + 10), MAX_DESCRIPTION_WORDS)
        return f"{min(lo, hi - 10)}-{hi} words"

    out = _WORD_RANGE.sub(scaled, rule, count=1)
    # Level 7 calls its range the guide's own, which stops being true once scaled.
    return out.replace(", the reference guide's own range", f" for this {float(duration):g}-second clip")


def build_user_message(bundle, brief, mode, duration, detail, creativity, references, carries_image,
                       picture_lines=(), labelled=False):
    lines = [f'Brief: "{str(brief).strip()}"']
    settings = [f"Creativity: {title_case(creativity)}", f"Mode: {mode}"]
    if duration:
        settings.append(f"Duration: {duration} sec")
    lines.append(f"Settings (context for how to write, never text to include): {' · '.join(settings)}")

    preset = bundle["creativity_presets"].get(creativity)
    if preset and preset.get("rule"):
        lines.append(f"Creativity - {title_case(creativity)}. {preset['rule']}")
        if any(r.get("kind") == "image" for r in references):
            lines.append(
                "A reference picture is attached. What it supplies, in the role it was given, stays exactly as "
                "the picture shows it at every Creativity setting. Creativity decides only what happens - the "
                "action, the camera, the cuts and the sound - never what is already in the picture."
            )

    table = bundle["detail_levels"]
    entry = table.get(str(detail)) or table.get(str(bundle["default_detail"]))
    if entry and entry.get("rule"):
        level = detail if str(detail) in table else bundle["default_detail"]
        lines.append(f"Detail level {level} of {len(table)} - {entry.get('label', level)}. {scale_detail_rule(entry['rule'], duration)}")

    if references:
        ref_lines, pictures = format_references(references, mode)
        lines += ["", "References:", *ref_lines, *picture_lines]
        labels = [tag for _ref, tag in pictures]
        if carries_image and labels:
            # Backends that can put a label beside each picture do (labelled);
            # the rest rely on the order.
            how = "each one right after its label" if labelled else "in that order"
            lines.append("")
            lines.append(
                f"The picture for {labels[0]} is attached to this message. Look at it — the line above says what it is FOR, the picture says what is in it."
                if len(labels) == 1 else
                f"The pictures for {', '.join(labels)} are attached to this message, {how}. Look at them — the lines above say what each one is FOR, the pictures say what is in them."
            )
    return "\n".join(lines)


# ── Output: the ===SEGMENT: contract ──────────────────────────────────────

_DELIMITER = re.compile(r"^===SEGMENT:\s*(.+?)\s*===\s*$", re.M)
# Qwen3-family models may still emit a think block even with think off.
_THINK = re.compile(r"<think>.*?</think>", re.S)


class ForgeError(Exception):
    def __init__(self, code, message, raw=None):
        super().__init__(message)
        self.code, self.message, self.raw = code, message, raw


def parse_segments(text, expected):
    raw = _THINK.sub("", str(text or "")).strip()
    matches = list(_DELIMITER.finditer(raw))
    if not matches:
        raise ForgeError("no_segments", "The model returned no ===SEGMENT: markers — it did not follow the output format. Try a larger model.", raw)
    segments = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        segments[m.group(1).strip()] = raw[m.end():end].strip()
    if "Refused" in segments:
        raise ForgeError("refused", f"The model refused: {segments['Refused']}", raw)
    missing = [label for label in expected if label not in segments]
    if missing:
        raise ForgeError("missing_segments", f"The model left out: {', '.join(missing)}.", raw)
    return segments


def _bare(body, names):
    alts = "|".join(re.escape(n) for n in names if n)
    return re.sub(rf"^\s*(?:{alts})\s*:\s*", "", str(body or ""), flags=re.I).strip()


_REF_FIELDS = [
    ("Subject definitions", "subject_definitions"),
    ("Summary", "summary"),
    ("Retention analysis", "retention_analysis"),
    ("Detailed description", "detailed_description"),
    ("Soundscape", "soundscape"),
    ("Music", "music"),
]
_OFFICIAL = {"Soundscape": "overall_soundscape", "Music": "non_diegetic_music",
             "Detailed description": "integrated_multimodal_description"}


def builder_fields(segments, mode):
    """Segment bodies as the node's builder_state fields."""
    def value(label, *extra):
        return _bare(segments.get(label), [label, _OFFICIAL.get(label), *extra])
    if mode == "REF2VA":
        return {"ref": {key: value(label, key, "overall_soundscape" if key == "soundscape" else None,
                                   "detailed_description" if key == "detailed_description" else None)
                        for label, key in _REF_FIELDS}}
    return {
        "imd": value("Detailed description"),
        "soundscape": value("Soundscape"),
        "music": value("Music"),
    }


# ── Simple prompt mode: a port of PromptForge's server/h3-simple.mjs ──────

_TIMESTAMP = re.compile(r"\b(\d{1,2}):(\d{2})(?:\.(\d+))?\b")


def check_prompt(fields, mode, duration, prompt_text, limit):
    """Mechanical checks on a finished prompt. Warnings, never repairs."""
    warnings = []
    description = fields["ref"]["detailed_description"] if mode == "REF2VA" else fields["imd"]
    try:
        clip = float(duration)
    except (TypeError, ValueError):
        clip = None
    if clip:
        stamps = [int(m) * 60 + int(s) + float(f"0.{frac}" if frac else 0)
                  for m, s, frac in _TIMESTAMP.findall(description)]
        if stamps and max(stamps) >= clip:
            warnings.append(f"Shots run to {max(stamps):g}s but the clip is {clip:g}s. Regenerate, or fix the timestamps.")
    if len(prompt_text) > limit:
        warnings.append(f"{len(prompt_text):,} characters; H3 takes {limit:,}. Lower Detail and regenerate.")
    return warnings


def _snapped_seconds_text(seconds):
    try:
        n = float(seconds)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    frames = max(5, int(n * 24))
    while frames % 17 != 5:
        frames += 1
    return f"{round(frames / 24, 2):.2f}"


def _last_shot(description):
    shots = [int(n) for n in re.findall(r"\[Shot\s+(\d+)\]", str(description or ""), re.I)]
    return max(shots) if shots else 1


def _alignment_line(mode, duration, shot):
    if mode == "I2VA":
        return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
    if mode not in ("FL2VA", "L2VA"):
        return ""
    s = _snapped_seconds_text(duration)
    if s is None:
        return ""
    if mode == "FL2VA":
        return ("How the reference pictures align with the target video — "
                "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
                f"Picture 2 (from Shot {shot}) aligns with the {s}-second mark of the target video.")
    return ("How the reference pictures align with the target video — "
            f"<Picture 1> (from [Shot {shot}]) aligns with the {s}-second mark of the target video.")


def simple_prompt(fields, mode, duration):
    if mode == "REF2VA":
        ref = fields["ref"]
        names = [("subject_definitions", "subject_definitions"), ("summary", "summary"),
                 ("retention_analysis", "retention_analysis"), ("detailed_description", "detailed_description"),
                 ("soundscape", "overall_soundscape"), ("music", "non_diegetic_music")]
        return "\n\n".join(f"{name}:\n{ref[key] or ('N/A' if key == 'music' else '')}" for key, name in names)
    body = "\n\n".join([
        f"integrated_multimodal_description: {fields['imd']}",
        f"overall_soundscape: {fields['soundscape']}",
        f"non_diegetic_music: {fields['music'] or 'N/A'}",
    ])
    head = _alignment_line(mode, duration, _last_shot(fields["imd"]))
    return f"{head}\n\n{body}" if head else body


# ── Backends ──────────────────────────────────────────────────────────────
#
# Three sources, one picker. A model id is "<source>:<name>":
#   ollama:<name>   an Ollama server, local by default or wherever Settings says
#   openai:<name>   any OpenAI-compatible server (llama.cpp server, llama-swap,
#                   LM Studio, koboldcpp), only when Settings names one
#   local:<name>    a file or folder in ComfyUI/models/llm, loaded in-process
#                   with the pack's own loaders from nodes_llm.py
#
# Server addresses come from ComfyUI's Settings panel, never from the
# workflow: a downloaded workflow must not be able to point this machine at a
# server of its choosing (see the security note on nodes_llm.py's history).

DEFAULT_OLLAMA = "http://127.0.0.1:11434"


def _base_url(value, default=""):
    url = str(value or default).strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://[^\s/]+", url):
        raise ForgeError("bad_url", f"Server address must start with http:// or https://, got {url!r}.")
    return url


def _is_this_machine(url):
    host = re.sub(r"^https?://", "", url).split("/")[0].rsplit(":", 1)[0].strip("[]").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _http(url, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


CANCELLED = "Cancelled. Nothing was applied to the node."


def _stream_lines(url, payload, timeout, cancel):
    """POST and yield the response line by line, stopping when Cancel is pressed.

    Leaving the `with` closes the connection, which is what makes a server
    stop: Ollama and llama.cpp both abort a generation whose client is gone.
    The check runs between lines, so a cancel lands at the next token - or,
    during the prompt read before the first token, as soon as one arrives.
    """
    req = urlrequest.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            if cancel is not None and cancel.is_set():
                raise ForgeError("cancelled", CANCELLED)
            line = raw.decode("utf-8", "replace").strip()
            if line:
                yield line


def _image_b64(path):
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


class Ollama:
    kind = "ollama"

    def __init__(self, base):
        self.base = base

    def models(self):
        out = []
        for m in _http(self.base + "/api/tags").get("models", []):
            details = m.get("details") or {}
            # Embedding models (nomic-embed-text and the like: BERT family)
            # cannot write, so they are not offered.
            families = " ".join([details.get("family") or "", *(details.get("families") or [])]).lower()
            if "bert" in families or "embed" in m["name"].lower():
                continue
            params = details.get("parameter_size")
            out.append({"id": f"ollama:{m['name']}", "label": f"{m['name']}{f' ({params})' if params else ''}"})
        return out

    def can_see(self, name):
        try:
            return "vision" in (_http(self.base + "/api/show", {"model": name}).get("capabilities") or [])
        except Exception:
            return False

    def loaded(self):
        try:
            return {m["name"] for m in _http(self.base + "/api/ps").get("models", [])}
        except Exception:
            return set()

    def unload(self, name):
        try:
            _http(self.base + "/api/generate", {"model": name, "keep_alive": 0}, timeout=30)
        except Exception as exc:
            log_dasiwa("H3 Forge", f"unload of {name} failed: {exc}")
        return name not in self.loaded()

    labels_pictures = True

    def chat(self, name, system, user, images_b64, sampling, num_ctx, timeout, cancel=None,
             image_labels=None, keep_loaded=False, max_tokens=None):
        message = {"role": "user", "content": user}
        messages = [{"role": "system", "content": system}, message]
        if images_b64:
            message["images"] = images_b64
            if image_labels and len(image_labels) == len(images_b64):
                # Ollama's chat cannot mix text and pictures inside a message,
                # so the labels go as lines under the text, in picture order -
                # the layout PromptForge's Ollama path sends. One message, not
                # one per picture: split up, qwen3.5:9b rambled to the token
                # limit and lost the last sections (0 of 10, 30 Sep 2026).
                message["content"] = user + "\n" + "\n".join(f"{label}:" for label in image_labels)
        parts, thought, stats = [], 0, {}
        for line in _stream_lines(self.base + "/api/chat", {
            # A scan is several calls in a row; the model stays in until the
            # caller unloads it. A single write still unloads itself.
            "model": name, "stream": True, "think": False, "keep_alive": "10m" if keep_loaded else 0,
            "messages": messages,
            "options": {"num_ctx": num_ctx, "num_predict": max_tokens or NUM_PREDICT,
                        "temperature": sampling.get("temperature", 0.7), "top_p": sampling.get("top_p", 0.8)},
        }, timeout, cancel):
            chunk = json.loads(line)
            if chunk.get("error"):
                raise ForgeError("backend", f"Ollama: {chunk['error']}")
            parts.append((chunk.get("message") or {}).get("content") or "")
            thought += len((chunk.get("message") or {}).get("thinking") or "")
            if chunk.get("done"):
                stats = {"prompt_tokens": chunk.get("prompt_eval_count"), "output_tokens": chunk.get("eval_count")}
        text = "".join(parts)
        # Thinking models (e.g. Ollama's plain "qwen3-vl:8b" tag) ignore
        # think=false and can spend the whole budget reasoning, answering nothing.
        if not text.strip() and thought:
            raise ForgeError("thinking_only", f"{name} spent its whole reply thinking and wrote no prompt. "
                             "Use a non-thinking (instruct) model instead, e.g. qwen3-vl:8b-instruct.")
        return text, stats


class OpenAICompatible:
    kind = "openai"

    def __init__(self, base):
        self.base = base
        self.api = base if base.endswith("/v1") else base + "/v1"
        self.root = self.api[: -len("/v1")]

    def models(self):
        return [{"id": f"openai:{m['id']}", "label": m["id"]} for m in _http(self.api + "/models").get("data", [])]

    def can_see(self, name):
        # No standard capability endpoint; the request itself is the test.
        return None

    def loaded(self):
        return set()

    def unload(self, name):
        # llama-swap has a per-model unload; a plain llama.cpp server or LM
        # Studio holds its model for the life of the process.
        try:
            _http(f"{self.root}/api/models/unload/{urlparse.quote(name, safe='')}", {}, timeout=30)
            return True
        except Exception:
            return False

    labels_pictures = True

    def chat(self, name, system, user, images_b64, sampling, num_ctx, timeout, cancel=None,
             image_labels=None, keep_loaded=False, max_tokens=None):
        content = user
        if images_b64:
            labelled = image_labels and len(image_labels) == len(images_b64)
            content = [{"type": "text", "text": user}]
            for i, b in enumerate(images_b64):
                if labelled:
                    content.append({"type": "text", "text": f"{image_labels[i]}:"})
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}})
        parts, thought, usage = [], 0, {}
        for line in _stream_lines(self.api + "/chat/completions", {
            "model": name, "stream": True, "stream_options": {"include_usage": True}, "max_tokens": max_tokens or NUM_PREDICT,
            "temperature": sampling.get("temperature", 0.7), "top_p": sampling.get("top_p", 0.8),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            # llama.cpp server honours this; others ignore unknown fields.
            "chat_template_kwargs": {"enable_thinking": False},
        }, timeout, cancel):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                parts.append(delta.get("content") or "")
                thought += len(delta.get("reasoning_content") or "")
        text = "".join(parts)
        if not text.strip() and thought:
            raise ForgeError("thinking_only", f"{name} spent its whole reply thinking and wrote no prompt. "
                             "Use a non-thinking (instruct) model instead, or turn thinking off in your server.")
        return text, {"prompt_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}


class _NoGqaWithoutFlash:
    """Keep transformers off PyTorch's math attention kernel while Forge generates.

    For grouped-query models (Qwen3, Qwen3-VL) with no mask, transformers passes
    enable_gqa=True to SDPA, trusting the flash kernel to take it. Where PyTorch
    has no flash kernel - the Windows builds - SDPA falls back to the math
    kernel, which materialises the whole attention matrix. Measured 24 Sep
    2026, torch 2.12+cu130 on a 5080, one layer at 8k tokens: 18.9 GiB and
    1.66 s, against 0.23 GiB and 0.016 s with the key/value heads repeated
    first. On a 9k-token REF2VA prompt that spilled a 4B model into shared
    system memory and took minutes.

    Scoped to Forge's own generate call, and a no-op wherever flash exists.
    """

    def __enter__(self):
        self._saved = None
        try:
            import torch
            from transformers.integrations import sdpa_attention
            if torch.cuda.is_available() and not torch.backends.cuda.is_flash_attention_available():
                self._saved = sdpa_attention.use_gqa_in_sdpa
                # Any signature: transformers added `value` in newer releases
                # (0440994), and the answer here never depends on the arguments.
                sdpa_attention.use_gqa_in_sdpa = lambda *args, **kwargs: False
        except Exception:
            self._saved = None
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            from transformers.integrations import sdpa_attention
            sdpa_attention.use_gqa_in_sdpa = self._saved
        return False


def _half_dtype():
    try:
        import torch
        if torch.cuda.is_available() and torch.cuda.is_bf16_supported():
            return "bfloat16"
    except Exception:
        pass
    return "float16"


class Local:
    """ComfyUI/models/llm, through nodes_llm.py's own loaders."""
    kind = "local"

    def _llm(self):
        from . import nodes_llm
        return nodes_llm

    def models(self):
        llm = self._llm()
        try:
            import llama_cpp  # noqa: F401
            has_llama_cpp = True
        except ImportError:
            has_llama_cpp = False
        out = []
        for name in llm._list_llm_models():
            if name == "None":
                continue
            gguf = name.lower().endswith(".gguf")
            if not gguf and os.path.splitext(name)[1].lower() in (".safetensors", ".bin"):
                continue  # a bare weight file cannot be loaded for chat
            if gguf and not has_llama_cpp:
                out.append({"id": f"local:{name}", "label": f"{name} (GGUF - needs llama-cpp-python installed)", "disabled": True})
            else:
                # "org--Model" is how Hugging Face downloads name folders; the
                # org prefix makes the model hard to find in the list.
                shown = name.split("--", 1)[-1]
                kind = "GGUF" if gguf else "transformers"
                if not gguf and self._compressed_tensors(llm, name):
                    kind += ", FP8 compressed-tensors: very slow in ComfyUI, get the normal version"
                out.append({"id": f"local:{name}", "label": f"{shown} ({kind})"})
        return out

    @staticmethod
    def _compressed_tensors(llm, name):
        """True for an llm-compressor checkpoint (quant_method compressed-tensors).

        Built for vLLM. Under transformers it re-quantizes activations in every
        layer on every token: measured 24 Sep 2026 on a Qwen3-VL-4B FP8 build,
        215 ms per token (~1,500 syncs and 8,400 launches per step) against
        39 ms for the same architecture in plain bf16.
        """
        try:
            path = llm._resolve_model_path(name, "")
            with open(os.path.join(path, "config.json"), "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            return False
        quant = cfg.get("quantization_config") or (cfg.get("text_config") or {}).get("quantization_config") or {}
        return quant.get("quant_method") == "compressed-tensors"

    def can_see(self, name):
        """Vision models carry a vision_config; nodes_llm's llama.cpp path is text-only.

        Asked rather than assumed: a text-only folder handed a picture fails in
        the processor with a technical error, instead of writing from the idea.
        """
        if name.lower().endswith(".gguf"):
            return False
        try:
            path = self._llm()._resolve_model_path(name, "")
            with open(os.path.join(path, "config.json"), "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            return False
        return bool(cfg.get("vision_config"))

    def loaded(self):
        return set()

    def unload(self, name):
        self._llm()._release_all_model_memory()
        return True

    # nodes_llm hands the pictures to the processor as one list, so there is
    # nowhere to put a label beside each; this path relies on their order.
    labels_pictures = False

    def chat(self, name, system, user, images_b64, sampling, num_ctx, timeout, cancel=None,
             image_labels=None, keep_loaded=False, max_tokens=None):
        llm = self._llm()
        gguf = name.lower().endswith(".gguf")
        budget = max_tokens or NUM_PREDICT
        config = {
            "model_path": llm._resolve_model_path(name, "", allow_gguf=gguf),
            "backend": "llama_cpp" if gguf else "transformers",
            "task": "vision" if images_b64 else "text",
            "device": "auto", "dtype": _half_dtype(), "quantization": "none",
            "cache_mode": "unload_after_run", "attention_implementation": "auto",
            "kv_cache_implementation": "default", "kv_cache_quant_backend": "quanto",
            "kv_cache_nbits": 4, "kv_cache_residual_length": 128,
            "llama_n_ctx": num_ctx, "llama_n_gpu_layers": -1, "llama_n_threads": 0, "llama_chat_format": "",
        }
        temperature, top_p = sampling.get("temperature", 0.7), sampling.get("top_p", 0.8)
        loaded = None
        try:
            if gguf:
                loaded = llm._load_llama_cpp_model(config, need_vision=False)
                # Streamed here rather than through _run_llama_cpp_generation so
                # Cancel can stop it between tokens.
                parts = []
                for chunk in loaded.model.create_chat_completion(
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                        max_tokens=budget, temperature=temperature, top_p=top_p, stream=True):
                    if cancel is not None and cancel.is_set():
                        raise ForgeError("cancelled", CANCELLED)
                    parts.append(((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or "")
                text = "".join(parts)
            else:
                pil = []
                if images_b64:
                    from PIL import Image
                    pil = [Image.open(io.BytesIO(base64.b64decode(b))).convert("RGB") for b in images_b64]
                loaded = llm._load_transformers_model(config, need_vision=bool(pil))
                if cancel is not None:
                    # _run_generation calls model.generate itself; hand it a stop
                    # check through that call. This model instance is Forge's own
                    # (unload_after_run), so nothing else sees the wrapper.
                    from transformers import StoppingCriteria, StoppingCriteriaList

                    class _Stop(StoppingCriteria):
                        def __call__(self, input_ids, scores, **kwargs):
                            return cancel.is_set()

                    plain = loaded.model.generate
                    loaded.model.generate = lambda *a, **k: plain(*a, stopping_criteria=StoppingCriteriaList([_Stop()]), **k)
                with _NoGqaWithoutFlash():
                    text, _ = llm._run_generation(loaded, config, system, user, pil, budget,
                                                  temperature, top_p, 1.0, -1, 0, True)
                if cancel is not None and cancel.is_set():
                    raise ForgeError("cancelled", CANCELLED)
        finally:
            if loaded is not None:
                try:
                    close = getattr(loaded.model, "close", None)
                    close() if callable(close) else loaded.model.to("cpu")
                except Exception:
                    pass
                del loaded
            llm._release_all_model_memory()
        return text, {"prompt_tokens": None, "output_tokens": None}


def backends(settings):
    """The sources this request may use, from the person's ComfyUI Settings."""
    settings = settings or {}
    out = {"local": Local(), "ollama": Ollama(_base_url(settings.get("ollama_url"), DEFAULT_OLLAMA))}
    openai_url = _base_url(settings.get("openai_url"))
    if openai_url:
        out["openai"] = OpenAICompatible(openai_url)
    return out


# Models Forge loaded on a server, so the Director can make sure they are gone
# before it runs even if a request was cut off mid-generation.
_FORGE_LOADED = set()  # (backend object, model name)


def unload_forge_models():
    """Backstop for the Director: make sure no Forge model is still resident."""
    for backend, name in list(_FORGE_LOADED):
        if name in backend.loaded():
            log_dasiwa("H3 Forge", f"{name} was still loaded; unloading before the Director runs")
            backend.unload(name)
    _FORGE_LOADED.clear()


def list_all(settings):
    """Every model the person can pick, and a plain-words note per source that failed.

    The default Ollama address failing is normal - most people do not run
    Ollama - so it is only reported when they set an address themselves.
    """
    settings = settings or {}
    models, errors = [], {}
    for kind, backend in backends(settings).items():
        try:
            models += backend.models()
        except Exception as exc:
            if kind == "ollama" and not settings.get("ollama_url"):
                continue
            where = getattr(backend, "base", "ComfyUI/models/llm")
            errors[kind] = f"Could not reach {where} ({exc.__class__.__name__}). Check the address in Settings > DaSiWa > H3 Forge."
    return models, errors


# request_id -> threading.Event, set by POST /dasiwa/h3/forge/cancel.
_CANCELS = {}


def cancel(request_id):
    event = _CANCELS.get(str(request_id or ""))
    if event is None:
        return False
    event.set()
    return True


def generate(body, input_directory=None, release_memory=None):
    """One Forge run. Blocking: call it off the event loop."""
    import threading
    request_id = str(body.get("request_id") or "")
    stop = threading.Event()
    if request_id:
        _CANCELS[request_id] = stop
    try:
        return _generate(body, input_directory, release_memory, stop)
    finally:
        _CANCELS.pop(request_id, None)


def _backend_for(body):
    kind, _, name = str(body.get("model") or "").partition(":")
    backend = backends(body.get("settings")).get(kind)
    if not backend or not name:
        raise ForgeError("no_model", "Pick a model.")
    return kind, name, backend


def _load_pictures(references, mode, input_directory):
    """<Picture N> -> base64 JPEG, for every picture on the timeline with a file."""
    from .helper_minimax_h3_director import resolve_input_path
    _lines, pictures = format_references(references, mode)
    return {tag: _image_b64(resolve_input_path(ref["path"], input_directory))
            for ref, tag in pictures if ref.get("path")}


def scan_units(backend, name, units, picture_b64, reader, num_ctx, timeout, stop, force=False):
    """Descriptions for the units, keyed by their labels: ({labels: text}, how many were scanned).

    A unit the overlay already has a description for is not scanned again;
    `force` is a rescan, which looks again and runs a little looser so it can
    come back different. The model stays loaded across the calls; the caller
    unloads it.
    """
    readings, scanned = {}, 0
    temperature = reader.get("rescan_temperature", 0.6) if force else reader.get("temperature", 0.2)
    for unit in units:
        key = ", ".join(unit["labels"])
        given = None if force else given_reading(unit)
        if given:
            readings[key] = given
            continue
        images = [picture_b64[label] for label in unit["labels"] if label in picture_b64]
        if not images:
            continue
        text, _stats = backend.chat(name, reader["system"], reading_message(unit), images,
                                    {"temperature": temperature, "top_p": 0.8}, num_ctx, timeout, stop,
                                    keep_loaded=True, max_tokens=int(reader.get("max_tokens") or SCAN_TOKENS))
        scanned += 1
        reading = usable_reading(text)
        if reading:
            readings[key] = reading
    return readings, scanned


def _readings_out(units, readings):
    return [{"labels": ", ".join(u["labels"]), "pictures": u["pictures"], "text": readings[", ".join(u["labels"])]}
            for u in units if ", ".join(u["labels"]) in readings]


def scan(body, input_directory=None, release_memory=None):
    """Look at the timeline's pictures ahead of a write. Blocking: call it off the event loop."""
    import threading
    request_id = str(body.get("request_id") or "")
    stop = threading.Event()
    if request_id:
        _CANCELS[request_id] = stop
    try:
        return _scan(body, input_directory, release_memory, stop)
    finally:
        _CANCELS.pop(request_id, None)


def _scan(body, input_directory, release_memory, stop):
    bundle = load_bundle()
    reader = bundle.get("picture_reader")
    if not reader or not reader.get("system"):
        raise ForgeError("scan_off", "This Forge bundle has no picture scanning. Re-export it from PromptForge.")
    kind, name, backend = _backend_for(body)
    references = [r for r in (body.get("references") or []) if isinstance(r, dict)]
    if backend.can_see(name) is False:
        raise ForgeError("no_vision", f"{name} cannot see pictures, so there is nothing to scan. Pick a vision model.")
    picture_b64 = _load_pictures(references, "REF2VA", input_directory) if input_directory else {}
    units = reading_units(references, "REF2VA")
    only = {int(n) for n in (body.get("pictures") or [])}
    if only:
        units = [u for u in units if only & set(u["pictures"])]
    if not units or not picture_b64:
        raise ForgeError("no_pictures", "There are no pictures on the timeline to scan.")

    num_ctx = int(body.get("num_ctx") or bundle["context_length"])
    timeout = int(body.get("timeout") or 600)
    local_gpu = kind == "local" or _is_this_machine(getattr(backend, "base", "http://127.0.0.1"))
    if release_memory and local_gpu:
        release_memory()
    if kind != "local":
        _FORGE_LOADED.add((backend, name))
    started = __import__("time").time()
    try:
        readings, scanned = scan_units(backend, name, units, picture_b64, reader, num_ctx, timeout, stop,
                                       force=bool(body.get("force")))
    except ForgeError:
        raise
    except urlerror.HTTPError as exc:
        raise ForgeError("backend", f"{kind} returned {exc.code}: {exc.read().decode(errors='replace')[:400]}"
                         + (" This server may not accept pictures." if 400 <= exc.code < 500 else ""))
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise ForgeError("backend", f"Could not reach {kind} at {getattr(backend, 'base', '')}: {exc}")
    except ImportError as exc:
        raise ForgeError("backend", str(exc))
    finally:
        unloaded = backend.unload(name) if kind != "local" else True
        if unloaded:
            _FORGE_LOADED.discard((backend, name))
    return {
        "readings": _readings_out(units, readings),
        "scanned": scanned,
        "model": f"{kind}:{name}",
        "unloaded": unloaded or not local_gpu,
        "stats": {"seconds": round(__import__("time").time() - started, 1)},
    }


def _generate(body, input_directory, release_memory, stop):
    bundle = load_bundle()
    mode = body.get("mode")
    if mode not in bundle["modes"]:
        raise ForgeError("bad_mode", f"Forge does not write {mode or 'this mode'} prompts.")
    brief = str(body.get("brief") or "").strip()
    if not brief:
        raise ForgeError("no_brief", "Write what the clip should be first.")
    kind, name, backend = _backend_for(body)
    creativity = body.get("creativity") or bundle["default_creativity"]
    if creativity not in bundle["creativity_presets"]:
        creativity = bundle["default_creativity"]
    detail = body.get("detail") or bundle["default_detail"]
    duration = body.get("duration")
    references = [r for r in (body.get("references") or []) if isinstance(r, dict)]

    sees = backend.can_see(name)
    picture_b64 = _load_pictures(references, mode, input_directory) if sees is not False and input_directory else {}
    # In <Picture N> order, which is the order the reference lines number them.
    labels = list(picture_b64)
    images = [picture_b64[label] for label in labels]

    # REF2VA with two or more pictures: each is looked at on its own first,
    # then the prompt is written from the descriptions and the labelled
    # pictures. One picture has nothing to be confused with.
    reader = bundle.get("picture_reader") or {}
    units = reading_units(references, mode) if mode == "REF2VA" and reader.get("system") and len(images) >= 2 else []

    spec = bundle["modes"][mode]
    sampling = bundle["creativity_presets"][creativity]
    # Sized once, for the write, and used for the scans too: Ollama reloads a
    # model whose context changes between calls. The descriptions are not
    # written yet, so each unit is allowed a paragraph.
    draft = build_user_message(bundle, brief, mode, duration, detail, creativity, references, bool(images))
    num_ctx = int(body.get("num_ctx") or context_for(
        bundle["context_length"], len(spec["system"]) + len(draft) + 600 * len(units), len(images)))
    timeout = int(body.get("timeout") or 600)

    # ComfyUI's models out first, so the LLM has the card to itself. Only when
    # the LLM runs on this machine: a remote server's VRAM is not ours to free.
    local_gpu = kind == "local" or _is_this_machine(getattr(backend, "base", "http://127.0.0.1"))
    if release_memory and local_gpu:
        release_memory()

    readings = {}

    def run(with_images):
        labelled = bool(with_images) and getattr(backend, "labels_pictures", False)
        user = build_user_message(bundle, brief, mode, duration, detail, creativity, references, bool(with_images),
                                  picture_lines=reading_lines(units, readings), labelled=labelled)
        return backend.chat(name, spec["system"], user, with_images, sampling, num_ctx, timeout, stop,
                            image_labels=labels if labelled else None)

    if kind != "local":
        _FORGE_LOADED.add((backend, name))
    started = __import__("time").time()
    try:
        try:
            if units:
                readings, _scanned = scan_units(backend, name, units, picture_b64, reader, num_ctx, timeout, stop)
            raw, stats = run(images)
        except urlerror.HTTPError as exc:
            # An OpenAI-compatible server that cannot take images says so with
            # a 4xx; try once more with words only rather than failing. The
            # descriptions the overlay already had still go along.
            if images and sees is None and 400 <= exc.code < 500:
                images, sees = [], False
                readings = {", ".join(u["labels"]): given_reading(u) for u in units if given_reading(u)}
                raw, stats = run([])
            else:
                raise ForgeError("backend", f"{kind} returned {exc.code}: {exc.read().decode(errors='replace')[:400]}")
    except ForgeError:
        raise
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise ForgeError("backend", f"Could not reach {kind} at {getattr(backend, 'base', '')}: {exc}")
    except ImportError as exc:
        raise ForgeError("backend", str(exc))
    finally:
        unloaded = backend.unload(name) if kind != "local" else True
        if unloaded:
            _FORGE_LOADED.discard((backend, name))

    stats["seconds"] = round(__import__("time").time() - started, 1)
    stats["num_ctx"] = num_ctx
    segments = parse_segments(raw, spec["segments"])
    fields = builder_fields(segments, mode)
    simple = simple_prompt(fields, mode, duration)
    warnings = check_prompt(fields, mode, duration, simple, bundle["max_output_chars"])
    if not unloaded and local_gpu:
        warnings.append("This server cannot unload its model, so it is still using this computer's VRAM. "
                        "Close the server (or unload the model in it) before pressing Run, or use Ollama or llama-swap, "
                        "which Forge can unload. See docs/minimax_h3_forge.md.")
    return {
        "mode": mode,
        # The draft's timestamps (and FL2VA/L2VA's end-frame line) are written
        # for this length; the pop-out warns if the node's duration has changed.
        "duration": duration,
        "fields": fields,
        "simple_prompt": simple,
        "warnings": warnings,
        "model": f"{kind}:{name}",
        "saw_images": len(images),
        # What the model saw in each picture, for the overlay's description cards.
        "readings": _readings_out(units, readings),
        "vision": sees,
        "unloaded": unloaded or not local_gpu,
        "stats": stats,
        "raw": raw,
    }


# ── Routes ────────────────────────────────────────────────────────────────

def register_routes():
    try:
        import folder_paths
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return
    server = getattr(PromptServer, "instance", None)
    if server is None:
        return

    def _release():
        from .nodes_llm import _release_all_model_memory
        _release_all_model_memory()

    @server.routes.post("/dasiwa/h3/forge/models")
    async def forge_models(request):
        try:
            body = await request.json()
            models, errors = await asyncio.to_thread(list_all, body.get("settings"))
        except ForgeError as exc:
            return web.json_response({"error": exc.code, "message": exc.message}, status=400)
        bundle = load_bundle()
        return web.json_response({
            "models": models,
            "errors": errors,
            "detail_levels": {k: v.get("label") for k, v in bundle["detail_levels"].items()},
            "creativity": list(bundle["creativity_presets"].keys()),
            "default_detail": bundle["default_detail"],
            "default_creativity": bundle["default_creativity"],
        })

    @server.routes.post("/dasiwa/h3/forge/cancel")
    async def forge_cancel(request):
        body = await request.json()
        return web.json_response({"cancelled": cancel(body.get("request_id"))})

    @server.routes.post("/dasiwa/h3/forge/scan")
    async def forge_scan(request):
        # Same rule as a write: it frees ComfyUI's models, so not while a workflow runs.
        if server.prompt_queue.get_tasks_remaining() > 0:
            return web.json_response({"error": "busy", "message": "A workflow is running. Scan the pictures before you queue, or wait for it to finish."}, status=409)
        try:
            body = await request.json()
            result = await asyncio.to_thread(scan, body, folder_paths.get_input_directory(), _release)
        except ForgeError as exc:
            return web.json_response({"error": exc.code, "message": exc.message}, status=422)
        except Exception as exc:
            log_dasiwa("H3 Forge", f"scan failed: {exc}")
            return web.json_response({"error": "internal", "message": str(exc)}, status=500)
        log_dasiwa("H3 Forge", f"{result['model']} scanned {result['scanned']} picture unit(s) in {result['stats']['seconds']}s, unloaded={result['unloaded']}")
        return web.json_response(result)

    @server.routes.post("/dasiwa/h3/forge")
    async def forge(request):
        # Freeing memory while a workflow is sampling would pull its models
        # out from under it, so a busy queue is a refusal, not a wait.
        if server.prompt_queue.get_tasks_remaining() > 0:
            return web.json_response({"error": "busy", "message": "A workflow is running. Forge the prompt before you queue, or wait for it to finish."}, status=409)
        try:
            body = await request.json()
            result = await asyncio.to_thread(generate, body, folder_paths.get_input_directory(), _release)
        except ForgeError as exc:
            return web.json_response({"error": exc.code, "message": exc.message, "raw": exc.raw}, status=422)
        except Exception as exc:
            log_dasiwa("H3 Forge", f"failed: {exc}")
            return web.json_response({"error": "internal", "message": str(exc)}, status=500)
        log_dasiwa("H3 Forge", f"{result['model']} wrote a {result['mode']} prompt in {result['stats']['seconds']}s, unloaded={result['unloaded']}")
        return web.json_response(result)


register_routes()
