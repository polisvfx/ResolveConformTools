#
# Render Scaled Plates - part of ResolveConformTools
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
Render Scaled Plates.py
───────────────────────
Version: 1.0

Queues every active plate of the current timeline for rendering at a reduced
resolution, in Individual Clips mode.

The rule is one of:
  * Height  - every plate scaled to a fixed height, width follows the aspect
  * Width   - every plate scaled to a fixed width, height follows the aspect
  * Factor  - every plate scaled by a factor (0.5 = half resolution)

Plates of different sizes come out at different resolutions under the same
rule (height 1080 makes 4096x2160 into 2048x1080 but 3840x2160 into
1920x1080), so the plates are grouped by the resolution they come out at and
each group gets its own "render timeline", named <base>_<W>x<H>, at that
resolution. Each one is queued as a render job with the chosen preset; only
the output resolution (and the target folder) is overridden.

Plates are never scaled up unless asked: one already smaller than the target
keeps its native size and lands in its own native-resolution group.

Scope: the clips selected on the timeline (Resolve 21.0.4+, every enabled
video clip when nothing is selected), every enabled video clip, or every
enabled video clip touching the timeline's In-Out range. Each plate covers the source range the
edit uses, with retimes flattened to 100% (a reversed or constant-speed
retime covers every frame it plays). Uses of the same source that overlap are
merged into one plate.

Render timelines carry a record of how they were made, so the script finds
them again later: Re-submit mode re-queues them without rebuilding.

Run from:  Workspace ▸ Scripts
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from datetime import datetime, timezone

TOOL_NAME = "Render Scaled Plates"
# Keep in step with the "Version:" line in the docstring above; this is what
# gets recorded into each render timeline's manifest.
TOOL_VERSION = "1.0"

MANIFEST_KEY = "RCT_RenderScaledPlates"
MANIFEST_MARKER_NAME = "Render Scaled Plates"
MANIFEST_MARKER_PREFIX = "RCT_RSP:"
MANIFEST_MARKER_COLOR = "Cream"
RENDER_BIN_NAME = "Render Timelines"

MODES = ("height", "width", "factor")
# What the value field starts at for each rule, until the user has used it.
DEFAULT_VALUES = {"height": "2160", "width": "4096", "factor": "0.5"}
# The factor slider runs 1.0 down to 0.0 in steps of SLIDER_STEPS; its lowest
# position is one step, because a factor of 0 is not a size.
SLIDER_STEPS = 100
SCOPES = ("selection", "all", "inout")
SCOPE_LABELS = {
    "selection": "Selected clips (all enabled clips if none)",
    "all": "All enabled clips",
    "inout": "Enabled clips between In and Out",
}
MODE_LABELS = {"height": "Scale to height", "width": "Scale to width",
               "factor": "Scale by factor"}
EXISTING_POLICIES = ("reuse", "replace", "new")
EXISTING_LABELS = {
    "reuse": "Re-use it (queue without rebuilding)",
    "replace": "Replace it (delete and rebuild)",
    "new": "Build a new one alongside (_v2, _v3 ...)",
}
CURRENT_SETTINGS = "<current render settings>"

# AppendToTimeline can place a clip a frame off from the requested source range;
# see Generate All Clips Timeline PRO for the measurements behind this.
SLIP_RETRY_LIMIT = 2

PREFS_FILE = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "ResolveConformTools", "render_scaled_plates.json")


# ---------------------------------------------------------------------------
# Pure helpers (no Resolve objects; tests/test_part_j.py extracts these)
# ---------------------------------------------------------------------------

def parse_resolution(text):
    """'3840x2160' -> (3840, 2160), or None.

    The combined "Resolution" clip property is the one to read: on 21.0.4.5
    "Resolution X" and "Resolution Y" return None for ordinary video clips.
    """
    if not text:
        return None
    m = re.match(r"^\s*(\d+)\s*[xX×]\s*(\d+)", str(text))
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return None
    return w, h


def round_even(value):
    """Nearest even integer (halves round up), at least 2. Most codecs refuse
    odd dimensions. Not round(): it rounds halves to even, so 429 became 428."""
    n = int(float(value) / 2.0 + 0.5) * 2
    return max(2, n)


def compute_target(src_w, src_h, mode, value, allow_upscale=False):
    """Target (w, h, note) for a plate of src_w x src_h under the rule.

    Both output dimensions are even; the one the rule does not fix follows
    the source aspect. note is "" normally, "native" when the plate is
    smaller than the target and upscaling is off (it keeps its own size), or
    "same" when the rule asks for the size the plate already has.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    value = float(value)
    if value <= 0:
        raise ValueError("the scale value must be greater than zero")
    if mode == "height":
        scale = value / src_h
    elif mode == "width":
        scale = value / src_w
    else:
        scale = value

    if scale > 1.0 and not allow_upscale:
        return src_w, src_h, "native"

    if mode == "height":
        w, h = round_even(src_w * scale), round_even(value)
    elif mode == "width":
        w, h = round_even(value), round_even(src_h * scale)
    else:
        w, h = round_even(src_w * scale), round_even(src_h * scale)

    if (w, h) == (src_w, src_h):
        return w, h, "same"
    return w, h, ""


def merge_plate_ranges(plates):
    """Merge uses of the same source whose ranges overlap or touch.

    plates: dicts with "key", "start", "end" (inclusive) and "order". Returns
    new dicts (the first plate's fields win) with the union range, the
    lowest order, and "uses" counting how many edit uses went into it.
    Input dicts are not modified.
    """
    by_key = {}
    for plate in plates:
        by_key.setdefault(plate["key"], []).append(plate)
    merged = []
    for group in by_key.values():
        group = sorted(group, key=lambda p: (p["start"], p["end"]))
        current = None
        for plate in group:
            if current is not None and plate["start"] <= current["end"] + 1:
                current["end"] = max(current["end"], plate["end"])
                current["order"] = min(current["order"], plate["order"])
                current["uses"] += plate.get("uses", 1)
                continue
            if current is not None:
                merged.append(current)
            current = dict(plate)
            current["uses"] = plate.get("uses", 1)
        if current is not None:
            merged.append(current)
    merged.sort(key=lambda p: p["order"])
    return merged


def group_by_resolution(plates):
    """{(w, h): [plates in order]} keyed by each plate's "target"."""
    groups = {}
    for plate in sorted(plates, key=lambda p: p["order"]):
        groups.setdefault(tuple(plate["target"]), []).append(plate)
    return groups


def resolution_label(w, h):
    return f"{int(w)}x{int(h)}"


def render_timeline_name(base, w, h):
    """'Shots' + 1920x1080 -> 'Shots_1920x1080'."""
    base = (base or "").strip() or "Plates"
    return f"{base}_{resolution_label(w, h)}"


def unique_name(name, taken):
    """name, or name_v2, name_v3 ... whichever is not in `taken`."""
    if name not in taken:
        return name
    n = 2
    while f"{name}_v{n}" in taken:
        n += 1
    return f"{name}_v{n}"


def target_dir_for(base_dir, w, h, per_resolution_subfolders):
    """Where a group's files go. '' means: leave the preset's folder alone."""
    base_dir = (base_dir or "").strip()
    if not base_dir:
        return ""
    if per_resolution_subfolders:
        return os.path.join(base_dir, resolution_label(w, h))
    return base_dir


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(*, base_name, width, height, mode, value, allow_upscale,
                   source_name, source_uid, clip_count, now_iso):
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "schema": 1,
        "base_name": base_name,
        "resolution": [int(width), int(height)],
        "rule": {"mode": mode, "value": value, "allow_upscale": bool(allow_upscale)},
        "source_timeline": {"name": source_name, "uid": source_uid},
        "clip_count": int(clip_count),
        "created_utc": now_iso,
        "last_queued_utc": None,
        "last_preset": None,
        "last_target_dir": None,
    }


