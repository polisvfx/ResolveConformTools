#!/usr/bin/env python
#
# Batch Rename - part of ResolveConformTools
# Copyright (C) 2026 Maris Polis - marispolis.com
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Batch Rename
Version: 2.0

A comprehensive batch renaming utility for DaVinci Resolve.

Two tabs rename two different things:
  - Media Pool Clips: renames the source clip (MediaPoolItem.SetName). Every
    timeline that uses the clip shows the new name.
  - Timeline Clips: renames clip instances on the current timeline only
    (TimelineItem.SetName, DaVinci Resolve 20.2+). The Media Pool is not touched,
    and two uses of the same source can carry different names.

Features:
  - Search & Replace (plain text or regex)
  - Add Prefix / Suffix (with date/time and timeline tokens)
  - Numbering: template, placement, padding, increment, start number or
    "continue after the highest existing number"
  - Change Case, Clean Name (strip extension / sequence range)
  - Remove N characters from start, end, or specific position
  - A rename that would leave a blank name is skipped
  - Pipeline of composable, reorderable operations
  - Live preview with collision detection
  - Filters: clip type, clip color, flag, markers; timeline tracks and selection
  - Restore timeline clip names from their Media Pool clip
  - Undo history and saveable presets
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QSpinBox, QCheckBox, QPushButton, QTreeWidget,
    QTreeWidgetItem, QListWidget, QListWidgetItem, QWidget, QGroupBox,
    QSplitter, QSizePolicy, QAbstractItemView, QTabWidget, QGridLayout,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap, QPen


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFS_KEY = "ResolveConformTools.BatchRename"
SCRIPT_VERSION = "2.0"
MAX_UNDO = 20

TAB_MEDIA_POOL = "media_pool"
TAB_TIMELINE = "timeline"

DATETIME_TOKENS = {
    "{date}": lambda dt: dt.strftime("%Y-%m-%d"),
    "{time}": lambda dt: dt.strftime("%H-%M-%S"),
    "{year}": lambda dt: dt.strftime("%Y"),
    "{month}": lambda dt: dt.strftime("%m"),
    "{day}": lambda dt: dt.strftime("%d"),
    "{hour}": lambda dt: dt.strftime("%H"),
    "{minute}": lambda dt: dt.strftime("%M"),
    "{second}": lambda dt: dt.strftime("%S"),
}

# Tokens that only have a value for timeline clips.
TIMELINE_TOKENS = ["{source}", "{track}", "{timeline}"]
NUMBER_TOKEN = "{n}"

# Media pool clip types returned by GetClipProperty("Type").
# Each entry: checkbox ID -> (label, set of type strings to match).
# Resolve may return slightly different strings across versions;
# the sets allow for known variants.
CLIP_TYPE_FILTERS = [
    ("FilterTimeline",  "Timeline",       {"Timeline"}),
    ("FilterVideo",     "Video",          {"Video", "Video + Audio"}),
    ("FilterAudio",     "Audio",          {"Audio"}),
    ("FilterStill",     "Still / Image",  {"Still"}),
    ("FilterCompound",  "Compound Clip",  {"Compound Clip"}),
    ("FilterFusion",    "Fusion Comp",    {"Fusion Composition", "Fusion Comp"}),
    ("FilterGenerator", "Generator",      {"Generator"}),
]

# All filter checkbox IDs including the "Other" catch-all
ALL_FILTER_IDS = [cb_id for cb_id, _, _ in CLIP_TYPE_FILTERS] + ["FilterOther"]

# Flat set of every type string we explicitly handle — anything not in here
# is considered "Other".
_KNOWN_TYPES = set()
for _, _, type_set in CLIP_TYPE_FILTERS:
    _KNOWN_TYPES |= type_set

# Resolve's clip colours (clip colour) and flag / marker colours.
CLIP_COLORS = [
    "Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy",
    "Blue", "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate",
]
FLAG_COLORS = [
    "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
    "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
]
MARKER_COLORS = FLAG_COLORS

FILTER_ANY = "Any"
FILTER_NONE = "None"
MARKER_HAS = "Has markers"
MARKER_NO = "No markers"
DEFAULT_ATTR_FILTERS = {"clip_color": FILTER_ANY, "flag": FILTER_ANY, "marker": FILTER_ANY}

MP_SORT_MODES = ["Bin order", "Name", "Start TC"]

SCOPE_WHOLE = "whole"
SCOPE_SELECTED = "selected"

# Numbering
PLACE_REPLACE = "replace"
PLACE_BEFORE = "before"
PLACE_AFTER = "after"
PLACEMENT_LABELS = [
    (PLACE_REPLACE, "Replace name"),
    (PLACE_BEFORE, "Before name"),
    (PLACE_AFTER, "After name"),
]
START_AT = "start"
START_CONTINUE = "continue"
COUNT_SHOWN = "shown"
COUNT_TIMELINE = "timeline"

# Change Case
CASE_UPPER = "upper"
CASE_LOWER = "lower"
CASE_TITLE = "title"
CASE_CAPITALIZE = "capitalize"
CASE_LABELS = [
    (CASE_UPPER, "UPPERCASE"),
    (CASE_LOWER, "lowercase"),
    (CASE_TITLE, "Title Case"),
    (CASE_CAPITALIZE, "Capitalize first letter"),
]

# A file extension: a dot, a letter, then up to four letters/digits, at the end.
# ".v002" is a version, not an extension, so it is excluded.
EXTENSION_RE = r"\.(?![vV]\d+$)[A-Za-z][A-Za-z0-9]{0,4}$"
# Resolve's image-sequence range, e.g. "plate_[0001-0100]" or "plate [0001-0100]".
SEQ_RANGE_RE = r"^(.*?)[\s._-]*\[\d+-\d+\]$"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OpType(Enum):
    SEARCH_REPLACE = "Search & Replace"
    PREFIX = "Add Prefix"
    SUFFIX = "Add Suffix"
    NUMBERING = "Numbering"
    CHANGE_CASE = "Change Case"
    CLEAN_NAME = "Clean Name"
    REMOVE_START = "Remove from Start"
    REMOVE_END = "Remove from End"
    REMOVE_POSITION = "Remove at Position"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class RenameOp:
    """A single rename operation in the pipeline."""
    op_type: OpType
    search_text: str = ""
    replace_text: str = ""
    case_sensitive: bool = True
    use_regex: bool = False
    affix_text: str = ""
    remove_count: int = 0
    remove_position: int = 0
    enabled: bool = True
    # Numbering
    num_template: str = "{n}"
    num_placement: str = "replace"
    num_separator: str = "_"
    num_padding: int = 4
    num_increment: int = 10
    num_start: int = 10
    num_start_mode: str = "start"
    num_count_over: str = "shown"
    # Clean Name
    clean_extension: bool = True
    clean_seq_range: bool = True
    # Change Case
    case_mode: str = "upper"
    case_skip_ext: bool = True

    def describe(self) -> str:
        """Human-readable one-line summary for the operations list."""
        if self.op_type == OpType.SEARCH_REPLACE:
            flags = ""
            if not self.case_sensitive:
                flags += " [i]"
            if self.use_regex:
                flags += " [re]"
            return f'Replace "{self.search_text}" -> "{self.replace_text}"{flags}'
        elif self.op_type == OpType.PREFIX:
            return f'Prefix: "{self.affix_text}"'
        elif self.op_type == OpType.SUFFIX:
            return f'Suffix: "{self.affix_text}"'
        elif self.op_type == OpType.NUMBERING:
            place = dict(PLACEMENT_LABELS).get(self.num_placement, self.num_placement)
            if self.num_start_mode == "continue":
                start = "continue after highest"
            else:
                start = f"from {self.num_start}"
            text = (f'Number "{self.num_template}" ({place.lower()}), {start}, '
                    f'step {self.num_increment}, pad {self.num_padding}')
            if self.num_count_over == "timeline":
                text += " [full-timeline positions]"
            return text
        elif self.op_type == OpType.CHANGE_CASE:
            mode = dict(CASE_LABELS).get(self.case_mode, self.case_mode)
            return f"Case: {mode}" + (" (keep extension)" if self.case_skip_ext else "")
        elif self.op_type == OpType.CLEAN_NAME:
            parts = []
            if self.clean_extension:
                parts.append("extension")
            if self.clean_seq_range:
                parts.append("sequence range")
            return "Clean name: strip " + (" + ".join(parts) if parts else "nothing")
        elif self.op_type == OpType.REMOVE_START:
            return f"Remove {self.remove_count} chars from start"
        elif self.op_type == OpType.REMOVE_END:
            return f"Remove {self.remove_count} chars from end"
        elif self.op_type == OpType.REMOVE_POSITION:
            return f"Remove {self.remove_count} chars at pos {self.remove_position}"
        return self.op_type.value


@dataclass
class RenamePreset:
    """A saved collection of operations and filter states."""
    name: str
    operations: list = field(default_factory=list)
    filters: dict = field(default_factory=dict)  # {checkbox_id: bool}
    include_subfolders: bool = False
    tab: str = "media_pool"
    extra: dict = field(default_factory=dict)  # attr filters, sort, scope


@dataclass
class UndoRecord:
    """Stores one batch rename for undo."""
    description: str
    mappings: list = field(default_factory=list)  # [(uid, old_name, new_name)]
    kind: str = "media_pool"
    timeline_uid: str = ""
    timeline_name: str = ""


# ---------------------------------------------------------------------------
# Token Expansion
# ---------------------------------------------------------------------------

def expand_tokens(text: str, now: Optional[datetime] = None,
                  extra: Optional[dict] = None) -> str:
    """Replace date/time tokens (and any `extra` {token: value}) in text."""
    if now is None:
        now = datetime.now()
    for token, formatter in DATETIME_TOKENS.items():
        if token in text:
            text = text.replace(token, formatter(now))
    if extra:
        for token, value in extra.items():
            if token in text:
                text = text.replace(token, "" if value is None else str(value))
    return text


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def split_extension(name: str):
    """Split "clip.mov" into ("clip", ".mov"); ("clip", "") when there is none."""
    m = re.search(EXTENSION_RE, name)
    if not m or m.start() == 0:
        return name, ""
    return name[:m.start()], name[m.start():]


def clean_name(name: str, strip_extension: bool = True, strip_seq_range: bool = True) -> str:
    """Strip a file extension and/or Resolve's "[0001-0100]" sequence range.

    The same two steps as Shot Numbering (Clip Name), in the same order: the
    extension first, since Resolve puts it after the range ("plate_[0001-0100].exr").
    """
    if strip_extension:
        stem, _ext = split_extension(name)
        if stem:
            name = stem
    if strip_seq_range:
        m = re.match(SEQ_RANGE_RE, name)
        if m and m.group(1):
            name = m.group(1)
    return name


def _title_word(word: str) -> str:
    return word[:1].upper() + word[1:].lower()


def change_case(name: str, mode: str, skip_extension: bool = True) -> str:
    """Change the case of a name, optionally leaving its extension alone."""
    ext = ""
    if skip_extension:
        name, ext = split_extension(name)
    if mode == CASE_UPPER:
        name = name.upper()
    elif mode == CASE_LOWER:
        name = name.lower()
    elif mode == CASE_TITLE:
        # Split on separators but keep them, so "a_b-c.d e" keeps its punctuation.
        parts = re.split(r"([ _\-.]+)", name)
        name = "".join(p if re.fullmatch(r"[ _\-.]+", p or " ") else _title_word(p)
                       for p in parts)
    elif mode == CASE_CAPITALIZE:
        name = name[:1].upper() + name[1:]
    return name + ext


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------

def format_number(value: int, padding: int) -> str:
    """Zero-pad a number; a negative one keeps its sign in front."""
    if value < 0:
        return "-" + str(-value).zfill(max(1, padding))
    return str(value).zfill(max(1, padding))


