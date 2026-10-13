"""Pose takers and the official-field-name segment headings."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nodes import h3_prompting as hp


def pic(easy_role, **extra):
    return {"kind": "image", "role": "subject", "easy_role": easy_role, "path": f"{easy_role}.png", **extra}


def segments_for(description):
    return {"Subject definitions": "N/A", "Summary": "[reference generation] A scene.",
            "Detailed description": description, "Soundscape": "Room tone.", "Music": "N/A"}


# -- Pose: its numbers say who takes it --------------------------------------

def test_a_pose_names_who_takes_it():
    cast = hp.easy_cast([pic("character-1"), pic("character-2"),
                         pic("pose", role="pose", picture_kinds=["pose"], who=[2])])
    assert cast["uses"][0]["takers"] == [2]
    lines = "\n".join(hp.easy_lines(cast))
    assert "<Picture 3> gives a pose only, taken by <Subject 2>:" in lines
    segments = segments_for("[Shot 1] They stand.")
    hp.easy_segments(cast, segments)
    assert "<Picture 3> gives the pose only, taken by <Subject 2>:" in segments["Subject definitions"]


def test_a_pose_taken_by_two_names_both():
    cast = hp.easy_cast([pic("character-1"), pic("character-2"),
                         pic("pose", role="pose", picture_kinds=["pose"], who=[1, 2])])
    assert "taken by <Subject 1> and <Subject 2>" in "\n".join(hp.easy_lines(cast))


def test_a_pose_with_nobody_tapped_reads_as_before():
    cast = hp.easy_cast([pic("character-1"), pic("pose", role="pose")])
    lines = "\n".join(hp.easy_lines(cast))
    assert "<Picture 2> gives a pose only: stance" in lines
    assert "taken by" not in lines


def test_a_pose_number_with_no_character_picture_names_nobody():
    cast = hp.easy_cast([pic("character-1"), pic("pose", role="pose", picture_kinds=["pose"], who=[3])])
    assert "taken by" not in "\n".join(hp.easy_lines(cast))


def test_nobody_in_a_pose_picture_joins_the_cast():
    cast = hp.easy_cast([pic("character-1"), pic("pose", role="pose", picture_kinds=["pose"], who=[1, 2])])
    assert [s["number"] for s in cast["subjects"] if s["kind"] == "character"] == [1]


# -- a section headed with its official field name ---------------------------

def test_a_field_name_heading_is_read_as_its_section():
    raw = ("===SEGMENT: Detailed description===\n[Shot 1] A shot.\n\n"
           "===SEGMENT: Soundscape===\nRain.\n\n===SEGMENT: non_diegetic_music===\nN/A")
    segments = hp.parse_segments(raw, ["Detailed description", "Soundscape", "Music"])
    assert segments["Music"] == "N/A"


def test_the_label_wins_when_both_headings_appear():
    raw = ("===SEGMENT: Detailed description===\nShots.\n\n===SEGMENT: Soundscape===\nRain.\n\n"
           "===SEGMENT: Music===\nPiano.\n\n===SEGMENT: non_diegetic_music===\nN/A")
    segments = hp.parse_segments(raw, ["Detailed description", "Soundscape", "Music"])
    assert segments["Music"] == "Piano."