def encode_manifest(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def decode_manifest(text):
    """Manifest dict, or None for anything that is not one of ours."""
    if not text or not isinstance(text, str):
        return None
    if text.startswith(MANIFEST_MARKER_PREFIX):
        text = text[len(MANIFEST_MARKER_PREFIX):]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("tool") != TOOL_NAME:
        return None
    res = data.get("resolution")
    if (not isinstance(res, list) or len(res) != 2
            or not all(isinstance(v, int) and v > 0 for v in res)):
        return None
    return data


def normalise_source_range(raw_start, raw_end, duration):
    """Inclusive (start, end) of the source frames a timeline item uses.

    Reversed clips report start > end, so the range is normalised. A clip
    AppendToTimeline placed can echo back the EXCLUSIVE end instead of the
    inclusive one (21.0.4.5 always, 21.2.0.6 sometimes); an exact one-frame
    overhang over the duration is that form and is taken back off. Retimed
    clips have a duration unrelated to their span and are left alone, as are
    frame holds (span 0 or 1).
    """
    start, end = min(raw_start, raw_end), max(raw_start, raw_end)
    reversed_clip = raw_start > raw_end
    if (not reversed_clip and end - start > 1 and duration is not None
            and end - start + 1 - int(duration) == 1):
        end -= 1
    return start, end


def parse_scale_value(mode, text):
    """The rule's value from the GUI text: an int for height/width, a float
    for factor (which also accepts '50%'). Raises ValueError with a message
    fit for the user."""
    text = (text or "").strip().replace(",", ".")
    if not text:
        raise ValueError("enter a value for the scale rule")
    if mode == "factor":
        pct = text.endswith("%")
        num = float(text.rstrip("%").strip())
        if pct:
            num /= 100.0
        if num <= 0:
            raise ValueError("the factor must be greater than zero")
        return num
    num = float(text)
    if num < 2 or num != int(num):
        raise ValueError(f"the {mode} must be a whole number of pixels (2 or more)")
    return int(num)


def initial_values(saved):
    """The value field's text for each rule: what the user last used for that
    rule (prefs "values"), else DEFAULT_VALUES."""
    values = dict(DEFAULT_VALUES)
    if isinstance(saved, dict):
        for mode in MODES:
            text = saved.get(mode)
            if isinstance(text, str) and text.strip():
                values[mode] = text.strip()
    return values


def factor_to_slider(factor):
    """Slider position for a factor, clamped to 1..SLIDER_STEPS."""
    pos = int(float(factor) * SLIDER_STEPS + 0.5)
    return max(1, min(SLIDER_STEPS, pos))


def slider_to_factor_text(pos):
    """Value-field text for a slider position: 50 -> '0.5', 100 -> '1'."""
    pos = max(1, min(SLIDER_STEPS, int(pos)))
    return f"{pos / SLIDER_STEPS:g}"


def plan_lines(groups, base_name):
    """Human-readable plan, one block per render timeline."""
    lines = []
    for (w, h), plates in sorted(groups.items(), key=lambda kv: (-kv[0][1], -kv[0][0])):
        lines.append(f"{render_timeline_name(base_name, w, h)}  "
                     f"({len(plates)} plate{'s' if len(plates) != 1 else ''})")
        for p in plates:
            note = ""
            if p.get("note") == "native":
                note = "  [smaller than target, kept native]"
            elif p.get("note") == "same":
                note = "  [already this size]"
            uses = f"  [{p['uses']} uses merged]" if p.get("uses", 1) > 1 else ""
            lines.append(f"    {p['name']}  {p['src_res'][0]}x{p['src_res'][1]} "
                         f"-> {w}x{h}  src {p['start']}-{p['end']}{note}{uses}")
    return lines


def mark_range(marks, timeline_start):
    """Absolute (in, out) record frames of a timeline's In/Out marks, out
    INCLUSIVE, from Timeline.GetMarkInOut(). A side that is not set is None
    (open). None when neither mark is set.

    Measured on 21.2.0.6: the marks are offsets from GetStartFrame(), with Out
    inclusive. Marks of 25-45 on a timeline starting at 86400 made a render
    job of 86425-86445. The video marks are used; audio ones fill in only if
    the video ones are missing.
    """
    if not isinstance(marks, dict):
        return None
    side = marks.get("video") or marks.get("audio") or {}
    if not isinstance(side, dict):
        return None
    start = int(timeline_start or 0)
    m_in, m_out = side.get("in"), side.get("out")
    if m_in is None and m_out is None:
        return None
    return (None if m_in is None else start + int(m_in),
            None if m_out is None else start + int(m_out))


def overlaps_range(rec_start, rec_end, in_out):
    """True if a clip at record [rec_start, rec_end) touches the inclusive
    In-Out range. A partly covered clip counts and comes in whole, because a
    plate is the whole shot."""
    if rec_start is None or rec_end is None:
        return False
    lo, hi = in_out
    if lo is not None and rec_end <= lo:
        return False
    if hi is not None and rec_start > hi:
        return False
    return True


def range_label(in_out):
    lo, hi = in_out
    return f"{'start' if lo is None else lo}-{'end' if hi is None else hi}"


# ---------------------------------------------------------------------------
# Preferences (per user, outside the project)
# ---------------------------------------------------------------------------

def load_prefs():
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(prefs):
    try:
        os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
        with open(PREFS_FILE, "w", encoding="utf-8") as fh:
            json.dump(prefs, fh, indent=2)
    except Exception as exc:
        print(f"  NOTE: could not save preferences ({exc})")


# ---------------------------------------------------------------------------
# Resolve helpers
# ---------------------------------------------------------------------------

def _call(obj, name, *args, default=None):
    """obj.name(*args), or default if the method is missing or raises.

    The Resolve bridge answers unknown attributes with None instead of
    raising, so a missing API looks like a call that returned nothing.
    """
    fn = getattr(obj, name, None)
    if not callable(fn):
        return default
    try:
        return fn(*args)
    except Exception:
        return default


def item_uid(item):
    return _call(item, "GetUniqueId") or None


def collect_plates(timeline, *, mode, value, allow_upscale, scope="selection"):
    """Read the plates off `timeline`.

    scope is one of SCOPES: "selection" (the selected clips, or every enabled
    clip when nothing is selected), "all" (every enabled clip) or "inout"
    (every enabled clip touching the In-Out range; nothing if no marks).
    Returns (plates, scope_label, skipped) where skipped lists
    (name, reason) for clips that could not become a plate.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    in_out = None
    if scope == "inout":
        in_out = mark_range(_call(timeline, "GetMarkInOut", default=None),
                            _call(timeline, "GetStartFrame", default=0))
        if in_out is None:
            return [], "no In or Out mark is set on the timeline", []
    selected = []
    if scope == "selection":
        selected = _call(timeline, "GetSelectedClips", default=[]) or []
    selected_uids = set()
    for item in selected:
        track = _call(item, "GetTrackTypeAndIndex", default=None)
        if track and str(track[0]).lower() != "video":
            continue
        uid = item_uid(item)
        if uid:
            selected_uids.add(uid)
    use_sel = bool(selected_uids)

    plates, skipped = [], []
    enabled_total = 0  # what "All enabled clips" would consider, for the label
    track_count = _call(timeline, "GetTrackCount", "video", default=0) or 0
    for track_index in range(1, int(track_count) + 1):
        track_on = _call(timeline, "GetIsTrackEnabled", "video", track_index, default=True)
        items = _call(timeline, "GetItemListInTrack", "video", track_index, default=[]) or []
        for item in items:
            if item is None:
                continue
            uid = item_uid(item)
            if (track_on is not False
                    and _call(item, "GetClipEnabled", default=True) is not False):
                enabled_total += 1
            if use_sel:
                if uid not in selected_uids:
                    continue
            else:
                if track_on is False:
                    continue
                if _call(item, "GetClipEnabled", default=True) is False:
                    continue
                if in_out is not None and not overlaps_range(
                        _call(item, "GetStart"), _call(item, "GetEnd"), in_out):
                    continue
            name = _call(item, "GetName", default="?") or "?"
            mpi = _call(item, "GetMediaPoolItem")
            if mpi is None:
                skipped.append((name, "no media (generator, title or transition)"))
                continue
            src_res = parse_resolution(_call(mpi, "GetClipProperty", "Resolution"))
            if src_res is None:
                skipped.append((name, "no readable resolution (compound or Fusion clip?)"))
                continue
            raw_start = _call(item, "GetSourceStartFrame")
            raw_end = _call(item, "GetSourceEndFrame")
            if raw_start is None or raw_end is None:
                skipped.append((name, "no source range"))
                continue
            start, end = normalise_source_range(
                int(raw_start), int(raw_end), _call(item, "GetDuration"))
            media_id = _call(mpi, "GetMediaId") or _call(mpi, "GetUniqueId") or name
            w, h, note = compute_target(src_res[0], src_res[1], mode, value, allow_upscale)
            par = _call(mpi, "GetClipProperty", "PAR") or ""
            plates.append({
                "key": media_id,
                "name": _call(mpi, "GetName", default=name) or name,
                "mpi": mpi,
                "item": item,
                "start": start,
                "end": end,
                "order": (int(_call(item, "GetStart", default=0) or 0), track_index),
                "src_res": src_res,
                "par": par,
                "target": (w, h),
                "note": note,
                "uses": 1,
            })

    if use_sel:
        label = (f"only the {len(plates)} SELECTED clip(s), of {enabled_total} "
                 f"enabled; deselect, or choose All enabled clips, to take them all")
    elif in_out is not None:
        label = (f"{len(plates)} enabled video clip(s) between In and Out "
                 f"({range_label(in_out)})")
    else:
        label = f"all {len(plates)} enabled video clip(s)"
    return plates, label, skipped


def get_or_create_bin(media_pool, name):
    root = media_pool.GetRootFolder()
    for sub in (_call(root, "GetSubFolderList", default=[]) or []):
        if _call(sub, "GetName") == name:
            return sub
    return media_pool.AddSubFolder(root, name)


def timeline_markers(timeline):
    markers = _call(timeline, "GetMarkers", default={}) or {}
    out = {}
    for frame, data in markers.items():
        try:
            out[int(float(frame))] = data or {}
        except (TypeError, ValueError):
            continue
    return out


def read_manifest(timeline):
    """The manifest on `timeline`, from its Media Pool item's third-party
    metadata first, then from the Cream marker. None if it is not ours."""
    mpi = _call(timeline, "GetMediaPoolItem")
    if mpi is not None:
        manifest = decode_manifest(_call(mpi, "GetThirdPartyMetadata", MANIFEST_KEY))
        if manifest is not None:
            return manifest
    for data in timeline_markers(timeline).values():
        custom = data.get("customData") or ""
        if custom.startswith(MANIFEST_MARKER_PREFIX):
            manifest = decode_manifest(custom)
            if manifest is not None:
                return manifest
    return None


def write_manifest(timeline, manifest):
    """Write to both stores; True if at least one took."""
    encoded = encode_manifest(manifest)
    ok_meta = False
    mpi = _call(timeline, "GetMediaPoolItem")
    if mpi is not None:
        ok_meta = bool(_call(mpi, "SetThirdPartyMetadata", MANIFEST_KEY, encoded))

    for frame, data in timeline_markers(timeline).items():
        if (data.get("customData") or "").startswith(MANIFEST_MARKER_PREFIX):
            _call(timeline, "DeleteMarkerAtFrame", frame)
    w, h = manifest["resolution"]
    src = (manifest.get("source_timeline") or {}).get("name") or "?"
    note = (f"Built by {TOOL_NAME} {manifest.get('tool_version')}\n"
            f"{w}x{h} from '{src}', {manifest.get('clip_count')} plate(s)")
    if manifest.get("last_queued_utc"):
        note += f"\nLast queued {manifest['last_queued_utc']}"
    ok_marker = bool(_call(timeline, "AddMarker", 0, MANIFEST_MARKER_COLOR,
                           MANIFEST_MARKER_NAME, note, 1,
                           MANIFEST_MARKER_PREFIX + encoded))
    if not ok_meta and not ok_marker:
        print(f"  WARNING: could not record '{timeline.GetName()}' as a render "
              f"timeline; Re-submit will not find it.")
    return ok_meta or ok_marker


def list_timelines(project):
    count = _call(project, "GetTimelineCount", default=0) or 0
    out = []
    for i in range(1, int(count) + 1):
        tl = _call(project, "GetTimelineByIndex", i)
        if tl is not None:
            out.append(tl)
    return out


def find_render_timelines(project):
    """[(timeline, manifest)] for every render timeline in the project."""
    found = []
    for tl in list_timelines(project):
        manifest = read_manifest(tl)
        if manifest is not None:
            found.append((tl, manifest))
    found.sort(key=lambda tm: (tm[1].get("base_name") or "", -tm[1]["resolution"][1],
                               tl_name(tm[0])))
    return found


def tl_name(timeline):
    return _call(timeline, "GetName", default="?") or "?"


def placed_source_range(item):
    """(start, end_EXCLUSIVE) of a clip AppendToTimeline just placed."""
    start = _call(item, "GetSourceStartFrame")
    duration = _call(item, "GetDuration")
    if start is None or duration is None:
        return None, None
    return int(start), int(start) + int(duration)


def append_plate(media_pool, timeline, plate):
    """Append one plate (video only), correcting a +-1 frame source slip.

    Returns (item, status) with status "exact", "corrected", "unfixed" or
    "error". endFrame is exclusive, hence end + 1.
    """
    target_start, target_end = plate["start"], plate["end"] + 1
    request = {"mediaPoolItem": plate["mpi"], "startFrame": target_start,
               "endFrame": target_end, "mediaType": 1}
    try:
        placed = media_pool.AppendToTimeline([request])
    except Exception:
        placed = None
    if not placed:
        return None, "error"
    for attempt in range(SLIP_RETRY_LIMIT + 1):
        if len(placed) != 1:
            return placed[0], "unfixed"
        item = placed[0]
        a_start, a_end = placed_source_range(item)
        if a_start is None:
            return item, "unfixed"
        ds, de = a_start - target_start, a_end - target_end
        if ds == 0 and de == 0:
            return item, ("corrected" if attempt else "exact")
        if attempt == SLIP_RETRY_LIMIT:
            print(f"    WARN: '{plate['name']}' placed at {a_start}-{a_end - 1}, "
                  f"wanted {target_start}-{target_end - 1}")
            return item, "unfixed"
        if not _call(timeline, "DeleteClips", [item], False, default=False):
            return item, "unfixed"
        request["startFrame"] -= ds
        request["endFrame"] -= de
        try:
            placed = media_pool.AppendToTimeline([request])
        except Exception:
            placed = None
        if not placed:
            return None, "error"
    return None, "error"


def fps_text(value):
    """25.0 -> '25', '23.976' -> '23.976'; '' when unreadable. SetSetting
    wants a string, and GetSetting answers the frame rate as a float."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def configure_timeline(timeline, w, h, fps):
    settings = [("useCustomSettings", "1")]
    # A new timeline already has the project's rate, and Resolve refuses to
    # "change" it to the same value, so only set it when it differs.
    want_fps = fps_text(fps)
    if want_fps and want_fps != fps_text(_call(timeline, "GetSetting", "timelineFrameRate")):
        settings.append(("timelineFrameRate", want_fps))
    settings += [
        ("timelineResolutionWidth", str(w)),
        ("timelineResolutionHeight", str(h)),
        ("timelineOutputResMatchTimelineRes", "1"),
        ("timelineOutputResolutionWidth", str(w)),
        ("timelineOutputResolutionHeight", str(h)),
        ("timelineInputResMismatchBehavior", "scaleToFit"),
    ]
    failed = []
    for key, val in settings:
        if val in (None, ""):
            continue
        if not _call(timeline, "SetSetting", key, val, default=False):
            failed.append(key)
    got = (str(_call(timeline, "GetSetting", "timelineResolutionWidth")),
           str(_call(timeline, "GetSetting", "timelineResolutionHeight")))
    if got != (str(w), str(h)):
        print(f"    WARNING: timeline resolution reads back {got[0]}x{got[1]}, "
              f"wanted {w}x{h}")
    if failed:
        print(f"    NOTE: Resolve refused: {', '.join(failed)}")


def build_render_timeline(project, media_pool, name, w, h, plates, *, fps,
                          copy_grades, manifest):
    """Create one render timeline and fill it. Returns the timeline or None."""
    bin_folder = get_or_create_bin(media_pool, RENDER_BIN_NAME)
    if bin_folder is not None:
        media_pool.SetCurrentFolder(bin_folder)
    timeline = media_pool.CreateEmptyTimeline(name)
    if timeline is None:
        print(f"  ERROR: could not create timeline '{name}'")
        return None
    project.SetCurrentTimeline(timeline)
    configure_timeline(timeline, w, h, fps)

    counts = {}
    for plate in plates:
        item, status = append_plate(media_pool, timeline, plate)
        counts[status] = counts.get(status, 0) + 1
        if item is None:
            print(f"    ERROR: could not place '{plate['name']}'")
            continue
        if copy_grades:
            if not _call(plate["item"], "CopyGrades", [item], default=False):
                print(f"    NOTE: grade not copied for '{plate['name']}'")
    print(f"    placed: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    write_manifest(timeline, manifest)
    return timeline


def queue_timeline(project, timeline, manifest, *, preset, base_dir, subfolders,
                   export_audio):
    """Add one Individual Clips render job for `timeline`. Returns job id or None."""
    w, h = manifest["resolution"]
    if not project.SetCurrentTimeline(timeline):
        print(f"  ERROR: could not open '{tl_name(timeline)}'")
        return None
    if preset and preset != CURRENT_SETTINGS:
        if not project.LoadRenderPreset(preset):
            print(f"  ERROR: could not load render preset '{preset}'")
            return None
    # Set the mode after loading: a preset can carry its own mode.
    if not project.SetCurrentRenderMode(0):
        print("  ERROR: could not switch the Deliver page to Individual Clips")
        return None
    settings = {"SelectAllFrames": True, "FormatWidth": int(w), "FormatHeight": int(h)}
    target = target_dir_for(base_dir, w, h, subfolders)
    if target:
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            print(f"  WARNING: could not create {target} ({exc})")
        settings["TargetDir"] = target
    if not export_audio:
        settings["ExportAudio"] = False
    if not project.SetRenderSettings(settings):
        print(f"  ERROR: Resolve refused the render settings for '{tl_name(timeline)}'")
        return None
    job_id = project.AddRenderJob()
    if not job_id:
        print(f"  ERROR: Resolve did not add a job for '{tl_name(timeline)}'")
        return None
    for job in (_call(project, "GetRenderJobList", default=[]) or []):
        if job.get("JobId") == job_id:
            got = (job.get("FormatWidth"), job.get("FormatHeight"))
            if got != (int(w), int(h)):
                print(f"  WARNING: job for '{tl_name(timeline)}' reads back "
                      f"{got[0]}x{got[1]}, wanted {w}x{h}")
            if job.get("RenderMode") and "individual" not in str(job["RenderMode"]).lower():
                print(f"  WARNING: job mode reads back '{job['RenderMode']}'")
            break
    manifest["last_queued_utc"] = utc_now_iso()
    manifest["last_preset"] = preset
    manifest["last_target_dir"] = target or None
    write_manifest(timeline, manifest)
    print(f"  queued '{tl_name(timeline)}' at {w}x{h}"
          + (f" -> {target}" if target else " (preset's folder)"))
    return job_id


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

def run_create(project, opts):
    source = project.GetCurrentTimeline()
    if source is None:
        print("ERROR: no timeline is open.")
        return []
    media_pool = project.GetMediaPool()
    plates, scope, skipped = collect_plates(
        source, mode=opts["mode"], value=opts["value"],
        allow_upscale=opts["allow_upscale"],
        scope=opts.get("scope", "selection"))
    print(f"Source: '{tl_name(source)}', {scope}")
    for name, why in skipped:
        print(f"  skipped '{name}': {why}")
    if opts["merge"]:
        plates = merge_plate_ranges(plates)
    if not plates:
        print("Nothing to do: no usable plates.")
        return []
    groups = group_by_resolution(plates)
    base = opts["base_name"]
    for line in plan_lines(groups, base):
        print("  " + line)
    nonsquare = sorted({p["par"] for p in plates
                        if p["par"] and str(p["par"]).lower() not in ("square", "1", "1.0")})
    if nonsquare:
        print(f"  NOTE: non-square pixel aspect ({', '.join(nonsquare)}): plates are "
              f"fitted to a square-pixel timeline, so check one render first.")
    if opts["dry_run"]:
        print("Dry run: nothing written.")
        return []

    fps = _call(source, "GetSetting", "timelineFrameRate") or ""
    existing = {}
    for tl, manifest in find_render_timelines(project):
        existing.setdefault(tl_name(tl), []).append((tl, manifest))
    all_names = {tl_name(tl) for tl in list_timelines(project)}

    to_queue = []
    for (w, h), group in sorted(groups.items(), key=lambda kv: (-kv[0][1], -kv[0][0])):
        name = render_timeline_name(base, w, h)
        manifest = build_manifest(
            base_name=base, width=w, height=h, mode=opts["mode"], value=opts["value"],
            allow_upscale=opts["allow_upscale"], source_name=tl_name(source),
            source_uid=item_uid(source), clip_count=len(group), now_iso=utc_now_iso())
        if name in all_names:
            prior = existing.get(name)
            policy = opts["existing"]
            if prior and policy == "reuse":
                print(f"  '{name}' exists: re-using it")
                to_queue.append(prior[0])
                continue
            if prior and policy == "replace":
                print(f"  '{name}' exists: replacing it")
                if not media_pool.DeleteTimelines([prior[0][0]]):
                    print(f"  ERROR: could not delete '{name}'; building a new one instead")
                    name = unique_name(name, all_names)
            else:
                # Same name but not ours, or policy "new": never touch it.
                name = unique_name(name, all_names)
        all_names.add(name)
        print(f"  building '{name}' ({w}x{h}, {len(group)} plate(s))")
        tl = build_render_timeline(project, media_pool, name, w, h, group, fps=fps,
                                   copy_grades=opts["copy_grades"], manifest=manifest)
        if tl is not None:
            to_queue.append((tl, manifest))

    project.SetCurrentTimeline(source)
    return to_queue


def run_queue(project, entries, opts):
    if not entries:
        return []
    restore = project.GetCurrentTimeline()
    jobs = []
    print(f"Queuing {len(entries)} render timeline(s), preset "
          f"'{opts['preset']}'")
    for tl, manifest in entries:
        jid = queue_timeline(project, tl, manifest, preset=opts["preset"],
                             base_dir=opts["target_dir"], subfolders=opts["subfolders"],
                             export_audio=opts["export_audio"])
        if jid:
            jobs.append(jid)
    if restore is not None:
        project.SetCurrentTimeline(restore)
    print(f"{len(jobs)} of {len(entries)} job(s) added to the render queue.")
    if jobs and opts["start_render"]:
        # Every job is queued before anything renders; one call starts them all.
        opts["render_started"] = bool(project.StartRendering(jobs, False))
        if opts["render_started"]:
            print(f"Rendering started: {len(jobs)} job(s).")
        else:
            print("ERROR: Resolve did not start rendering.")
    return jobs


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def build_and_show_ui(project):
    """Show the dialog. Returns options (with "action") or None if cancelled."""
    ui = fu.UIManager  # noqa: F821
    disp = bmd.UIDispatcher(ui)  # noqa: F821
    prefs = load_prefs()

    source = project.GetCurrentTimeline()
    source_name = tl_name(source) if source is not None else ""
    presets = [CURRENT_SETTINGS] + list(_call(project, "GetRenderPresetList", default=[]) or [])
    found = find_render_timelines(project)

    win = disp.AddWindow({
        "ID": "RSPWin",
        "WindowTitle": f"{TOOL_NAME} {TOOL_VERSION}",
        "Geometry": [120, 120, 680, 720],
        "Spacing": 8,
    }, [
        ui.VGroup({"ID": "root"}, [
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Mode:", "Weight": 0, "MinimumSize": [110, 0]}),
                ui.ComboBox({"ID": "action"}),
            ]),
            ui.Label({"ID": "scopeLabel", "Text": "", "Weight": 0, "WordWrap": True}),
            # --- create ---
            ui.HGroup({"ID": "rowBase", "Weight": 0}, [
                ui.Label({"Text": "Base name:", "Weight": 0, "MinimumSize": [110, 0]}),
                ui.LineEdit({"ID": "baseName", "Text": f"{source_name}_plates"}),
            ]),
            ui.HGroup({"ID": "rowScope", "Weight": 0}, [
                ui.Label({"Text": "Clips:", "Weight": 0, "MinimumSize": [110, 0]}),
                ui.ComboBox({"ID": "scope",
                             "ToolTip": "Resolve keeps a selection around after "
                                        "edits, so check the count above.\n"
                                        "In and Out: every enabled clip touching "
                                        "the marked range, whole."}),
            ]),
            ui.HGroup({"ID": "rowRule", "Weight": 0}, [
                ui.Label({"Text": "Rule:", "Weight": 0, "MinimumSize": [110, 0]}),
                ui.ComboBox({"ID": "mode"}),
                ui.LineEdit({"ID": "value", "Text": "",
                             "MaximumSize": [120, 1000],
                             "ToolTip": "Pixels for height/width; 0.5 or 50% for a "
                                        "factor (above 1 only with upscaling)"}),
            ]),
            ui.HGroup({"ID": "rowSlider", "Weight": 0}, [
                ui.Label({"Text": "Factor:", "Weight": 0, "MinimumSize": [110, 0]}),
                ui.Label({"Text": "0", "Weight": 0}),
                ui.Slider({"ID": "factorSlider", "Orientation": "Horizontal",
                           "Minimum": 1, "Maximum": SLIDER_STEPS,
                           "Value": SLIDER_STEPS // 2, "PageStep": 10,
                           "ToolTip": "Drag to set the factor, or type it in the "
                                      "field above"}),
                ui.Label({"Text": "1.0", "Weight": 0}),
            ]),
            ui.HGroup({"ID": "rowChecks", "Weight": 0}, [
                ui.CheckBox({"ID": "allowUpscale", "Text": "Allow upscaling",
                             "Checked": bool(prefs.get("allow_upscale", False)),
                             "ToolTip": "Off: a plate already smaller than the target "
                                        "renders at its native size."}),
                ui.CheckBox({"ID": "copyGrades", "Text": "Copy grades",
                             "Checked": bool(prefs.get("copy_grades", False)),
                             "ToolTip": "Copy each clip's grade onto its plate. Off: "
                                        "plates are ungraded (colour management "
                                        "still applies)."}),
                ui.CheckBox({"ID": "merge", "Text": "Merge overlapping uses",
                             "Checked": bool(prefs.get("merge", True)),
                             "ToolTip": "One plate for uses of the same source whose "
                                        "ranges overlap or touch."}),
            ]),
            ui.HGroup({"ID": "rowExisting", "Weight": 0}, [
                ui.Label({"Text": "If it exists:", "Weight": 0, "MinimumSize": [110, 0]}),
                ui.ComboBox({"ID": "existing"}),
            ]),
            # --- list ---
            ui.Tree({"ID": "tree", "SortingEnabled": False,
                     "SelectionMode": "ExtendedSelection",
                     "AlternatingRowColors": True}),
            ui.HGroup({"ID": "rowTreeBtns", "Weight": 0}, [
                ui.Button({"ID": "refresh", "Text": "Refresh preview"}),
                ui.Button({"ID": "selectAll", "Text": "Select all"}),
            ]),
            # --- shared ---
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Render preset:", "Weight": 0, "MinimumSize": [110, 0]}),
                ui.ComboBox({"ID": "preset"}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Target folder:", "Weight": 0, "MinimumSize": [110, 0]}),
                ui.LineEdit({"ID": "targetDir", "Text": prefs.get("target_dir", ""),
                             "PlaceholderText": "empty = the preset's folder"}),
                ui.Button({"ID": "browse", "Text": "...", "Weight": 0,
                           "MaximumSize": [40, 1000]}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.CheckBox({"ID": "subfolders", "Text": "Subfolder per resolution",
                             "Checked": bool(prefs.get("subfolders", True))}),
                ui.CheckBox({"ID": "exportAudio", "Text": "Export audio",
                             "Checked": bool(prefs.get("export_audio", False))}),
                ui.CheckBox({"ID": "startRender", "Text": "Start rendering",
                             "Checked": bool(prefs.get("start_render", False))}),
                ui.CheckBox({"ID": "dryRun", "Text": "Dry run",
                             "Checked": False,
                             "ToolTip": "Print the plan to the console, change nothing."}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.HGap(0, 1),
                ui.Button({"ID": "cancel", "Text": "Cancel"}),
                ui.Button({"ID": "go", "Text": "Create && Queue", "Default": True}),
            ]),
        ]),
    ])
    items = win.GetItems()

    items["action"].AddItem("Create render timelines")
    items["action"].AddItem("Re-submit existing render timelines")
    if prefs.get("action") == "resubmit" and found:
        items["action"].CurrentIndex = 1
    for sc in SCOPES:
        items["scope"].AddItem(SCOPE_LABELS[sc])
    if prefs.get("scope") in SCOPES:
        items["scope"].CurrentIndex = SCOPES.index(prefs["scope"])
    for m in MODES:
        items["mode"].AddItem(MODE_LABELS[m])
    if prefs.get("mode") in MODES:
        items["mode"].CurrentIndex = MODES.index(prefs["mode"])
    values = initial_values(prefs.get("values"))
    rule = {"mode": MODES[items["mode"].CurrentIndex], "syncing": False}
    for p in EXISTING_POLICIES:
        items["existing"].AddItem(EXISTING_LABELS[p])
    if prefs.get("existing") in EXISTING_POLICIES:
        items["existing"].CurrentIndex = EXISTING_POLICIES.index(prefs["existing"])
    for p in presets:
        items["preset"].AddItem(p)
    if prefs.get("preset") in presets:
        items["preset"].CurrentIndex = presets.index(prefs["preset"])

    tree = items["tree"]
    state = {"result": None, "rows": {}}

    def is_resubmit():
        return items["action"].CurrentIndex == 1

    def set_header(labels, widths):
        tree.Clear()
        hdr = tree.NewItem()
        for i, text in enumerate(labels):
            hdr.Text[i] = text
        tree.SetHeaderItem(hdr)
        tree.ColumnCount = len(labels)
        for i, wdt in enumerate(widths):
            tree.ColumnWidth[i] = wdt

    def add_row(texts):
        row = tree.NewItem()
        for i, text in enumerate(texts):
            row.Text[i] = str(text)
        tree.AddTopLevelItem(row)
        return row

    def read_rule():
        mode = MODES[items["mode"].CurrentIndex]
        return mode, parse_scale_value(mode, items["value"].Text)

    def sync_slider_from_text():
        try:
            factor = parse_scale_value("factor", items["value"].Text)
        except ValueError:
            return
        rule["syncing"] = True
        items["factorSlider"].Value = factor_to_slider(factor)
        rule["syncing"] = False

    def relayout():
        """Lay the window out again after showing or hiding rows.

        UIManager does not do this itself: a row that was hidden comes back
        with its widgets at 0,0 (the slider sat in the window's top-left
        corner) until the window is resized. Measured on 21.2.0.6: the slider
        reads back at x=0 after un-hiding and in its place after RecalcLayout.
        """
        recalc = getattr(win, "RecalcLayout", None)
        if callable(recalc):
            try:
                recalc()
            except Exception:
                pass

    def show_rule():
        """Put the current rule's value in the field; slider only for factor."""
        mode = rule["mode"]
        rule["syncing"] = True
        items["value"].Text = values[mode]
        rule["syncing"] = False
        items["rowSlider"].Hidden = is_resubmit() or mode != "factor"
        relayout()
        if mode == "factor":
            sync_slider_from_text()

    def on_mode_changed(ev):
        values[rule["mode"]] = items["value"].Text.strip() or values[rule["mode"]]
        rule["mode"] = MODES[items["mode"].CurrentIndex]
        show_rule()
        fill_preview()

    def on_slider(ev):
        if rule["syncing"] or rule["mode"] != "factor":
            return
        rule["syncing"] = True
        items["value"].Text = slider_to_factor_text(items["factorSlider"].Value)
        rule["syncing"] = False
        values["factor"] = items["value"].Text
        fill_preview()

    def on_value_edited(ev):
        if rule["syncing"]:
            return
        values[rule["mode"]] = items["value"].Text.strip()
        if rule["mode"] == "factor":
            sync_slider_from_text()

    def fill_preview():
        set_header(["Render timeline", "Plates", "Notes"], [330, 60, 230])
        state["rows"] = {}
        if source is None:
            items["scopeLabel"].Text = "No timeline is open."
            return
        try:
            mode, value = read_rule()
        except ValueError as exc:
            items["scopeLabel"].Text = f"Rule: {exc}"
            return
        plates, scope, skipped = collect_plates(
            source, mode=mode, value=value,
            allow_upscale=items["allowUpscale"].Checked,
            scope=SCOPES[items["scope"].CurrentIndex])
        if items["merge"].Checked:
            plates = merge_plate_ranges(plates)
        groups = group_by_resolution(plates)
        base = items["baseName"].Text
        taken = {tl_name(tl) for tl in list_timelines(project)}
        for (w, h), group in sorted(groups.items(), key=lambda kv: (-kv[0][1], -kv[0][0])):
            name = render_timeline_name(base, w, h)
            notes = []
            native = sum(1 for p in group if p["note"] == "native")
            if native:
                notes.append(f"{native} kept native")
            if name in taken:
                notes.append("exists")
            add_row([name, len(group), ", ".join(notes)])
        text = f"Source '{source_name}': {scope} -> {len(groups)} render timeline(s)"
        if skipped:
            text += f"; {len(skipped)} skipped (see console)"
        items["scopeLabel"].Text = text

    def fill_found():
        set_header(["Render timeline", "Res", "Plates", "Source", "Last queued"],
                   [230, 80, 50, 140, 130])
        state["rows"] = {}
        for idx, (tl, manifest) in enumerate(found):
            w, h = manifest["resolution"]
            row = add_row([tl_name(tl), f"{w}x{h}", manifest.get("clip_count", "?"),
                           (manifest.get("source_timeline") or {}).get("name", "?"),
                           (manifest.get("last_queued_utc") or "never").replace("T", " ")])
            state["rows"][idx] = row
        items["scopeLabel"].Text = (f"{len(found)} render timeline(s) found in this "
                                    f"project. Select the ones to queue again.")

    def refresh_mode(ev=None):
        resub = is_resubmit()
        for wid in ("rowBase", "rowScope", "rowRule", "rowChecks", "rowExisting"):
            items[wid].Hidden = resub
        items["rowSlider"].Hidden = resub or rule["mode"] != "factor"
        items["refresh"].Hidden = resub
        items["selectAll"].Hidden = not resub
        relayout()
        items["go"].Text = "Queue selected" if resub else "Create && Queue"
        if resub:
            fill_found()
        else:
            fill_preview()

    def on_select_all(ev):
        for row in state["rows"].values():
            row.Selected = True

    def on_browse(ev):
        start = items["targetDir"].Text or ""
        path = fu.RequestDir(start) if start else fu.RequestDir()  # noqa: F821
        if path:
            items["targetDir"].Text = str(path)

    def collect_common():
        return {
            "preset": presets[items["preset"].CurrentIndex],
            "target_dir": items["targetDir"].Text.strip(),
            "subfolders": bool(items["subfolders"].Checked),
            "export_audio": bool(items["exportAudio"].Checked),
            "start_render": bool(items["startRender"].Checked),
            "dry_run": bool(items["dryRun"].Checked),
        }

    def on_go(ev):
        opts = collect_common()
        if is_resubmit():
            chosen = [found[idx] for idx, row in state["rows"].items() if row.Selected]
            if not chosen:
                items["scopeLabel"].Text = "Select at least one render timeline."
                return
            opts.update({"action": "resubmit", "entries": chosen})
        else:
            try:
                mode, value = read_rule()
            except ValueError as exc:
                items["scopeLabel"].Text = f"Rule: {exc}"
                return
            if source is None:
                items["scopeLabel"].Text = "Open the timeline to take plates from first."
                return
            opts.update({
                "action": "create",
                "base_name": items["baseName"].Text.strip() or f"{source_name}_plates",
                "mode": mode, "value": value,
                "allow_upscale": bool(items["allowUpscale"].Checked),
                "copy_grades": bool(items["copyGrades"].Checked),
                "merge": bool(items["merge"].Checked),
                "scope": SCOPES[items["scope"].CurrentIndex],
                "existing": EXISTING_POLICIES[items["existing"].CurrentIndex],
            })
        state["result"] = opts
        disp.ExitLoop()

    def on_cancel(ev):
        disp.ExitLoop()

    win.On.RSPWin.Close = on_cancel
    win.On.cancel.Clicked = on_cancel
    win.On.go.Clicked = on_go
    win.On.action.CurrentIndexChanged = refresh_mode
    win.On.refresh.Clicked = lambda ev: fill_preview()
    win.On.mode.CurrentIndexChanged = on_mode_changed
    win.On.factorSlider.ValueChanged = on_slider
    win.On.value.TextChanged = on_value_edited
    win.On.value.EditingFinished = lambda ev: fill_preview()
    win.On.allowUpscale.Clicked = lambda ev: fill_preview()
    win.On.merge.Clicked = lambda ev: fill_preview()
    win.On.scope.CurrentIndexChanged = lambda ev: fill_preview()
    win.On.selectAll.Clicked = on_select_all
    win.On.browse.Clicked = on_browse

    show_rule()
    refresh_mode()
    win.Show()
    disp.RunLoop()
    win.Hide()

    result = state["result"]
    if result is not None:
        prefs.update({k: result[k] for k in (
            "preset", "target_dir", "subfolders", "export_audio", "start_render")})
        prefs["action"] = result["action"]
        if result["action"] == "create":
            prefs.update({k: result[k] for k in (
                "mode", "allow_upscale", "copy_grades", "merge", "existing",
                "scope")})
            values[result["mode"]] = str(result["value"])
            prefs["values"] = values
            prefs.pop("value", None)
        save_prefs(prefs)
    return result


def summary_headline(action, built, queued, wanted, rendering, dry_run):
    """One line saying what a run did, for the summary window."""
    if dry_run:
        return "Dry run: nothing was changed. The plan is below."
    parts = []
    if action == "create":
        parts.append(f"Built or re-used {built} render timeline(s)")
    parts.append(f"queued {queued} of {wanted} job(s)")
    if rendering is True:
        parts.append("rendering started")
    elif rendering is False:
        parts.append("rendering did NOT start")
    text = ", ".join(parts)
    return text[0].upper() + text[1:] + "."


class _Tee(io.TextIOBase):
    """Copies everything printed during a run, so the summary can show it."""

    def __init__(self, target):
        self.target, self.parts = target, []

    def write(self, text):
        self.parts.append(text)
        try:
            return self.target.write(text)
        except Exception:
            return len(text)

    def flush(self):
        try:
            self.target.flush()
        except Exception:
            pass


def show_summary(headline, log_text):
    """A window with the outcome and the run's log. The console is easy to
    miss, and a run that did less than expected should say so on screen."""
    ui = fu.UIManager  # noqa: F821
    disp = bmd.UIDispatcher(ui)  # noqa: F821
    win = disp.AddWindow({
        "ID": "RSPSummary",
        "WindowTitle": f"{TOOL_NAME} - done",
        "Geometry": [160, 160, 760, 520],
    }, [
        ui.VGroup({}, [
            ui.Label({"ID": "headline", "Text": headline, "Weight": 0,
                      "WordWrap": True, "StyleSheet": "font-weight: bold;"}),
            ui.TextEdit({"ID": "log", "ReadOnly": True, "Text": log_text,
                         "Font": ui.Font({"Family": "Consolas", "PixelSize": 12})}),
            ui.HGroup({"Weight": 0}, [
                ui.HGap(0, 1),
                ui.Button({"ID": "ok", "Text": "OK", "Default": True}),
            ]),
        ]),
    ])
    win.On.ok.Clicked = lambda ev: disp.ExitLoop()
    win.On.RSPSummary.Close = lambda ev: disp.ExitLoop()
    win.Show()
    disp.RunLoop()
    win.Hide()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(project, opts):
    """Carry out what the dialog asked for. Returns the summary headline."""
    print(f"=== {TOOL_NAME} {TOOL_VERSION} ===")
    if opts["action"] == "create":
        entries = run_create(project, opts)
    else:
        entries = opts["entries"]
        if opts["dry_run"]:
            for tl, manifest in entries:
                w, h = manifest["resolution"]
                where = (target_dir_for(opts["target_dir"], w, h, opts["subfolders"])
                         or "the preset's folder")
                print(f"  would queue '{tl_name(tl)}' at {w}x{h} -> {where}")
    jobs = []
    if not opts["dry_run"]:
        jobs = run_queue(project, entries, opts)
    rendering = opts.get("render_started") if opts.get("start_render") else None
    return summary_headline(opts["action"], len(entries), len(jobs), len(entries),
                            rendering, opts["dry_run"])


def main():
    project = resolve.GetProjectManager().GetCurrentProject()  # noqa: F821
    if project is None:
        print("ERROR: no project is open.")
        return
    opts = build_and_show_ui(project)
    if opts is None:
        print("Cancelled.")
        return
    tee = _Tee(sys.stdout)
    old_stdout, sys.stdout = sys.stdout, tee
    try:
        headline = run(project, opts)
    except Exception as exc:  # show it rather than leave the user guessing
        import traceback
        traceback.print_exc(file=tee)
        headline = f"Stopped by an error: {exc}"
    finally:
        sys.stdout = old_stdout
    try:
        show_summary(headline, "".join(tee.parts))
    except Exception as exc:
        print(f"(could not show the summary window: {exc})")


main()