def numbering_regex(op: RenameOp):
    """Compiled regex that finds this numbering op's number in an existing name.

    Literal template text is escaped, {n} becomes the captured digits and any
    other token matches anything. Placement decides where in the name to look:
    the whole name (replace), its start (before) or its end (after), with the
    separator in between.
    """
    template = op.num_template or NUMBER_TOKEN
    tokens = [NUMBER_TOKEN] + list(DATETIME_TOKENS) + TIMELINE_TOKENS
    split_re = "(" + "|".join(re.escape(t) for t in tokens) + ")"
    pattern = ""
    seen_number = False
    for part in re.split(split_re, template):
        if part == NUMBER_TOKEN:
            pattern += r"(\d+)" if not seen_number else r"\d+"
            seen_number = True
        elif part in tokens:
            pattern += ".*?"
        else:
            pattern += re.escape(part)
    if not seen_number:
        return None
    sep = re.escape(op.num_separator or "")
    if op.num_placement == PLACE_BEFORE:
        return re.compile("^" + pattern + sep)
    if op.num_placement == PLACE_AFTER:
        return re.compile(sep + pattern + "$")
    return re.compile("^" + pattern + "$")


def highest_existing_number(op: RenameOp, names) -> Optional[int]:
    """Highest number already used in `names` for this op's template, or None."""
    rx = numbering_regex(op)
    if rx is None:
        return None
    highest = None
    for name in names:
        if not isinstance(name, str):
            continue
        m = rx.search(name)
        if m:
            n = int(m.group(1))
            if highest is None or n > highest:
                highest = n
    return highest


def numbering_base(op: RenameOp, all_names=None) -> int:
    """First number this op hands out.

    "continue" starts one increment past the highest number already in use (the
    idea of NextShotNumber in Shot Numbering (Clip Name)), and falls back to the
    op's own start number when nothing matches yet.
    """
    if op.num_start_mode == START_CONTINUE:
        highest = highest_existing_number(op, all_names or [])
        if highest is not None:
            return highest + op.num_increment
    return op.num_start


# ---------------------------------------------------------------------------
# Pipeline Engine
# ---------------------------------------------------------------------------

def apply_operation(name: str, op: RenameOp, now: Optional[datetime] = None,
                    ctx: Optional[dict] = None, op_index: Optional[int] = None) -> str:
    """Apply a single RenameOp to a clip name string.

    `ctx` carries the per-item values: "index" (position in the numbering
    sequence), "full_index" (position on the whole timeline, or None),
    "tokens" ({token: value}) and "num_bases" ({op_index: first number}).
    """
    if now is None:
        now = datetime.now()
    ctx = ctx or {}
    tokens = ctx.get("tokens") or {}

    if op.op_type == OpType.SEARCH_REPLACE:
        search = expand_tokens(op.search_text, now, tokens)
        replace = expand_tokens(op.replace_text, now, tokens)
        if op.use_regex:
            try:
                flags = 0 if op.case_sensitive else re.IGNORECASE
                name = re.sub(search, replace, name, flags=flags)
            except re.error:
                pass
        else:
            if op.case_sensitive:
                if search:
                    name = name.replace(search, replace)
            else:
                try:
                    pattern = re.escape(search)
                    name = re.sub(pattern, lambda _m: replace, name, flags=re.IGNORECASE)
                except re.error:
                    pass

    elif op.op_type == OpType.PREFIX:
        name = expand_tokens(op.affix_text, now, tokens) + name

    elif op.op_type == OpType.SUFFIX:
        name = name + expand_tokens(op.affix_text, now, tokens)

    elif op.op_type == OpType.NUMBERING:
        position = ctx.get("index", 0)
        if op.num_count_over == COUNT_TIMELINE and ctx.get("full_index") is not None:
            position = ctx["full_index"]
        base = (ctx.get("num_bases") or {}).get(op_index, op.num_start)
        number = format_number(base + position * op.num_increment, op.num_padding)
        # {n} first, so a source name that happens to contain "{n}" is left alone.
        text = (op.num_template or NUMBER_TOKEN).replace(NUMBER_TOKEN, number)
        text = expand_tokens(text, now, tokens)
        if op.num_placement == PLACE_BEFORE:
            name = text + op.num_separator + name
        elif op.num_placement == PLACE_AFTER:
            name = name + op.num_separator + text
        else:
            name = text

    elif op.op_type == OpType.CHANGE_CASE:
        name = change_case(name, op.case_mode, op.case_skip_ext)

    elif op.op_type == OpType.CLEAN_NAME:
        name = clean_name(name, op.clean_extension, op.clean_seq_range)

    elif op.op_type == OpType.REMOVE_START:
        name = name[op.remove_count:]

    elif op.op_type == OpType.REMOVE_END:
        if op.remove_count > 0 and len(name) > op.remove_count:
            name = name[:-op.remove_count]

    elif op.op_type == OpType.REMOVE_POSITION:
        pos = op.remove_position
        count = op.remove_count
        if 0 <= pos < len(name):
            name = name[:pos] + name[pos + count:]

    return name


def apply_pipeline(name: str, operations: list, now: Optional[datetime] = None,
                   ctx: Optional[dict] = None) -> str:
    """Apply the full operation pipeline sequentially to a name."""
    if now is None:
        now = datetime.now()
    for op_index, op in enumerate(operations):
        if not op.enabled:
            continue
        name = apply_operation(name, op, now, ctx, op_index)
    return name


def compute_new_names(items: list, operations: list, now: Optional[datetime] = None,
                      all_names=None, full_index_of: Optional[dict] = None) -> list:
    """New name for every item, in order. Preview and rename both call this.

    `items` is the list shown in the preview, in numbering order; an item's index
    in it is its numbering position. `all_names` is every current name in the
    tab's whole source (for "continue"). `full_index_of` maps an item uid to its
    position on the whole timeline (for "every clip on the timeline" counting).
    """
    if now is None:
        now = datetime.now()
    bases = {}
    for op_index, op in enumerate(operations):
        if op.enabled and op.op_type == OpType.NUMBERING:
            bases[op_index] = numbering_base(op, all_names)
    result = []
    for index, info in enumerate(items):
        ctx = {
            "index": index,
            "full_index": (full_index_of or {}).get(info.get("uid")) if full_index_of else None,
            "tokens": info.get("tokens") or {},
            "num_bases": bases,
        }
        result.append(apply_pipeline(info["name"], operations, now, ctx))
    return result


def pipeline_needs_all_names(operations: list) -> bool:
    """True when a numbering op has to look at every existing name."""
    return any(op.enabled and op.op_type == OpType.NUMBERING
               and op.num_start_mode == START_CONTINUE for op in operations)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

_OP_FIELDS = [
    "search_text", "replace_text", "case_sensitive", "use_regex", "affix_text",
    "remove_count", "remove_position", "enabled",
    "num_template", "num_placement", "num_separator", "num_padding",
    "num_increment", "num_start", "num_start_mode", "num_count_over",
    "clean_extension", "clean_seq_range", "case_mode", "case_skip_ext",
]


def serialize_op(op: RenameOp) -> dict:
    """Convert a RenameOp to a JSON-serializable dict."""
    d = {"op_type": op.op_type.value}
    for name in _OP_FIELDS:
        d[name] = getattr(op, name)
    return d


def deserialize_op(d: dict) -> RenameOp:
    """Reconstruct a RenameOp from a dict. Fields a v1 preset lacks keep defaults."""
    op = RenameOp(op_type=OpType(d["op_type"]))
    for name in _OP_FIELDS:
        if name in d:
            setattr(op, name, d[name])
    return op


# ---------------------------------------------------------------------------
# Resolve API helpers
# ---------------------------------------------------------------------------

def _call(obj, method: str, *args, default=None):
    """Call obj.method(*args), or return `default`.

    Resolve's bridge returns None for unknown attribute names instead of raising,
    so a missing method and a call that fails both end up at `default`.
    """
    fn = getattr(obj, method, None)
    if not callable(fn):
        return default
    try:
        result = fn(*args)
    except Exception:
        return default
    return default if result is None else result


def read_attrs(obj) -> dict:
    """Clip colour, flags and markers of a MediaPoolItem or TimelineItem."""
    color = _call(obj, "GetClipColor", default="") or ""
    flags = _call(obj, "GetFlagList", default=[]) or []
    if isinstance(flags, dict):
        flags = list(flags.values())
    markers = _call(obj, "GetMarkers", default={}) or {}
    if not isinstance(markers, dict):
        markers = {}
    marker_colors = sorted({str(m.get("color")) for m in markers.values()
                            if isinstance(m, dict) and m.get("color")})
    return {
        "clip_color": str(color),
        "flags": [str(f) for f in flags if f],
        "marker_colors": marker_colors,
        "marker_count": len(markers),
    }


def item_matches_attr_filters(info: dict, filters: Optional[dict]) -> bool:
    """Apply the Clip Color / Flag / Marker filters to an item dict."""
    f = filters or {}
    want_color = f.get("clip_color", FILTER_ANY)
    color = info.get("clip_color") or ""
    if want_color == FILTER_NONE and color:
        return False
    if want_color not in (FILTER_ANY, FILTER_NONE) and color != want_color:
        return False

    want_flag = f.get("flag", FILTER_ANY)
    flags = info.get("flags") or []
    if want_flag == FILTER_NONE and flags:
        return False
    if want_flag not in (FILTER_ANY, FILTER_NONE) and want_flag not in flags:
        return False

    want_marker = f.get("marker", FILTER_ANY)
    count = info.get("marker_count", 0)
    if want_marker == MARKER_HAS and count == 0:
        return False
    if want_marker == MARKER_NO and count > 0:
        return False
    if (want_marker not in (FILTER_ANY, MARKER_HAS, MARKER_NO)
            and want_marker not in (info.get("marker_colors") or [])):
        return False
    return True


def frames_to_tc(frames: int, fps: float, drop_frame: bool = False) -> str:
    """Timecode for an absolute frame count; drop-frame at 29.97 / 59.94."""
    nominal = int(round(fps)) or 25
    sep = ":"
    if drop_frame and nominal in (30, 60):
        drop = 2 if nominal == 30 else 4
        per_10min = nominal * 600 - drop * 9
        per_min = nominal * 60 - drop
        tens, rem = divmod(frames, per_10min)
        extra = drop * 9 * tens
        if rem > drop:
            extra += drop * ((rem - drop) // per_min)
        frames += extra
        sep = ";"
    ff = frames % nominal
    total_seconds = frames // nominal
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = (total_seconds // 3600) % 24
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"


# ---------------------------------------------------------------------------
# Media Pool side
# ---------------------------------------------------------------------------

def _get_clip_type(clip) -> str:
    """Return the Type string for a media pool item."""
    try:
        props = clip.GetClipProperty("Type")
        if props:
            return str(props)
    except Exception:
        pass
    return "Unknown"


def _classify_type(type_str: str) -> str:
    """Map a Resolve type string to the user-facing label."""
    for _, label, type_set in CLIP_TYPE_FILTERS:
        if type_str in type_set:
            return label
    return "Other"


def _clip_to_item(clip, bin_index: int = 0) -> dict:
    """Build an item dict from a Resolve media pool clip object."""
    clip_name = clip.GetName() or ""
    media_id = _call(clip, "GetMediaId", default="") or _call(clip, "GetUniqueId", default="") or ""
    clip_type = _get_clip_type(clip)
    start_tc = ""
    try:
        start_tc = clip.GetClipProperty("Start TC") or ""
    except Exception:
        pass
    info = {
        "kind": TAB_MEDIA_POOL,
        "item": clip,
        "name": clip_name,
        "uid": str(media_id) or clip_name,
        "media_id": media_id,
        "type": clip_type,
        "type_label": _classify_type(clip_type),
        "start_tc": str(start_tc),
        "bin_index": bin_index,
        "tokens": {},
    }
    info.update(read_attrs(clip))
    return info


def _is_bin(obj) -> bool:
    """Return True if *obj* is a Media Pool folder/bin, not a clip."""
    return callable(getattr(obj, "GetClipList", None))


def get_selected_media_pool_items(project) -> list:
    """Return items based on the active Media Pool selection.

    - If clips (non-bin items) are selected, return those clips.
    - If only bins are selected, return all clips inside those bins.
    - Returns an empty list when nothing is selected so the caller can
      fall back to folder-based collection.
    """
    media_pool = project.GetMediaPool()
    if not media_pool:
        return []
    try:
        selected = media_pool.GetSelectedClips()
    except Exception:
        return []
    if not selected:
        return []
    if isinstance(selected, dict):
        selected = list(selected.values())

    clips = [s for s in selected if not _is_bin(s)]
    bins = [s for s in selected if _is_bin(s)]

    if clips:
        return [_clip_to_item(c, i) for i, c in enumerate(clips)]

    # Only bins selected — collect their contents.
    items: list = []
    seen: set = set()
    for folder in bins:
        for clip in folder.GetClipList() or []:
            cid = id(clip)
            if cid not in seen:
                seen.add(cid)
                items.append(_clip_to_item(clip, len(items)))
    return items


def get_media_pool_items(project, recursive: bool = False, from_root: bool = False) -> list:
    """Get all items from the current (or root) media pool folder with type info."""
    media_pool = project.GetMediaPool()
    if not media_pool:
        return []
    folder = media_pool.GetRootFolder() if from_root else media_pool.GetCurrentFolder()
    if not folder:
        return []
    items = []

    def collect_from_folder(f):
        for clip in f.GetClipList() or []:
            items.append(_clip_to_item(clip, len(items)))
        if recursive:
            for sub in f.GetSubFolderList() or []:
                collect_from_folder(sub)

    collect_from_folder(folder)
    return items


def get_all_media_pool_names(project) -> list:
    """Every clip name in the project's media pool (for "continue" numbering)."""
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder() if media_pool else None
    names = []

    def walk(f):
        for clip in f.GetClipList() or []:
            n = _call(clip, "GetName", default="")
            if n:
                names.append(n)
        for sub in f.GetSubFolderList() or []:
            walk(sub)

    if root:
        walk(root)
    return names


def sort_media_pool_items(items: list, mode: str) -> list:
    """Order media pool items for numbering: bin order, name or start TC."""
    if mode == "Name":
        return sorted(items, key=lambda i: (i["name"].lower(), i.get("bin_index", 0)))
    if mode == "Start TC":
        return sorted(items, key=lambda i: (i.get("start_tc") or "~", i["name"].lower()))
    return sorted(items, key=lambda i: i.get("bin_index", 0))


def filter_media_pool_items(items: list, active_types: set, attr_filters: dict) -> list:
    """Media pool items whose type label is active and that pass the attr filters."""
    return [i for i in items
            if i.get("type_label") in active_types and item_matches_attr_filters(i, attr_filters)]


# ---------------------------------------------------------------------------
# Timeline side
# ---------------------------------------------------------------------------

def timeline_item_uid(item) -> str:
    """Stable key for a TimelineItem across API calls.

    Resolve hands back a fresh wrapper per call, so `==` and identity are useless
    (verified on 21.0.4.5). GetUniqueId() is stable and distinct per instance.
    """
    uid = _call(item, "GetUniqueId", default="")
    if uid:
        return "uid:" + str(uid)
    mpi = _call(item, "GetMediaPoolItem")
    mpi_id = _call(mpi, "GetUniqueId", default="") if mpi else ""
    return "geo:%s:%s:%s:%s" % (mpi_id, _call(item, "GetStart", default=-1),
                                _call(item, "GetEnd", default=-1),
                                _call(item, "GetLeftOffset", default=-1))


def timeline_rename_supported(resolve_obj, timeline=None) -> bool:
    """Does this build have TimelineItem.SetName()? (DaVinci Resolve 20.2+)"""
    if timeline is not None:
        for track in range(1, int(_call(timeline, "GetTrackCount", "video", default=0)) + 1):
            items = _call(timeline, "GetItemListInTrack", "video", track, default=[]) or []
            if items:
                return callable(getattr(items[0], "SetName", None))
    version = _call(resolve_obj, "GetVersion", default=[]) or []
    try:
        major, minor = int(version[0]), int(version[1])
    except (IndexError, TypeError, ValueError):
        return False
    return (major, minor) >= (20, 2)


def has_timeline_selection_api(timeline) -> bool:
    """Does this build have Timeline.GetSelectedClips()? (DaVinci Resolve 21.0.4+)"""
    return timeline is not None and callable(getattr(timeline, "GetSelectedClips", None))


def get_timeline_selection(timeline):
    """Selected TimelineItems, or None when the API is missing.

    None (unsupported) and [] (nothing selected) are deliberately distinct.
    """
    if not has_timeline_selection_api(timeline):
        return None
    try:
        sel = timeline.GetSelectedClips()
    except Exception:
        return None
    if sel is None:
        return []
    if isinstance(sel, dict):
        sel = list(sel.values())
    return [s for s in sel if s]


def get_timeline_items(timeline, with_attrs: bool = True) -> list:
    """Every video-track item on `timeline`, in timeline order (start, then track).

    The same order as GetOrderedTimelineItems in the Shot Numbering scripts.
    """
    if timeline is None:
        return []
    tl_name = _call(timeline, "GetName", default="") or ""
    items = []
    count = int(_call(timeline, "GetTrackCount", "video", default=0) or 0)
    for track in range(1, count + 1):
        for item in _call(timeline, "GetItemListInTrack", "video", track, default=[]) or []:
            mpi = _call(item, "GetMediaPoolItem")
            source = (_call(mpi, "GetName", default="") or "") if mpi else ""
            info = {
                "kind": TAB_TIMELINE,
                "item": item,
                "name": _call(item, "GetName", default="") or "",
                "uid": timeline_item_uid(item),
                "track": track,
                "start": int(_call(item, "GetStart", default=0) or 0),
                "source_name": source,
                "type_label": f"V{track}",
                "tokens": {
                    "{source}": clean_name(source) if source else "",
                    "{track}": f"V{track}",
                    "{timeline}": tl_name,
                },
            }
            if with_attrs:
                info.update(read_attrs(item))
            items.append(info)
    items.sort(key=lambda i: (i["start"], i["track"]))
    return items


def filter_to_selection(items: list, selection) -> tuple:
    """(items that are in `selection`, number of selected items ignored).

    Audio and subtitle items are dropped with GetTrackTypeAndIndex(): `items` only
    holds video items, so they would never match anyway, but they are counted.
    """
    keys = set()
    for sel in selection or []:
        track = _call(sel, "GetTrackTypeAndIndex", default=None)
        if isinstance(track, (list, tuple)) and track and str(track[0]) != "video":
            continue
        keys.add(timeline_item_uid(sel))
    picked = [i for i in items if i["uid"] in keys]
    return picked, len(selection or []) - len(picked)


def filter_timeline_items(items: list, tracks: Optional[set], attr_filters: dict) -> list:
    """Timeline items on an enabled track that pass the attr filters."""
    return [i for i in items
            if (tracks is None or i["track"] in tracks)
            and item_matches_attr_filters(i, attr_filters)]


def restore_plan(items: list) -> list:
    """[(item_info, media_pool_name)] for items whose name differs from their source."""
    return [(i, i["source_name"]) for i in items
            if i.get("source_name") and i["source_name"] != i["name"]]


def track_range_label(items: list) -> str:
    """"V1", "V1-V3" or "V1, V3" style label for the tracks in `items`."""
    tracks = sorted({i["track"] for i in items if "track" in i})
    if not tracks:
        return ""
    if len(tracks) == 1:
        return f"V{tracks[0]}"
    if tracks == list(range(tracks[0], tracks[-1] + 1)):
        return f"V{tracks[0]}–V{tracks[-1]}"
    return ", ".join(f"V{t}" for t in tracks)


def find_timeline_by_uid(project, uid: str):
    """The project's timeline with this unique id, or None."""
    current = _call(project, "GetCurrentTimeline")
    if current is not None and _call(current, "GetUniqueId", default="") == uid:
        return current
    for idx in range(1, int(_call(project, "GetTimelineCount", default=0) or 0) + 1):
        tl = _call(project, "GetTimelineByIndex", idx)
        if tl is not None and _call(tl, "GetUniqueId", default="") == uid:
            return tl
    return None


# ---------------------------------------------------------------------------
# Rename / undo
# ---------------------------------------------------------------------------

def rename_item(item, new_name: str) -> bool:
    """Rename a MediaPoolItem or TimelineItem with SetName()."""
    try:
        if item.SetName(new_name):
            return True
    except Exception as e:
        print(f"  SetName() failed: {e}")
    return False


def execute_rename(pairs: list, kind: str = TAB_MEDIA_POOL, timeline_uid: str = "",
                   timeline_name: str = "", verb: str = "Renamed") -> tuple:
    """Rename each (item_info, new_name). Returns (UndoRecord or None, failed)."""
    mappings = []
    failed = 0
    what = "timeline clips" if kind == TAB_TIMELINE else "media pool clips"
    print(f"\n--- Batch Rename: {len(pairs)} {what} ---")
    for info, new_name in pairs:
        old_name = info["name"]
        if new_name == old_name:
            continue
        if not new_name.strip():
            # SetName("") is accepted and leaves a blank clip name (21.2.0.6).
            failed += 1
            print(f"  SKIPPED: '{old_name}' -> empty name")
            continue
        if rename_item(info["item"], new_name):
            mappings.append((info.get("uid") or old_name, old_name, new_name))
            print(f"  {verb}: '{old_name}' -> '{new_name}'")
        else:
            failed += 1
            print(f"  FAILED:  '{old_name}' -> '{new_name}'")
    if not mappings:
        return None, failed
    where = f" on '{timeline_name}'" if kind == TAB_TIMELINE and timeline_name else ""
    noun = what if len(mappings) != 1 else what[:-1]
    record = UndoRecord(
        description=f"{verb} {len(mappings)} {noun}{where}",
        mappings=mappings, kind=kind,
        timeline_uid=timeline_uid, timeline_name=timeline_name,
    )
    return record, failed


def undo_lookup(items: list, mappings: list) -> list:
    """[(item_info, old_name)] to revert. Matches by uid, then by the new name."""
    by_uid = {i.get("uid"): i for i in items if i.get("uid")}
    plan = []
    used = set()
    for uid, old_name, new_name in mappings:
        info = by_uid.get(uid)
        if info is None:
            info = next((i for i in items if i["name"] == new_name
                         and id(i) not in used), None)
        if info is not None:
            used.add(id(info))
            plan.append((info, old_name))
    return plan


def execute_undo(undo_record: UndoRecord, project) -> int:
    """Reverse a rename operation. Returns number of items reverted."""
    if undo_record.kind == TAB_TIMELINE:
        tl = find_timeline_by_uid(project, undo_record.timeline_uid)
        if tl is None:
            print(f"Undo: timeline '{undo_record.timeline_name}' no longer exists.")
            return 0
        items = get_timeline_items(tl, with_attrs=False)
    else:
        # Search the whole pool: a clip may sit in another bin by now.
        items = get_media_pool_items(project, recursive=True, from_root=True)
    reverted = 0
    for info, old_name in undo_lookup(items, undo_record.mappings):
        if rename_item(info["item"], old_name):
            reverted += 1
    return reverted


# ---------------------------------------------------------------------------
# Preset Persistence
# ---------------------------------------------------------------------------

def preset_to_dict(preset: RenamePreset) -> dict:
    return {
        "name": preset.name,
        "operations": preset.operations,
        "filters": preset.filters,
        "include_subfolders": preset.include_subfolders,
        "tab": preset.tab,
        "extra": preset.extra,
    }


def preset_from_dict(d: dict) -> RenamePreset:
    return RenamePreset(
        name=d["name"],
        operations=d.get("operations", []),
        filters=d.get("filters", {}),
        include_subfolders=d.get("include_subfolders", False),
        tab=d.get("tab", TAB_MEDIA_POOL),
        extra=d.get("extra", {}) or {},
    )


def save_presets(presets: list) -> None:
    """Save all presets to Fusion settings."""
    try:
        fusion = resolve.Fusion()  # noqa: F821
        fusion.SetData(f"{PREFS_KEY}.presets",
                       json.dumps([preset_to_dict(p) for p in presets]))
    except Exception as e:
        print(f"Error saving presets: {e}")


def load_presets() -> list:
    """Load presets from Fusion settings."""
    try:
        fusion = resolve.Fusion()  # noqa: F821
        raw = fusion.GetData(f"{PREFS_KEY}.presets")
        if not raw:
            return []
        return [preset_from_dict(d) for d in json.loads(raw)]
    except Exception:
        return []


def save_default_preset_name(name: Optional[str]) -> None:
    """Save (or clear) the default preset name."""
    try:
        fusion = resolve.Fusion()  # noqa: F821
        fusion.SetData(f"{PREFS_KEY}.default_preset", name or "")
    except Exception:
        pass


def load_default_preset_name() -> str:
    """Load the default preset name, or empty string if none."""
    try:
        fusion = resolve.Fusion()  # noqa: F821
        return fusion.GetData(f"{PREFS_KEY}.default_preset") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# PySide6 UI
# ---------------------------------------------------------------------------

DARK_STYLE = """
QDialog, QWidget {
    background-color: #2b2b2b;
    color: #cccccc;
    font-size: 12px;
}
QGroupBox {
    border: 1px solid #555555;
    border-radius: 3px;
    margin-top: 8px;
    padding-top: 14px;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 4px;
    color: #cccccc;
}
QLabel { color: #cccccc; }
QLineEdit, QComboBox {
    background-color: #3a3a3a;
    border: 1px solid #555555;
    border-radius: 3px;
    padding: 3px 6px;
    color: #cccccc;
}
QSpinBox {
    background-color: #3a3a3a;
    border: 1px solid #555555;
    border-radius: 3px;
    padding: 3px 6px;
    color: #cccccc;
    min-width: 50px;
}
QSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 16px;
    border-left: 1px solid #555555;
    background-color: #3a3a3a;
}
QSpinBox::up-button:hover { background-color: #4a4a4a; }
QSpinBox::up-button:pressed { background-color: #2a2a2a; }
QSpinBox::up-arrow { image: none; border: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid #cccccc; }
QSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 16px;
    border-left: 1px solid #555555;
    border-top: 1px solid #555555;
    background-color: #3a3a3a;
}
QSpinBox::down-button:hover { background-color: #4a4a4a; }
QSpinBox::down-button:pressed { background-color: #2a2a2a; }
QSpinBox::down-arrow { image: none; border: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #cccccc; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: #2d89ef;
}
QComboBox::drop-down {
    border: none;
    padding-right: 6px;
}
QComboBox QAbstractItemView {
    background-color: #3a3a3a;
    color: #cccccc;
    selection-background-color: #2d89ef;
}
QCheckBox { color: #cccccc; spacing: 4px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #555555; border-radius: 2px;
    background-color: #3a3a3a;
}
QCheckBox::indicator:checked {
    background-color: #2d89ef; border-color: #2d89ef;
}
QCheckBox:disabled, QLabel:disabled { color: #666666; }
QPushButton {
    padding: 4px 12px;
    background-color: #3a3a3a; color: #cccccc;
    border: 1px solid #555555; border-radius: 3px;
}
QPushButton:hover { background-color: #4a4a4a; border-color: #777777; }
QPushButton:pressed { background-color: #2a2a2a; }
QPushButton:disabled { background-color: #2d2d2d; color: #666666; border-color: #3a3a3a; }
QTreeWidget {
    background-color: #1e1e1e;
    alternate-background-color: #252525;
    border: 1px solid #555555;
    border-radius: 3px;
    color: #cccccc;
}
QTreeWidget::item:selected { background-color: #2d89ef; }
QTreeWidget QHeaderView::section {
    background-color: #333333;
    color: #cccccc;
    border: none;
    border-right: 1px solid #555555;
    padding: 3px 6px;
    font-weight: bold;
}
QListWidget {
    background-color: #1e1e1e;
    border: 1px solid #555555;
    border-radius: 3px;
    color: #cccccc;
}
QListWidget::item { padding: 0px; }
QListWidget::item:selected { background-color: transparent; }
QSplitter::handle {
    background-color: #555555;
    width: 3px;
}
QTabWidget::pane { border: 1px solid #555555; border-radius: 3px; top: -1px; }
QTabBar::tab {
    background: #333333; color: #aaaaaa;
    padding: 7px 18px; margin-right: 2px;
    border: 1px solid #555555; border-bottom: none;
    border-top-left-radius: 3px; border-top-right-radius: 3px;
    font-weight: bold; font-size: 13px;
}
QTabBar::tab:selected { background: #2b2b2b; color: #ffffff; }
QTabBar::tab:hover:!selected { background: #3a3a3a; }
"""

OP_ROW_STYLE = """
QWidget#OpRow { background: transparent; }
QWidget#OpRow:hover QLabel#OpIndex { color: #ffffff; }
QLabel#OpDesc { color: #cccccc; }
QLabel#OpIndex { color: #888888; font-weight: bold; min-width: 18px; }
QLabel#DragHandle { color: #666666; font-size: 14px; min-width: 16px; }
QPushButton#OpDelete {
    background: transparent; border: none; color: #666666;
    font-size: 16px; font-weight: bold; padding: 0px;
    min-width: 20px; max-width: 20px;
}
QPushButton#OpDelete:hover { color: #ff6b6b; }
"""

# Accent per tab: the banner, the tab and the Rename button share it, so the
# colour alone says which kind of clip is about to change.
TAB_ACCENT = {TAB_MEDIA_POOL: "#2d89ef", TAB_TIMELINE: "#e07b1f"}
TAB_ACCENT_DARK = {TAB_MEDIA_POOL: "#1b6ec2", TAB_TIMELINE: "#b8621a"}

BANNER_STYLE = (
    "QLabel {{ background-color: {bg}; color: #ffffff; border-radius: 3px;"
    " padding: 8px 10px; font-size: 12px; }}"
)



class OperationRowWidget(QWidget):
    """Custom widget for a single operation row in the list."""
    delete_clicked = Signal(int)
    toggled = Signal(int, bool)

    def __init__(self, index: int, op: RenameOp, parent=None):
        super().__init__(parent)
        self.setObjectName("OpRow")
        self.index = index
        self.op = op

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        self.enabled_cb = QCheckBox()
        self.enabled_cb.setChecked(op.enabled)
        self.enabled_cb.setToolTip("Enable / disable this operation")
        self.enabled_cb.setFixedWidth(18)
        self.enabled_cb.toggled.connect(self._on_toggled)
        layout.addWidget(self.enabled_cb)

        self.handle = QLabel("≡")
        self.handle.setObjectName("DragHandle")
        self.handle.setAlignment(Qt.AlignCenter)
        self.handle.setFixedWidth(16)
        layout.addWidget(self.handle)

        self.idx_label = QLabel(str(index + 1))
        self.idx_label.setObjectName("OpIndex")
        self.idx_label.setAlignment(Qt.AlignCenter)
        self.idx_label.setFixedWidth(20)
        layout.addWidget(self.idx_label)

        self.desc_label = QLabel(op.describe())
        self.desc_label.setObjectName("OpDesc")
        layout.addWidget(self.desc_label, stretch=1)

        delete_btn = QPushButton("×")
        delete_btn.setObjectName("OpDelete")
        delete_btn.setFixedSize(20, 20)
        delete_btn.setCursor(Qt.PointingHandCursor)
        delete_btn.setToolTip("Remove operation")
        delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.index))
        layout.addWidget(delete_btn)

        self.setStyleSheet(OP_ROW_STYLE)
        self._apply_enabled_style()

    def _on_toggled(self, checked: bool):
        self.op.enabled = checked
        self._apply_enabled_style()
        self.toggled.emit(self.index, checked)

    def _apply_enabled_style(self):
        if not self.op.enabled:
            self.desc_label.setStyleSheet("color: #666666; text-decoration: line-through;")
        else:
            self.desc_label.setStyleSheet("")

class DragDropOpsList(QListWidget):
    """QListWidget with drag-drop reordering and custom row widgets."""
    order_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSpacing(1)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.order_changed.emit()

class AttrFilterWidget(QWidget):
    """Clip Color / Flag / Marker filter combos."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.color = self._combo([FILTER_ANY, FILTER_NONE] + CLIP_COLORS,
                                 "Clip colour. 'None' means no clip colour set.")
        self.flag = self._combo([FILTER_ANY, FILTER_NONE] + FLAG_COLORS,
                                "Flag colour. 'None' means no flags.")
        self.marker = self._combo([FILTER_ANY, MARKER_HAS, MARKER_NO] + MARKER_COLORS,
                                  "A colour keeps clips that have at least one "
                                  "marker of that colour.")
        for label, combo in (("Clip Color:", self.color), ("Flag:", self.flag),
                             ("Marker:", self.marker)):
            row.addWidget(QLabel(label))
            row.addWidget(combo)
        row.addStretch()

    def _combo(self, values, tip):
        c = QComboBox()
        c.addItems(values)
        c.setToolTip(tip)
        c.currentIndexChanged.connect(lambda _i: self.changed.emit())
        return c

    def state(self) -> dict:
        return {"clip_color": self.color.currentText(),
                "flag": self.flag.currentText(),
                "marker": self.marker.currentText()}

    def set_state(self, state: Optional[dict]):
        state = state or DEFAULT_ATTR_FILTERS
        for key, combo in (("clip_color", self.color), ("flag", self.flag),
                           ("marker", self.marker)):
            idx = combo.findText(state.get(key, FILTER_ANY))
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)


def _make_app_icon():
    """Create a programmatic 32x32 icon for the Batch Rename tool."""
    size = 32
    pix = QPixmap(size, size)
    pix.fill(QColor("#1e1e1e"))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#5599ff"))
    pen.setWidth(2)
    p.setPen(pen)
    p.drawLine(6, 16, 22, 16)
    p.drawLine(19, 11, 25, 16)
    p.drawLine(19, 21, 25, 16)
    font = QFont("Segoe UI", 7, QFont.Bold)
    p.setFont(font)
    p.setPen(QColor("#ffffff"))
    p.drawText(4, 10, "BR")
    p.end()
    return QIcon(pix)


def _clear_layout(layout):
    while layout.count():
        w = layout.takeAt(0).widget()
        if w is not None:
            w.deleteLater()


def _make_tree(headers, widths):
    tree = QTreeWidget()
    tree.setHeaderLabels(headers)
    tree.setAlternatingRowColors(True)
    tree.setRootIsDecorated(False)
    tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
    tree.setSortingEnabled(False)  # the row order IS the numbering order
    for col, w in enumerate(widths):
        tree.setColumnWidth(col, w)
    return tree



class BatchRenameDialog(QDialog):
    """Main Batch Rename dialog using PySide6."""

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.project = project
        self.operations: list[RenameOp] = []
        self.undo_stack: list[UndoRecord] = []
        self.presets: list[RenamePreset] = load_presets()
        self.editing_index: Optional[int] = None
        self.default_preset: str = load_default_preset_name()

        # Media pool tab state
        self.mp_items: list = []          # fetched, unfiltered
        self.mp_shown: list = []          # filtered + sorted = numbering order
        self.mp_all_names: Optional[list] = None  # lazily, for "continue"

        # Timeline tab state
        self.tl_timeline = None
        self.tl_uid = ""
        self.tl_name = ""
        self.tl_all: list = []            # every video item on the timeline
        self.tl_scope_items: list = []    # after the selection scope
        self.tl_shown: list = []          # after track + attr filters
        self.tl_supported = False
        self.tl_scope_message = ""
        self.tl_track_checks: dict = {}

        self.setWindowIcon(_make_app_icon())
        self.resize(1450, 760)
        self.setMinimumSize(900, 560)
        self.setStyleSheet(DARK_STYLE)
        self._build_ui()
        self._connect_signals()
        self._initial_state()

        # Close the dialog if DaVinci Resolve exits.
        self._resolve_timer = QTimer(self)
        self._resolve_timer.timeout.connect(self._check_resolve_alive)
        self._resolve_timer.start(2000)

    # ------------------------------------------------------------------
    # Resolve lifecycle
    # ------------------------------------------------------------------

    def _check_resolve_alive(self):
        try:
            alive = resolve.GetProjectManager() is not None  # noqa: F821
        except Exception:
            alive = False
        if not alive:
            self._resolve_timer.stop()
            self.close()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 1020])

    def _build_left_panel(self):
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        # Operation builder
        builder_group = QGroupBox("Add Operation")
        builder_layout = QVBoxLayout(builder_group)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Type:"))
        self.op_type_combo = QComboBox()
        for ot in OpType:
            self.op_type_combo.addItem(ot.value)
        type_row.addWidget(self.op_type_combo, stretch=1)
        builder_layout.addLayout(type_row)

        # Search & Replace
        self.search_container = QWidget()
        search_layout = QVBoxLayout(self.search_container)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(4)
        find_row = QHBoxLayout()
        find_row.addWidget(QLabel("Find:"))
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Text to find")
        find_row.addWidget(self.search_field, stretch=1)
        search_layout.addLayout(find_row)
        replace_row = QHBoxLayout()
        replace_row.addWidget(QLabel("Replace:"))
        self.replace_field = QLineEdit()
        replace_row.addWidget(self.replace_field, stretch=1)
        search_layout.addLayout(replace_row)
        flags_row = QHBoxLayout()
        self.case_sensitive = QCheckBox("Case Sensitive")
        self.case_sensitive.setChecked(True)
        self.use_regex = QCheckBox("Use Regex")
        flags_row.addWidget(self.case_sensitive)
        flags_row.addWidget(self.use_regex)
        flags_row.addStretch()
        search_layout.addLayout(flags_row)
        builder_layout.addWidget(self.search_container)

        # Prefix / Suffix
        self.affix_container = QWidget()
        affix_layout = QHBoxLayout(self.affix_container)
        affix_layout.setContentsMargins(0, 0, 0, 0)
        affix_layout.addWidget(QLabel("Text:"))
        self.affix_field = QLineEdit()
        affix_layout.addWidget(self.affix_field, stretch=1)
        builder_layout.addWidget(self.affix_container)

        # Numbering
        self.num_container = QWidget()
        grid = QGridLayout(self.num_container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        self.num_template = QLineEdit("{n}")
        self.num_template.setToolTip(
            "{n} is the number. Other text and tokens are allowed, e.g. SH_{n}.")
        self.num_placement = QComboBox()
        for _key, label in PLACEMENT_LABELS:
            self.num_placement.addItem(label)
        self.num_separator = QLineEdit("_")
        self.num_separator.setMaximumWidth(50)
        self.num_padding = QSpinBox()
        self.num_padding.setRange(1, 10)
        self.num_padding.setValue(4)
        self.num_increment = QSpinBox()
        self.num_increment.setRange(1, 1000)
        self.num_increment.setValue(10)
        self.num_start_mode = QComboBox()
        self.num_start_mode.addItems(["Start at", "Continue after highest existing"])
        self.num_start_mode.setToolTip(
            "Continue looks at every current name (the whole timeline, or the whole "
            "Media Pool) for numbers made by this template, and starts one "
            "increment past the highest. If none are found it uses the start number.")
        self.num_start = QSpinBox()
        self.num_start.setRange(0, 999999999)
        self.num_start.setValue(10)
        self.num_next_free = QPushButton("Next free")
        self.num_next_free.setToolTip(
            "Fill in the number after the highest one already in use in this tab.")
        self.num_count_over = QComboBox()
        self.num_count_over.addItems(["Clips shown in the preview",
                                      "Every clip on the timeline"])
        self.num_count_over.setToolTip(
            "Timeline tab only. 'Every clip on the timeline' numbers by position on "
            "the whole timeline, so a selected or filtered subset gets the same "
            "numbers a full run would give it (Shot Numbering's 'Keep full-timeline "
            "numbers'). The Media Pool tab always counts the clips shown.")
        grid.addWidget(QLabel("Template:"), 0, 0)
        grid.addWidget(self.num_template, 0, 1, 1, 3)
        grid.addWidget(QLabel("Placement:"), 1, 0)
        grid.addWidget(self.num_placement, 1, 1)
        grid.addWidget(QLabel("Separator:"), 1, 2)
        grid.addWidget(self.num_separator, 1, 3)
        grid.addWidget(QLabel("Padding:"), 2, 0)
        grid.addWidget(self.num_padding, 2, 1)
        grid.addWidget(QLabel("Increment:"), 2, 2)
        grid.addWidget(self.num_increment, 2, 3)
        grid.addWidget(QLabel("Start:"), 3, 0)
        grid.addWidget(self.num_start_mode, 3, 1, 1, 3)
        grid.addWidget(QLabel("Number:"), 4, 0)
        grid.addWidget(self.num_start, 4, 1, 1, 2)
        grid.addWidget(self.num_next_free, 4, 3)
        grid.addWidget(QLabel("Count over:"), 5, 0)
        grid.addWidget(self.num_count_over, 5, 1, 1, 3)
        grid.setColumnStretch(1, 1)
        builder_layout.addWidget(self.num_container)

        # Change Case
        self.case_container = QWidget()
        case_layout = QHBoxLayout(self.case_container)
        case_layout.setContentsMargins(0, 0, 0, 0)
        case_layout.addWidget(QLabel("Mode:"))
        self.case_mode = QComboBox()
        for _key, label in CASE_LABELS:
            self.case_mode.addItem(label)
        case_layout.addWidget(self.case_mode, stretch=1)
        self.case_skip_ext = QCheckBox("Keep extension as is")
        self.case_skip_ext.setChecked(True)
        case_layout.addWidget(self.case_skip_ext)
        builder_layout.addWidget(self.case_container)

        # Clean Name
        self.clean_container = QWidget()
        clean_layout = QHBoxLayout(self.clean_container)
        clean_layout.setContentsMargins(0, 0, 0, 0)
        self.clean_ext = QCheckBox("Strip file extension (.mov, .R3D)")
        self.clean_ext.setChecked(True)
        self.clean_seq = QCheckBox("Strip sequence range [0001-0100]")
        self.clean_seq.setChecked(True)
        clean_layout.addWidget(self.clean_ext)
        clean_layout.addWidget(self.clean_seq)
        clean_layout.addStretch()
        builder_layout.addWidget(self.clean_container)

        # Remove
        self.remove_container = QWidget()
        remove_layout = QHBoxLayout(self.remove_container)
        remove_layout.setContentsMargins(0, 0, 0, 0)
        remove_layout.addWidget(QLabel("Count:"))
        self.remove_count = QSpinBox()
        self.remove_count.setRange(1, 999)
        self.remove_count.setValue(1)
        remove_layout.addWidget(self.remove_count)
        self.position_label = QLabel("Position:")
        remove_layout.addWidget(self.position_label)
        self.remove_position = QSpinBox()
        self.remove_position.setRange(0, 999)
        remove_layout.addWidget(self.remove_position)
        remove_layout.addStretch()
        builder_layout.addWidget(self.remove_container)

        self.builder_message = QLabel("")
        self.builder_message.setStyleSheet("color: #ff6b6b;")
        self.builder_message.setWordWrap(True)
        builder_layout.addWidget(self.builder_message)

        btn_row = QHBoxLayout()
        self.add_op_btn = QPushButton("Add Operation")
        self.cancel_edit_btn = QPushButton("Cancel Edit")
        self.cancel_edit_btn.setEnabled(False)
        btn_row.addWidget(self.add_op_btn)
        btn_row.addWidget(self.cancel_edit_btn)
        btn_row.addStretch()
        builder_layout.addLayout(btn_row)
        left_layout.addWidget(builder_group)

        # Operations pipeline
        ops_group = QGroupBox("Operation Pipeline (used by both tabs)")
        ops_layout = QVBoxLayout(ops_group)
        self.ops_list = DragDropOpsList()
        ops_layout.addWidget(self.ops_list)
        ops_btn_row = QHBoxLayout()
        self.move_up_btn = QPushButton("Move Up")
        self.move_down_btn = QPushButton("Move Down")
        self.edit_op_btn = QPushButton("Edit")
        self.clear_ops_btn = QPushButton("Clear All")
        for b in (self.move_up_btn, self.move_down_btn, self.edit_op_btn, self.clear_ops_btn):
            ops_btn_row.addWidget(b)
        ops_btn_row.addStretch()
        ops_layout.addLayout(ops_btn_row)
        left_layout.addWidget(ops_group, stretch=1)

        # Presets
        preset_group = QGroupBox("Presets")
        preset_layout = QVBoxLayout(preset_group)
        preset_top = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        preset_top.addWidget(self.preset_combo, stretch=1)
        self.preset_name = QLineEdit()
        self.preset_name.setPlaceholderText("Preset name")
        preset_top.addWidget(self.preset_name, stretch=1)
        preset_layout.addLayout(preset_top)
        preset_btn_row = QHBoxLayout()
        self.save_preset_btn = QPushButton("Save")
        self.load_preset_btn = QPushButton("Load")
        self.delete_preset_btn = QPushButton("Delete")
        self.default_preset_btn = QPushButton("Set as Default")
        for b in (self.save_preset_btn, self.load_preset_btn,
                  self.delete_preset_btn, self.default_preset_btn):
            preset_btn_row.addWidget(b)
        preset_btn_row.addStretch()
        preset_layout.addLayout(preset_btn_row)
        left_layout.addWidget(preset_group)

        # Action buttons
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 10, 6, 0)
        self.rename_btn = QPushButton("Rename")
        self.rename_btn.setMinimumHeight(44)
        self.undo_btn = QPushButton("Undo Last (0)")
        self.undo_btn.setMinimumHeight(44)
        self.undo_btn.setMinimumWidth(120)
        action_row.addWidget(self.rename_btn, stretch=1)
        action_row.addWidget(self.undo_btn)
        left_layout.addLayout(action_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #999999;")
        left_layout.addWidget(self.status_label)
        return left

    def _build_right_panel(self):
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_mp_tab(), "Media Pool Clips")
        self.tabs.addTab(self._build_tl_tab(), "Timeline Clips")
        self.tabs.setTabToolTip(0, "Rename source clips in the Media Pool")
        self.tabs.setTabToolTip(1, "Rename clip instances on the current timeline only")
        return self.tabs

    def _preview_header(self, refresh_btn, collision_label):
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 2)
        lbl = QLabel("Preview")
        lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        row.addWidget(lbl)
        row.addWidget(refresh_btn)
        row.addWidget(collision_label, stretch=1)
        return row

    def _build_mp_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.mp_banner = QLabel(
            "<b>MEDIA POOL CLIPS</b> — renames the <b>source clip</b> in the "
            "Media Pool. Its uses on timelines show the new name too, unless they were "
            "renamed on the timeline before.")
        self.mp_banner.setWordWrap(True)
        self.mp_banner.setStyleSheet(BANNER_STYLE.format(bg=TAB_ACCENT[TAB_MEDIA_POOL]))
        lay.addWidget(self.mp_banner)

        src = QLabel("Source: the clips selected in the Media Pool (or the clips in "
                     "selected bins). With nothing selected, the current bin.")
        src.setWordWrap(True)
        src.setStyleSheet("color: #999999;")
        lay.addWidget(src)

        filter_group = QGroupBox("Filters")
        fl = QVBoxLayout(filter_group)
        type_row = QHBoxLayout()
        self.filter_checks = {}
        for _, label, _ in CLIP_TYPE_FILTERS:
            cb = QCheckBox(label)
            cb.setChecked(True)
            self.filter_checks[label] = cb
            type_row.addWidget(cb)
        cb_other = QCheckBox("Other")
        cb_other.setChecked(True)
        self.filter_checks["Other"] = cb_other
        type_row.addWidget(cb_other)
        type_row.addStretch()
        fl.addLayout(type_row)
        self.mp_attr = AttrFilterWidget()
        fl.addWidget(self.mp_attr)
        opt_row = QHBoxLayout()
        self.include_subfolders = QCheckBox("Include Subfolders")
        opt_row.addWidget(self.include_subfolders)
        opt_row.addSpacing(20)
        opt_row.addWidget(QLabel("Sort by (= numbering order):"))
        self.mp_sort = QComboBox()
        self.mp_sort.addItems(MP_SORT_MODES)
        opt_row.addWidget(self.mp_sort)
        opt_row.addStretch()
        fl.addLayout(opt_row)
        lay.addWidget(filter_group)

        self.mp_refresh_btn = QPushButton("Refresh from Resolve")
        self.mp_collision = QLabel("")
        lay.addLayout(self._preview_header(self.mp_refresh_btn, self.mp_collision))
        self.mp_tree = _make_tree(["Type", "Color", "Original Name", "New Name"],
                                  [100, 70, 300, 300])
        lay.addWidget(self.mp_tree, stretch=1)
        return tab

    def _build_tl_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.tl_banner = QLabel("")
        self.tl_banner.setWordWrap(True)
        self.tl_banner.setStyleSheet(BANNER_STYLE.format(bg=TAB_ACCENT[TAB_TIMELINE]))
        lay.addWidget(self.tl_banner)

        filter_group = QGroupBox("Scope && Filters")
        fl = QVBoxLayout(filter_group)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Apply to:"))
        self.tl_scope = QComboBox()
        self.tl_scope.addItems(["Whole timeline", "Selected timeline clips only"])
        self.tl_scope.setToolTip(
            "The timeline selection is read when you press Refresh and again when "
            "you press Rename.")
        scope_row.addWidget(self.tl_scope)
        self.tl_scope_note = QLabel("")
        self.tl_scope_note.setStyleSheet("color: #999999;")
        scope_row.addWidget(self.tl_scope_note, stretch=1)
        fl.addLayout(scope_row)

        track_row = QHBoxLayout()
        track_row.addWidget(QLabel("Video tracks:"))
        self.tl_tracks_box = QWidget()
        self.tl_tracks_layout = QHBoxLayout(self.tl_tracks_box)
        self.tl_tracks_layout.setContentsMargins(0, 0, 0, 0)
        track_row.addWidget(self.tl_tracks_box)
        track_row.addStretch()
        fl.addLayout(track_row)

        self.tl_attr = AttrFilterWidget()
        fl.addWidget(self.tl_attr)
        lay.addWidget(filter_group)

        self.tl_refresh_btn = QPushButton("Refresh from Resolve")
        self.tl_collision = QLabel("")
        header = self._preview_header(self.tl_refresh_btn, self.tl_collision)
        self.tl_restore_btn = QPushButton("Restore Media Pool Names")
        self.tl_restore_btn.setToolTip(
            "Set the timeline clips in scope (the selected preview rows, or every "
            "row shown) back to their Media Pool clip's name. Can be undone. "
            "Resolve has no way to re-link a clip's name, so this writes the name as "
            "text: a later Media Pool rename will not reach these clips.")
        header.addWidget(self.tl_restore_btn)
        lay.addLayout(header)
        self.tl_tree = _make_tree(
            ["Track", "Start", "Color", "Media Pool Clip", "Current Name", "New Name"],
            [55, 95, 70, 230, 230, 260])
        lay.addWidget(self.tl_tree, stretch=1)
        return tab

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self.op_type_combo.currentIndexChanged.connect(self._on_op_type_changed)
        self.add_op_btn.clicked.connect(self._on_add_operation)
        self.cancel_edit_btn.clicked.connect(self._on_cancel_edit)
        self.move_up_btn.clicked.connect(self._on_move_up)
        self.move_down_btn.clicked.connect(self._on_move_down)
        self.edit_op_btn.clicked.connect(self._on_edit_operation)
        self.clear_ops_btn.clicked.connect(self._on_clear_operations)
        self.ops_list.itemDoubleClicked.connect(self._on_ops_double_click)
        self.ops_list.order_changed.connect(self._on_ops_reordered)
        self.rename_btn.clicked.connect(self._on_rename)
        self.undo_btn.clicked.connect(self._on_undo)
        self.save_preset_btn.clicked.connect(self._on_save_preset)
        self.load_preset_btn.clicked.connect(self._on_load_preset)
        self.delete_preset_btn.clicked.connect(self._on_delete_preset)
        self.default_preset_btn.clicked.connect(self._on_default_preset)
        self.preset_combo.currentIndexChanged.connect(self._update_default_button)
        self.num_next_free.clicked.connect(self._on_next_free)
        self.num_start_mode.currentIndexChanged.connect(self._on_op_type_changed)
        self.num_placement.currentIndexChanged.connect(self._on_op_type_changed)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Media pool tab
        self.mp_refresh_btn.clicked.connect(self._refresh_mp)
        self.include_subfolders.stateChanged.connect(self._refresh_mp)
        for cb in self.filter_checks.values():
            cb.stateChanged.connect(self._update_preview)
        self.mp_attr.changed.connect(self._update_preview)
        self.mp_sort.currentIndexChanged.connect(self._update_preview)
        self.mp_tree.itemSelectionChanged.connect(self._update_rename_button)

        # Timeline tab
        self.tl_refresh_btn.clicked.connect(self._refresh_tl)
        self.tl_scope.currentIndexChanged.connect(self._refresh_tl)
        self.tl_attr.changed.connect(self._update_preview)
        self.tl_restore_btn.clicked.connect(self._on_restore)
        self.tl_tree.itemSelectionChanged.connect(self._update_rename_button)

    # ------------------------------------------------------------------
    # Initial state
    # ------------------------------------------------------------------

    def _initial_state(self):
        self._refresh_preset_combo()
        self._on_op_type_changed()
        self._refresh_mp(update=False)
        self._refresh_tl(update=False)

        if self.default_preset:
            for i, preset in enumerate(self.presets):
                if preset.name == self.default_preset:
                    self.preset_combo.setCurrentIndex(i)
                    self._apply_preset(preset)
                    print(f"Loaded default preset: {preset.name}")
                    break
        self._update_default_button()
        self._on_tab_changed()

    # ------------------------------------------------------------------
    # Tabs
    # ------------------------------------------------------------------

    def _tab(self) -> str:
        return TAB_TIMELINE if self.tabs.currentIndex() == 1 else TAB_MEDIA_POOL

    def _on_tab_changed(self, *_):
        tab = self._tab()
        label = "Timeline Clips" if tab == TAB_TIMELINE else "Media Pool Clips"
        self.setWindowTitle(f"Batch Rename v{SCRIPT_VERSION} — {label}")
        hint = "Tokens: {date} {time} {year} {month} {day}..."
        if tab == TAB_TIMELINE:
            hint = "Tokens: {source} {track} {timeline} {date} {time}..."
        self.affix_field.setPlaceholderText(hint)
        self.replace_field.setPlaceholderText("Replace with — " + hint)
        self._update_preview()

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _refresh_mp(self, *_, update: bool = True):
        selected = get_selected_media_pool_items(self.project)
        if selected:
            self.mp_items = selected
        else:
            self.mp_items = get_media_pool_items(
                self.project, self.include_subfolders.isChecked())
        self.mp_all_names = None
        if update:
            self._update_preview()

    def _mp_names_for_continue(self) -> list:
        if self.mp_all_names is None:
            self.mp_all_names = get_all_media_pool_names(self.project)
        return self.mp_all_names

    def _refresh_tl(self, *_, update: bool = True):
        """Re-read the current timeline, its items and the timeline selection."""
        tl = _call(self.project, "GetCurrentTimeline")
        self.tl_timeline = tl
        self.tl_uid = _call(tl, "GetUniqueId", default="") if tl else ""
        self.tl_name = _call(tl, "GetName", default="") if tl else ""
        self.tl_supported = tl is not None and timeline_rename_supported(resolve, tl)  # noqa: F821
        self.tl_all = get_timeline_items(tl) if tl else []
        self.tl_scope_items, self.tl_scope_message = self._apply_scope(tl, self.tl_all)
        self._rebuild_track_checks(tl)
        self._update_tl_banner()
        if update:
            self._update_preview()

    def _apply_scope(self, tl, items):
        """(items in scope, note). A selection scope never falls back to the
        whole timeline: renaming everything when nothing is selected would be a
        surprise."""
        has_api = has_timeline_selection_api(tl)
        self.tl_scope.model().item(1).setEnabled(has_api or tl is None)
        if self.tl_scope.currentIndex() != 1:
            return items, ""
        if not has_api:
            return [], "Needs DaVinci Resolve 21.0.4+ (Timeline.GetSelectedClips)."
        selection = get_timeline_selection(tl) or []
        picked, ignored = filter_to_selection(items, selection)
        if not selection:
            return [], "Nothing is selected on the timeline. Select clips, then Refresh."
        note = f"{len(picked)} selected video clip(s)"
        if ignored:
            note += f"; {ignored} audio/other item(s) ignored"
        return picked, note

    def _rebuild_track_checks(self, tl):
        previous = {t: cb.isChecked() for t, cb in self.tl_track_checks.items()}
        _clear_layout(self.tl_tracks_layout)
        self.tl_track_checks = {}
        count = int(_call(tl, "GetTrackCount", "video", default=0) or 0) if tl else 0
        for t in range(1, count + 1):
            name = _call(tl, "GetTrackName", "video", t, default="") or ""
            label = f"V{t}" if not name or name == f"Video {t}" else f"V{t} {name}"
            cb = QCheckBox(label)
            cb.setChecked(previous.get(t, True))
            cb.stateChanged.connect(self._update_preview)
            self.tl_tracks_layout.addWidget(cb)
            self.tl_track_checks[t] = cb
        if not count:
            self.tl_tracks_layout.addWidget(QLabel("(no timeline)"))

    def _update_tl_banner(self):
        if self.tl_timeline is None:
            text = ("<b>TIMELINE CLIPS</b> — no timeline is open. Open one and "
                    "press Refresh.")
        elif not self.tl_supported:
            text = ("<b>TIMELINE CLIPS</b> — renaming timeline clips needs "
                    "DaVinci Resolve 20.2 or newer (TimelineItem.SetName).")
        else:
            text = (f"<b>TIMELINE CLIPS</b> — renames clip instances on timeline "
                    f"<b>“{self.tl_name}”</b> only. Media Pool clips are "
                    "<b>not</b> changed. Two uses of the same source can get "
                    "different names, and a renamed clip keeps its own name from then "
                    "on: it no longer follows Media Pool renames.")
        self.tl_banner.setText(text)
        ok = self.tl_timeline is not None and self.tl_supported
        bg = TAB_ACCENT[TAB_TIMELINE] if ok else "#8a2d2d"
        self.tl_banner.setStyleSheet(BANNER_STYLE.format(bg=bg))
        self.tl_scope_note.setText(self.tl_scope_message)

    # ------------------------------------------------------------------
    # Filtering / computing
    # ------------------------------------------------------------------

    def _active_type_labels(self) -> set:
        return {label for label, cb in self.filter_checks.items() if cb.isChecked()}

    def _mp_shown_from(self, items):
        filtered = filter_media_pool_items(items, self._active_type_labels(),
                                           self.mp_attr.state())
        return sort_media_pool_items(filtered, self.mp_sort.currentText())

    def _tl_tracks(self):
        if not self.tl_track_checks:
            return None
        return {t for t, cb in self.tl_track_checks.items() if cb.isChecked()}

    def _tl_shown_from(self, items):
        return filter_timeline_items(items, self._tl_tracks(), self.tl_attr.state())

    def _compute(self, tab, shown, all_items=None):
        """New names for `shown`, exactly as the preview shows them."""
        now = datetime.now()
        if tab == TAB_TIMELINE:
            all_items = self.tl_all if all_items is None else all_items
            all_names = [i["name"] for i in all_items]
            full_index_of = {i["uid"]: n for n, i in enumerate(all_items)}
            return compute_new_names(shown, self.operations, now, all_names, full_index_of)
        all_names = (self._mp_names_for_continue()
                     if pipeline_needs_all_names(self.operations) else [])
        return compute_new_names(shown, self.operations, now, all_names)

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _update_preview(self, *_):
        self.mp_shown = self._mp_shown_from(self.mp_items)
        self._fill_tree(TAB_MEDIA_POOL, self.mp_tree, self.mp_collision, self.mp_shown)
        self.tl_shown = self._tl_shown_from(self.tl_scope_items)
        self._fill_tree(TAB_TIMELINE, self.tl_tree, self.tl_collision, self.tl_shown)
        self._update_rename_button()

    def _row_values(self, tab, info, new):
        color = info.get("clip_color", "")
        if tab == TAB_TIMELINE:
            return [f"V{info['track']}", self._tl_tc(info["start"]), color,
                    info.get("source_name", ""), info["name"], new]
        return [info["type_label"], color, info["name"], new]

    def _tl_tc(self, frame):
        tl = self.tl_timeline
        try:
            fps = float(_call(tl, "GetSetting", "timelineFrameRate", default=25) or 25)
        except (TypeError, ValueError):
            fps = 25.0
        df = str(_call(tl, "GetSetting", "timelineDropFrameTimecode", default="0")) in ("1", "True")
        return frames_to_tc(frame, fps, df)

    def _fill_tree(self, tab, tree, collision_label, shown):
        tree.blockSignals(True)
        tree.clear()
        collision_label.setText("")
        collision_label.setStyleSheet("")
        new_names = self._compute(tab, shown) if self.operations else [i["name"] for i in shown]
        counts = Counter(new_names)
        collisions = {n for n, c in counts.items() if c > 1}
        if collisions and self.operations:
            affected = sum(1 for n in new_names if n in collisions)
            collision_label.setText(
                f"WARNING: {len(collisions)} name collision(s) affecting {affected} clips")
            collision_label.setStyleSheet("color: #FF6B6B; font-weight: bold;")
        new_col = 5 if tab == TAB_TIMELINE else 3
        for idx, (info, new) in enumerate(zip(shown, new_names)):
            clash = self.operations and new in collisions
            display = new + "  [COLLISION]" if clash else new
            tw = QTreeWidgetItem(tree, self._row_values(tab, info, display))
            tw.setData(0, Qt.UserRole, idx)
            if clash:
                tw.setForeground(new_col, QColor("#FF6B6B"))
            elif new != info["name"]:
                tw.setForeground(new_col, QColor("#8fd18f"))
        tree.blockSignals(False)

    def _tree(self, tab):
        return self.tl_tree if tab == TAB_TIMELINE else self.mp_tree

    def _selected_uids(self, tab) -> list:
        shown = self.tl_shown if tab == TAB_TIMELINE else self.mp_shown
        uids = []
        for tw in self._tree(tab).selectedItems():
            idx = tw.data(0, Qt.UserRole)
            if idx is not None and 0 <= idx < len(shown):
                uids.append(shown[idx]["uid"])
        return uids

    def _update_rename_button(self, *_):
        tab = self._tab()
        sel = len(self._tree(tab).selectedItems())
        shown = self.tl_shown if tab == TAB_TIMELINE else self.mp_shown
        n = sel or len(shown)
        noun = "Timeline Clip" if tab == TAB_TIMELINE else "Media Pool Clip"
        text = f"Rename {n}{' Selected' if sel else ''} {noun}{'s' if n != 1 else ''}"
        if tab == TAB_TIMELINE and shown:
            rows = shown
            if sel:
                keep = set(self._selected_uids(tab))
                rows = [i for i in shown if i["uid"] in keep]
            tracks = track_range_label(rows)
            if tracks:
                text += f" ({tracks})"
        self.rename_btn.setText(f"  {text}  ")
        accent, dark = TAB_ACCENT[tab], TAB_ACCENT_DARK[tab]
        self.rename_btn.setStyleSheet(
            "QPushButton { font-weight: bold; font-size: 14px; padding: 8px 20px;"
            f" background-color: {accent}; color: #ffffff; border: none;"
            " border-radius: 3px; }"
            f"QPushButton:hover {{ background-color: {dark}; }}"
            "QPushButton:disabled { background-color: #3a3a3a; color: #777777; }")
        blocked = tab == TAB_TIMELINE and (self.tl_timeline is None or not self.tl_supported)
        self.rename_btn.setEnabled(not blocked)
        self.tl_restore_btn.setEnabled(self.tl_timeline is not None and self.tl_supported)

    def _status(self, text, error=False):
        print(text)
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #ff6b6b;" if error else "color: #8fd18f;")

    # ------------------------------------------------------------------
    # Operations list
    # ------------------------------------------------------------------

    def _refresh_ops_list(self):
        self.ops_list.clear()
        for i, op in enumerate(self.operations):
            item = QListWidgetItem(self.ops_list)
            widget = OperationRowWidget(i, op)
            widget.delete_clicked.connect(self._on_delete_op_row)
            widget.toggled.connect(self._on_op_toggled)
            item.setSizeHint(widget.sizeHint())
            self.ops_list.setItemWidget(item, widget)

    def _on_delete_op_row(self, index: int):
        if 0 <= index < len(self.operations):
            self.operations.pop(index)
            self._refresh_ops_list()
            self._update_preview()

    def _on_op_toggled(self, index: int, enabled: bool):
        if 0 <= index < len(self.operations):
            self.operations[index].enabled = enabled
            self._update_preview()

    def _on_ops_reordered(self):
        new_ops = []
        for i in range(self.ops_list.count()):
            widget = self.ops_list.itemWidget(self.ops_list.item(i))
            if widget and hasattr(widget, "op"):
                new_ops.append(widget.op)
        if len(new_ops) == len(self.operations):
            self.operations = new_ops
        self._refresh_ops_list()
        self._update_preview()

    def _get_selected_op_index(self) -> Optional[int]:
        row = self.ops_list.currentRow()
        if row < 0 or row >= len(self.operations):
            return None
        return row

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def _refresh_preset_combo(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in self.presets:
            self.preset_combo.addItem(preset.name)
        self.preset_combo.blockSignals(False)

    def _apply_preset(self, preset: RenamePreset):
        self.operations = [deserialize_op(d) for d in preset.operations]
        for cb_id, label, _ in CLIP_TYPE_FILTERS:
            if cb_id in preset.filters:
                self.filter_checks[label].setChecked(preset.filters[cb_id])
        if "FilterOther" in preset.filters:
            self.filter_checks["Other"].setChecked(preset.filters["FilterOther"])
        self.include_subfolders.blockSignals(True)
        self.include_subfolders.setChecked(preset.include_subfolders)
        self.include_subfolders.blockSignals(False)
        extra = preset.extra or {}
        self.mp_attr.set_state(extra.get("mp_attr"))
        self.tl_attr.set_state(extra.get("tl_attr"))
        sort_idx = self.mp_sort.findText(extra.get("mp_sort", MP_SORT_MODES[0]))
        self.mp_sort.setCurrentIndex(max(0, sort_idx))
        self.tl_scope.blockSignals(True)
        self.tl_scope.setCurrentIndex(1 if extra.get("tl_scope") == SCOPE_SELECTED else 0)
        self.tl_scope.blockSignals(False)
        self.tabs.setCurrentIndex(1 if preset.tab == TAB_TIMELINE else 0)
        self._refresh_ops_list()
        self._refresh_mp(update=False)
        self._refresh_tl(update=False)
        self._update_preview()
        self._update_default_button()

    def _update_default_button(self):
        idx = self.preset_combo.currentIndex()
        if 0 <= idx < len(self.presets) and self.presets[idx].name == self.default_preset:
            self.default_preset_btn.setText("Remove Default")
            return
        self.default_preset_btn.setText("Set as Default")

    # ------------------------------------------------------------------
    # Operation builder
    # ------------------------------------------------------------------

    def _on_op_type_changed(self, *_):
        ot = self.op_type_combo.currentText()
        self.search_container.setVisible(ot == OpType.SEARCH_REPLACE.value)
        self.affix_container.setVisible(ot in (OpType.PREFIX.value, OpType.SUFFIX.value))
        self.num_container.setVisible(ot == OpType.NUMBERING.value)
        self.case_container.setVisible(ot == OpType.CHANGE_CASE.value)
        self.clean_container.setVisible(ot == OpType.CLEAN_NAME.value)
        is_remove = ot in (OpType.REMOVE_START.value, OpType.REMOVE_END.value,
                           OpType.REMOVE_POSITION.value)
        is_pos = ot == OpType.REMOVE_POSITION.value
        self.remove_container.setVisible(is_remove)
        self.position_label.setVisible(is_pos)
        self.remove_position.setVisible(is_pos)
        self.num_separator.setEnabled(self.num_placement.currentIndex() != 0)
        self.builder_message.setText("")

    def _numbering_op_from_fields(self) -> RenameOp:
        op = RenameOp(op_type=OpType.NUMBERING)
        op.num_template = self.num_template.text() or NUMBER_TOKEN
        op.num_placement = PLACEMENT_LABELS[self.num_placement.currentIndex()][0]
        op.num_separator = self.num_separator.text()
        op.num_padding = self.num_padding.value()
        op.num_increment = self.num_increment.value()
        op.num_start = self.num_start.value()
        op.num_start_mode = START_CONTINUE if self.num_start_mode.currentIndex() == 1 else START_AT
        op.num_count_over = COUNT_TIMELINE if self.num_count_over.currentIndex() == 1 else COUNT_SHOWN
        return op

    def _build_op_from_fields(self) -> Optional[RenameOp]:
        try:
            op_type = OpType(self.op_type_combo.currentText())
        except ValueError:
            return None
        if op_type == OpType.NUMBERING:
            op = self._numbering_op_from_fields()
            if NUMBER_TOKEN not in op.num_template:
                self.builder_message.setText("The template needs {n} where the number goes.")
                return None
            return op
        op = RenameOp(op_type=op_type)
        if op_type == OpType.SEARCH_REPLACE:
            op.search_text = self.search_field.text()
            op.replace_text = self.replace_field.text()
            op.case_sensitive = self.case_sensitive.isChecked()
            op.use_regex = self.use_regex.isChecked()
            if not op.search_text:
                self.builder_message.setText("The Find field is empty.")
                return None
            if op.use_regex:
                try:
                    re.compile(op.search_text)
                except re.error as e:
                    self.builder_message.setText(f"Invalid regex: {e}")
                    return None
        elif op_type in (OpType.PREFIX, OpType.SUFFIX):
            op.affix_text = self.affix_field.text()
            if not op.affix_text:
                self.builder_message.setText("The Text field is empty.")
                return None
        elif op_type == OpType.CHANGE_CASE:
            op.case_mode = CASE_LABELS[self.case_mode.currentIndex()][0]
            op.case_skip_ext = self.case_skip_ext.isChecked()
        elif op_type == OpType.CLEAN_NAME:
            op.clean_extension = self.clean_ext.isChecked()
            op.clean_seq_range = self.clean_seq.isChecked()
        elif op_type in (OpType.REMOVE_START, OpType.REMOVE_END):
            op.remove_count = self.remove_count.value()
        elif op_type == OpType.REMOVE_POSITION:
            op.remove_count = self.remove_count.value()
            op.remove_position = self.remove_position.value()
        return op

    def _on_next_free(self):
        op = self._numbering_op_from_fields()
        op.num_start_mode = START_CONTINUE
        if self._tab() == TAB_TIMELINE:
            names = [i["name"] for i in self.tl_all]
        else:
            names = self._mp_names_for_continue()
        highest = highest_existing_number(op, names)
        value = op.num_increment if highest is None else highest + op.num_increment
        self.num_start.setValue(value)
        self.num_start_mode.setCurrentIndex(0)
        where = "this timeline" if self._tab() == TAB_TIMELINE else "the Media Pool"
        self.builder_message.setStyleSheet("color: #999999;")
        self.builder_message.setText(
            f"Highest existing on {where}: {highest}" if highest is not None
            else f"No names on {where} match “{op.num_template}” yet.")

    def _on_add_operation(self):
        self.builder_message.setStyleSheet("color: #ff6b6b;")
        op = self._build_op_from_fields()
        if op is None:
            return
        if self.editing_index is not None:
            op.enabled = self.operations[self.editing_index].enabled
            self.operations[self.editing_index] = op
            self._exit_edit_mode()
        else:
            self.operations.append(op)
        self._refresh_ops_list()
        self._update_preview()

    def _enter_edit_mode(self, idx: int):
        op = self.operations[idx]
        self.editing_index = idx
        self.op_type_combo.setCurrentIndex(list(OpType).index(op.op_type))
        self.search_field.setText(op.search_text)
        self.replace_field.setText(op.replace_text)
        self.case_sensitive.setChecked(op.case_sensitive)
        self.use_regex.setChecked(op.use_regex)
        self.affix_field.setText(op.affix_text)
        self.remove_count.setValue(max(1, op.remove_count))
        self.remove_position.setValue(op.remove_position)
        self.num_template.setText(op.num_template)
        self.num_placement.setCurrentIndex(
            [k for k, _ in PLACEMENT_LABELS].index(op.num_placement)
            if op.num_placement in dict(PLACEMENT_LABELS) else 0)
        self.num_separator.setText(op.num_separator)
        self.num_padding.setValue(op.num_padding)
        self.num_increment.setValue(op.num_increment)
        self.num_start.setValue(op.num_start)
        self.num_start_mode.setCurrentIndex(1 if op.num_start_mode == START_CONTINUE else 0)
        self.num_count_over.setCurrentIndex(1 if op.num_count_over == COUNT_TIMELINE else 0)
        self.case_mode.setCurrentIndex(
            [k for k, _ in CASE_LABELS].index(op.case_mode)
            if op.case_mode in dict(CASE_LABELS) else 0)
        self.case_skip_ext.setChecked(op.case_skip_ext)
        self.clean_ext.setChecked(op.clean_extension)
        self.clean_seq.setChecked(op.clean_seq_range)
        self.add_op_btn.setText("Apply Edit")
        self.cancel_edit_btn.setEnabled(True)
        self._on_op_type_changed()

    def _exit_edit_mode(self):
        self.editing_index = None
        self.add_op_btn.setText("Add Operation")
        self.cancel_edit_btn.setEnabled(False)

    def _on_cancel_edit(self):
        self._exit_edit_mode()

    def _on_edit_operation(self):
        idx = self._get_selected_op_index()
        if idx is not None:
            self._enter_edit_mode(idx)

    def _on_ops_double_click(self, item):
        idx = self.ops_list.row(item)
        if 0 <= idx < len(self.operations):
            self._enter_edit_mode(idx)

    def _on_move_up(self):
        idx = self._get_selected_op_index()
        if idx is None or idx == 0:
            return
        self.operations[idx], self.operations[idx - 1] = (
            self.operations[idx - 1], self.operations[idx])
        self._refresh_ops_list()
        self.ops_list.setCurrentRow(idx - 1)
        self._update_preview()

    def _on_move_down(self):
        idx = self._get_selected_op_index()
        if idx is None or idx >= len(self.operations) - 1:
            return
        self.operations[idx], self.operations[idx + 1] = (
            self.operations[idx + 1], self.operations[idx])
        self._refresh_ops_list()
        self.ops_list.setCurrentRow(idx + 1)
        self._update_preview()

    def _on_clear_operations(self):
        self.operations.clear()
        self._refresh_ops_list()
        self._update_preview()

    # ------------------------------------------------------------------
    # Rename / restore / undo
    # ------------------------------------------------------------------

    def _push_undo(self, record):
        self.undo_stack.append(record)
        if len(self.undo_stack) > MAX_UNDO:
            self.undo_stack.pop(0)
        self.undo_btn.setText(f"Undo Last ({len(self.undo_stack)})")

    def _fresh_tl_rows(self):
        """Re-read the timeline for a write. Returns (shown, all) or None."""
        expected_uid = self.tl_uid
        self._refresh_tl(update=False)
        if self.tl_timeline is None:
            self._status("No timeline is open.", error=True)
            return None
        if not self.tl_supported:
            self._status("This Resolve build cannot rename timeline clips (needs 20.2+).",
                         error=True)
            return None
        if expected_uid and self.tl_uid != expected_uid:
            self._update_preview()
            self._status("The current timeline changed since the preview was built. "
                         "Check the preview and press Rename again.", error=True)
            return None
        shown = self._tl_shown_from(self.tl_scope_items)
        if self.tl_scope.currentIndex() == 1 and not shown:
            self._update_preview()
            self._status(self.tl_scope_message or "Nothing selected on the timeline.",
                         error=True)
            return None
        return shown, self.tl_all

    def _on_rename(self):
        if not self.operations:
            self._status("Add an operation first.", error=True)
            return
        tab = self._tab()
        selected_uids = set(self._selected_uids(tab))

        if tab == TAB_TIMELINE:
            fresh = self._fresh_tl_rows()
            if fresh is None:
                return
            shown, all_items = fresh
            new_names = self._compute(tab, shown, all_items)
        else:
            self._refresh_mp(update=False)
            shown = self._mp_shown_from(self.mp_items)
            new_names = self._compute(tab, shown)

        pairs = list(zip(shown, new_names))
        if selected_uids:
            pairs = [(i, n) for i, n in pairs if i["uid"] in selected_uids]
        if not pairs:
            self._status("Nothing to rename.", error=True)
            return
        counts = Counter(new_names)
        if any(c > 1 for c in counts.values()):
            print("WARNING: name collisions detected. Proceeding anyway.")

        record, failed = execute_rename(
            pairs, kind=tab, timeline_uid=self.tl_uid if tab == TAB_TIMELINE else "",
            timeline_name=self.tl_name if tab == TAB_TIMELINE else "")
        if record:
            self._push_undo(record)
            msg = record.description
            if failed:
                msg += f" ({failed} failed, see the console)"
            self._status(msg, error=bool(failed))
        else:
            self._status("No names changed." + (f" {failed} failed." if failed else ""),
                         error=bool(failed))
        self._refresh_tl(update=False) if tab == TAB_TIMELINE else self._refresh_mp(update=False)
        self._update_preview()

    def _on_restore(self):
        selected_uids = set(self._selected_uids(TAB_TIMELINE))
        fresh = self._fresh_tl_rows()
        if fresh is None:
            return
        shown, _ = fresh
        if selected_uids:
            shown = [i for i in shown if i["uid"] in selected_uids]
        plan = restore_plan(shown)
        if not plan:
            self._status("Every clip in scope already has its Media Pool name.")
            return
        record, failed = execute_rename(plan, kind=TAB_TIMELINE, timeline_uid=self.tl_uid,
                                        timeline_name=self.tl_name, verb="Restored")
        if record:
            self._push_undo(record)
            self._status(record.description + (f" ({failed} failed)" if failed else ""),
                         error=bool(failed))
        self._refresh_tl(update=False)
        self._update_preview()

    def _on_undo(self):
        if not self.undo_stack:
            self._status("Nothing to undo.", error=True)
            return
        record = self.undo_stack.pop()
        reverted = execute_undo(record, self.project)
        self.undo_btn.setText(f"Undo Last ({len(self.undo_stack)})")
        self._status(f"Undid “{record.description}”: reverted {reverted} of "
                     f"{len(record.mappings)}.", error=reverted < len(record.mappings))
        self._refresh_mp(update=False)
        self._refresh_tl(update=False)
        self._update_preview()

    # ------------------------------------------------------------------
    # Preset events
    # ------------------------------------------------------------------

    def _on_save_preset(self):
        name = self.preset_name.text().strip()
        if not name:
            self._status("Enter a preset name first.", error=True)
            return
        filters = {cb_id: self.filter_checks[label].isChecked()
                   for cb_id, label, _ in CLIP_TYPE_FILTERS}
        filters["FilterOther"] = self.filter_checks["Other"].isChecked()
        extra = {
            "mp_attr": self.mp_attr.state(),
            "tl_attr": self.tl_attr.state(),
            "mp_sort": self.mp_sort.currentText(),
            "tl_scope": SCOPE_SELECTED if self.tl_scope.currentIndex() == 1 else SCOPE_WHOLE,
        }
        preset = RenamePreset(
            name=name, operations=[serialize_op(op) for op in self.operations],
            filters=filters, include_subfolders=self.include_subfolders.isChecked(),
            tab=self._tab(), extra=extra)
        for i, p in enumerate(self.presets):
            if p.name == name:
                self.presets[i] = preset
                break
        else:
            self.presets.append(preset)
        save_presets(self.presets)
        self._refresh_preset_combo()
        self.preset_combo.setCurrentIndex([p.name for p in self.presets].index(name))
        self._update_default_button()
        self._status(f"Saved preset: {name}")

    def _on_load_preset(self):
        idx = self.preset_combo.currentIndex()
        if 0 <= idx < len(self.presets):
            self._apply_preset(self.presets[idx])
            self._status(f"Loaded preset: {self.presets[idx].name}")

    def _on_delete_preset(self):
        idx = self.preset_combo.currentIndex()
        if idx < 0 or idx >= len(self.presets):
            return
        name = self.presets[idx].name
        if name == self.default_preset:
            self.default_preset = ""
            save_default_preset_name("")
        self.presets.pop(idx)
        save_presets(self.presets)
        self._refresh_preset_combo()
        self._update_default_button()
        self._status(f"Deleted preset: {name}")

    def _on_default_preset(self):
        idx = self.preset_combo.currentIndex()
        if idx < 0 or idx >= len(self.presets):
            return
        name = self.presets[idx].name
        if name == self.default_preset:
            self.default_preset = ""
            save_default_preset_name("")
            self._status("Default preset cleared.")
        else:
            self.default_preset = name
            save_default_preset_name(name)
            self._status(f"Default preset set to: {name}")
        self._update_default_button()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_instance_server = None  # keep reference alive for single-instance lock


def main():
    global _instance_server

    project = resolve.GetProjectManager().GetCurrentProject()  # noqa: F821
    if not project:
        print("Error: No project is open.")
        return

    # --- Single-instance guard via named local socket -------------------
    try:
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        _has_network = True
    except ImportError:
        _has_network = False

    _SOCKET_NAME = "ResolveConformTools.BatchRename"

    if _has_network:
        sock = QLocalSocket()
        sock.connectToServer(_SOCKET_NAME)
        if sock.waitForConnected(200):
            print("Batch Rename is already running.")
            sock.disconnectFromServer()
            return
        sock.abort()
        _instance_server = QLocalServer()
        QLocalServer.removeServer(_SOCKET_NAME)
        _instance_server.listen(_SOCKET_NAME)

    print(f"=== Batch Rename v{SCRIPT_VERSION} loaded ===")

    app = QApplication.instance()
    standalone = app is None
    if standalone:
        app = QApplication(sys.argv)

    dlg = BatchRenameDialog(project)
    dlg.show()

    if standalone:
        app.exec()
    else:
        # Resolve already has a Qt event loop — run as modal dialog
        dlg.exec()

    if _instance_server is not None:
        _instance_server.close()
        _instance_server = None

    print("=== Batch Rename closed ===")


main()
