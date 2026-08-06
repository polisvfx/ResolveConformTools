#!/usr/bin/env python
"""
Generate All Clips Timeline PRO
Version: 1.3

Creates a master timeline from selected timelines, collecting all unique source clips,
merging overlapping source ranges, and placing them on a new timeline.

Supports detection and marking of:
- Duplicate clips (colored markers)
- Retimed clips (red markers with speed info)
- Frame holds / freeze frames
- Non-linear retimes / speed ramps (via XML analysis)
- Reversed clips (normalized to forward-playing)
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Constants
# Keep in step with the "Version:" line in the module docstring above — the repo
# convention is that the docstring is authoritative, this is what gets recorded
# into the manifest so an old timeline says which build last touched it.
TOOL_VERSION = "1.3"
MIN_FRAME_DIFF = 3
MIN_PERCENT_DIFF = 3.0
DEFAULT_CONNECTION_THRESHOLD = 25
INTER_TIMELINE_GAP = 25  # frames between source-timeline blocks in preserve-layout mode
# AppendToTimeline can place clips with a small source-frame slip relative to
# what we requested (observed: -1/0 on start, 0/-1 on end depending on whether
# the request hits a media-end clamp or an auto pre-roll handle). The post-
# processing matchers (duplicate/retime markers, clip-name import) use this
# tolerance when correlating placed timeline items back to their ClipInfo.
SLIP_TOLERANCE = 2
# Max attempts to compensate for AppendToTimeline source-frame slip before
# accepting whatever Resolve produced and logging a warning.
SLIP_RETRY_LIMIT = 2

# Diagnostics
# If XML retime expansion would grow a clip's source range by more than this
# factor, skip the expansion and keep the API range. Catches the "full length
# inclusion" case caused by Time Remap keyframes referencing the entire source.
XML_EXPANSION_MAX_GROWTH = 5.0
# In the end-of-run Range Audit, flag any clip whose final source range on the
# new timeline is >= this many times the original API-reported range.
RANGE_AUDIT_GROWTH_THRESHOLD = 2.0

TIMELINE_BLOCK_MARKER_COLORS = [
    "Blue", "Cyan", "Green", "Yellow", "Red",
    "Pink", "Purple", "Fuchsia", "Rose", "Lavender",
    "Sky", "Mint", "Lemon", "Sand", "Cocoa",
]

DUPLICATE_MARKER_COLORS = [
    "Yellow", "Green", "Cyan", "Blue", "Purple", "Pink",
    "Fuchsia", "Lavender", "Rose", "Cocoa", "Sand", "Sky", "Mint", "Lemon",
]

# Update-mode manifest. Stamped onto an All Clips timeline so a later run knows
# which source timelines it was built from and with which settings — neither is
# recoverable by looking at the output. Everything else (which clips are on the
# timeline, where, and over what source range) is deliberately NOT stored: it is
# re-scanned every run so edits made in between are respected rather than
# overwritten from a stale cache.
MANIFEST_SCHEMA = 1
MANIFEST_KEY = "RCT_AllClipsManifest"
MANIFEST_MARKER_PREFIX = "RCT_AllClipsManifest_v1:"
MANIFEST_MARKER_COLOR = "Cream"  # the one colour no other pass in this file uses
MANIFEST_MARKER_NAME = "All Clips Manifest"
MANIFEST_MAX_RUNS = 20
# The customData size limit is undocumented; keep the marker copy well clear of
# anything that might be a cliff by dropping the oldest run records first.
MANIFEST_MARKER_MAX_CHARS = 8000

# customData prefix on the per-clip update marker. Carries the desired range the
# clip was last reconciled against, which is what stops a clip whose target is
# physically unreachable from being rebuilt on every run forever.
UPDATE_MARKER_PREFIX = "RCT_UPD:"
# A source range must move by more than this many frames before the update
# rebuilds the clip. Must stay >= SLIP_TOLERANCE so last run's own placement slip
# never reads as a change; 3 also matches MIN_FRAME_DIFF, the "difference that
# matters" threshold the retime detector already uses.
UPDATE_CHANGE_TOLERANCE = 3
# Free space after the last clip on a track: there is always more timeline.
FREE_SPACE_UNBOUNDED = 1 << 30
# Spare frames to reserve on each side when deciding whether a grown clip may
# also use the append helper's +/-1 widening fallback. Without it, widening
# collides with the neighbour and the append fails.
REBUILD_FIT_SLACK = 1

# Per-clip update markers. Colours are Resolve marker names; the overlap with
# DUPLICATE_MARKER_COLORS is harmless — different frame, name and customData.
UPDATE_MARKER_COLORS = {
    "extended": "Green",
    "shortened": "Yellow",
    "both": "Sand",
    "new": "Mint",
    "superseded": "Purple",
    "dropped": "Rose",
    "manual": "Fuchsia",
}
# Fraction of the clip the update marker sits at. The duplicate pass uses 0.25
# and the retime pass 0.5, so 0.75 keeps all three apart on all but tiny clips —
# and pick_marker_frame() steps off a collision even then.
UPDATE_MARKER_FRACTION = 0.75
RUN_MARKER_PREFIX = "RCT_RUN:"
RUN_MARKER_COLOR = "Sky"
# Changelog lines kept in a clip's update marker note before the tail is folded
# into a "... (N earlier changes)" summary line.
CHANGELOG_MAX_LINES = 8
CHANGELOG_TRUNCATION_MARK = "... ("
# Marker text stays ASCII: it round-trips through the scripting bridge and back
# out through GetMarkers on every subsequent run.
CHANGELOG_SEPARATOR = " | "


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ClipInfo:
    """Source clip frame range for timeline assembly.

    All frame ranges are normalized: start_frame <= end_frame.
    Metadata fields track the original clip's editorial state.
    """
    media_pool_item: object
    start_frame: int
    end_frame: int
    timeline_inpoint: int = 0
    timeline_endpoint: int = 0
    source_track_index: int = 1
    source_timeline_name: str = ""
    is_reversed: bool = False
    is_retimed: bool = False
    is_frame_hold: bool = False
    is_non_linear_retime: bool = False
    retime_percentage: Optional[float] = None
    xml_source_min: Optional[int] = None
    xml_source_max: Optional[int] = None
    source_name: Optional[str] = None
    version_names: list = field(default_factory=list)
    current_version_name: Optional[str] = None
    # Populated by find_duplicate_groups after merging. None = not a duplicate.
    duplicate_set_index: Optional[int] = None
    duplicate_position: Optional[int] = None
    duplicate_set_size: Optional[int] = None
    # Raw API-reported source range, captured BEFORE XML expansion and merging.
    # Used by the end-of-run Range Audit to detect ranges that grew unexpectedly.
    api_source_start: Optional[int] = None
    api_source_end: Optional[int] = None


@dataclass
class TimelineClipData:
    """Wrapper for timeline items used in duplicate detection and marker placement."""
    clip: object
    media_pool_item: object
    start_frame: int
    name: str
    track_index: int
    retime_percentage: Optional[float] = None
    is_non_linear_retime: bool = False


@dataclass
class RetimeKeyframe:
    """A single keyframe from the FCP XML Time Remap effect."""
    when: int
    value: int


@dataclass
class PlacedClip:
    """A clip found on an existing All Clips timeline during an update scan.

    record_end is EXCLUSIVE and is computed as GetStart() + GetDuration() rather
    than from GetEnd(), whose inclusivity the API docs leave ambiguous. Duration
    is unambiguous, so occupancy arithmetic never depends on that reading.
    """
    item: object
    identity: str
    source_start: int          # inclusive, normalised so start <= end
    source_end: int            # inclusive
    record_start: int          # absolute timeline frame
    record_end: int            # exclusive
    track_index: int
    name: str = ""
    # Desired range this clip was last reconciled against, read back from its own
    # update marker. None when the clip has never been through an update.
    accepted_start: Optional[int] = None
    accepted_end: Optional[int] = None
    linked_item_count: int = 0


@dataclass
class ItemState:
    """Everything about a TimelineItem that survives a delete + re-append.

    Grades and Fusion comps are absent because they cannot be carried across:
    CopyGrades copies FROM a live source item, and SetCDL has no getter. Their
    presence is recorded so a clip that would lose them can be reported, and
    optionally left alone.
    """
    name: Optional[str] = None
    clip_color: Optional[str] = None
    flags: list = field(default_factory=list)
    markers: dict = field(default_factory=dict)
    enabled: Optional[bool] = None
    fusion_comp_count: int = 0
    version_names: list = field(default_factory=list)
    linked_item_count: int = 0
    track_index: int = 1
    record_start: int = 0
    duration: int = 0
    source_start: int = 0
    source_end: int = 0


@dataclass
class ClipDiff:
    """One reconciliation decision: what to do about one shot."""
    identity: str
    kind: str                          # unchanged|extended|shortened|both|new|dropped
    clip_info: Optional[ClipInfo] = None   # None for "dropped"
    placed: Optional[PlacedClip] = None    # None for "new"
    head_delta: int = 0                # desired_start - placed_start; <0 grows
    tail_delta: int = 0                # desired_end - placed_end;    >0 grows


# ---------------------------------------------------------------------------
# XML Retime Analysis
# ---------------------------------------------------------------------------

def export_timeline_to_xml(timeline) -> Optional[str]:
    """Export a timeline to FCP 7 XML in a temp file. Returns the file path or None."""
    try:
        fd, temp_path = tempfile.mkstemp(suffix=".xml", prefix="resolve_export_")
        os.close(fd)
        success = timeline.Export(temp_path, resolve.EXPORT_FCP_7_XML)  # noqa: F821
        if success:
            print(f"  Exported timeline XML to: {temp_path}")
            return temp_path
        else:
            print("  Warning: XML export failed (may require Resolve Studio)")
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            return None
    except Exception as e:
        print(f"  Warning: XML export error: {e}")
        return None


def parse_time_remap_keyframes(clip_element: ET.Element) -> list[RetimeKeyframe]:
    """Extract Time Remap keyframes from a <clipitem> element.

    Looks for <effect><name>Time Remap</name> with <parameter> containing
    <keyframe> entries with <when> and <value>.
    """
    keyframes = []
    for effect in clip_element.findall(".//effect"):
        name_el = effect.find("name")
        if name_el is None or name_el.text != "Time Remap":
            continue
        for param in effect.findall("parameter"):
            for kf in param.findall("keyframe"):
                when_el = kf.find("when")
                value_el = kf.find("value")
                if when_el is not None and value_el is not None:
                    try:
                        keyframes.append(RetimeKeyframe(
                            when=int(when_el.text),
                            value=int(value_el.text),
                        ))
                    except (ValueError, TypeError):
                        continue
    return keyframes


def compute_source_range_from_keyframes(
    keyframes: list[RetimeKeyframe],
) -> tuple[int, int, bool]:
    """From retime keyframes, compute (min_source_frame, max_source_frame, is_non_linear).

    is_non_linear is True if the speed varies between keyframe segments.
    """
    if not keyframes:
        return (0, 0, False)

    values = [kf.value for kf in keyframes]
    src_min = min(values)
    src_max = max(values)

    # Check for non-linear speed: compare speed ratios between consecutive segments
    is_non_linear = False
    if len(keyframes) >= 3:
        speeds = []
        for i in range(len(keyframes) - 1):
            dt = keyframes[i + 1].when - keyframes[i].when
            dv = keyframes[i + 1].value - keyframes[i].value
            if dt != 0:
                speeds.append(dv / dt)
        if speeds:
            ref_speed = speeds[0]
            for s in speeds[1:]:
                if abs(s - ref_speed) > 0.01:
                    is_non_linear = True
                    break

    return (src_min, src_max, is_non_linear)


def build_xml_clip_lookup(xml_path: str) -> dict[str, list[dict]]:
    """Parse FCP 7 XML and build a lookup from clip name to retime data.

    Returns: {clip_file_name: [{source_min, source_max, is_non_linear}, ...]}
    """
    lookup: dict[str, list[dict]] = {}
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"  Warning: XML parse error: {e}")
        return lookup

    for clip_item in root.iter("clipitem"):
        keyframes = parse_time_remap_keyframes(clip_item)
        if not keyframes:
            continue

        # Get clip name from <name> or <file><name>
        clip_name = None
        name_el = clip_item.find("name")
        if name_el is not None and name_el.text:
            clip_name = name_el.text
        if not clip_name:
            file_el = clip_item.find("file")
            if file_el is not None:
                file_name_el = file_el.find("name")
                if file_name_el is not None and file_name_el.text:
                    clip_name = file_name_el.text
        if not clip_name:
            continue

        src_min, src_max, is_non_linear = compute_source_range_from_keyframes(keyframes)
        lookup.setdefault(clip_name, []).append({
            "source_min": src_min,
            "source_max": src_max,
            "is_non_linear": is_non_linear,
        })

    return lookup


def match_xml_retime_to_clip(
    clip_name: str,
    clip_source_start: int,
    clip_source_end: int,
    xml_lookup: dict[str, list[dict]],
) -> Optional[dict]:
    """Match a Resolve timeline clip to its XML counterpart by name and overlapping range."""
    entries = xml_lookup.get(clip_name)
    if not entries:
        return None
    for entry in entries:
        # Check for overlapping source range
        if entry["source_max"] >= clip_source_start and entry["source_min"] <= clip_source_end:
            return entry
    # No overlap found — don't guess; returning a wrong entry would expand ranges incorrectly
    return None


# ---------------------------------------------------------------------------
# Sorting Functions
# ---------------------------------------------------------------------------

def sort_by_start_frame(clip_infos: list[ClipInfo]) -> None:
    clip_infos.sort(key=lambda c: c.start_frame)


def sort_by_source_name(clip_infos: list[ClipInfo]) -> None:
    clip_infos.sort(key=lambda c: (c.media_pool_item.GetName(), c.timeline_inpoint))


def sort_by_reel_name(clip_infos: list[ClipInfo]) -> None:
    def reel_key(c: ClipInfo):
        reel = c.media_pool_item.GetClipProperty("Reel Name") or ""
        name = c.media_pool_item.GetName()
        if reel:
            return (0, reel, c.timeline_inpoint)
        return (1, name, c.timeline_inpoint)
    clip_infos.sort(key=reel_key)


def sort_by_timeline_inpoint(clip_infos: list[ClipInfo]) -> None:
    clip_infos.sort(key=lambda c: c.timeline_inpoint)


SORT_METHODS = {
    "Source Name": sort_by_source_name,
    "Source Inpoint": sort_by_start_frame,
    "Inpoint on Timeline": sort_by_timeline_inpoint,
    "Reel Name": sort_by_reel_name,
    "None": lambda _: None,
}


# ---------------------------------------------------------------------------
# Clip Merging Functions
# ---------------------------------------------------------------------------

def check_overlap(a: ClipInfo, b: ClipInfo, threshold: int) -> bool:
    """Check if two normalized ClipInfos overlap within threshold."""
    return not (a.end_frame < b.start_frame - threshold or
                b.end_frame < a.start_frame - threshold)


def merge_two(a: ClipInfo, b: ClipInfo) -> ClipInfo:
    """Merge two overlapping ClipInfos into one spanning both ranges."""
    merged_min = min(a.start_frame, b.start_frame)
    merged_max = max(a.end_frame, b.end_frame)

    print(f"Merging clips with frames:")
    print(f"  A: {a.start_frame} to {a.end_frame}")
    print(f"  B: {b.start_frame} to {b.end_frame}")
    print(f"  Merged: {merged_min} to {merged_max}")

    # Merge XML source ranges if available
    xml_min = None
    xml_max = None
    if a.xml_source_min is not None and b.xml_source_min is not None:
        xml_min = min(a.xml_source_min, b.xml_source_min)
    else:
        xml_min = a.xml_source_min if a.xml_source_min is not None else b.xml_source_min
    if a.xml_source_max is not None and b.xml_source_max is not None:
        xml_max = max(a.xml_source_max, b.xml_source_max)
    else:
        xml_max = a.xml_source_max if a.xml_source_max is not None else b.xml_source_max

    # Carry the API-reported source range through the merge as the span of
    # constituent API ranges. Range Audit compares final start/end_frame to
    # this span — any growth past 1.0x means XML expansion was applied to
    # at least one constituent.
    api_min: Optional[int] = None
    api_max: Optional[int] = None
    if a.api_source_start is not None and b.api_source_start is not None:
        api_min = min(a.api_source_start, b.api_source_start)
    else:
        api_min = a.api_source_start if a.api_source_start is not None else b.api_source_start
    if a.api_source_end is not None and b.api_source_end is not None:
        api_max = max(a.api_source_end, b.api_source_end)
    else:
        api_max = a.api_source_end if a.api_source_end is not None else b.api_source_end

    # Union version names from both clips, preserving order; active version taken from a
    merged_versions: list = []
    seen_versions: set = set()
    for vname in (a.version_names or []) + (b.version_names or []):
        if vname not in seen_versions:
            merged_versions.append(vname)
            seen_versions.add(vname)

    return ClipInfo(
        media_pool_item=a.media_pool_item,
        start_frame=merged_min,
        end_frame=merged_max,
        timeline_inpoint=min(a.timeline_inpoint, b.timeline_inpoint),
        timeline_endpoint=max(a.timeline_endpoint, b.timeline_endpoint),
        source_track_index=a.source_track_index,
        source_timeline_name=a.source_timeline_name,
        is_reversed=a.is_reversed or b.is_reversed,
        is_retimed=a.is_retimed or b.is_retimed,
        is_frame_hold=a.is_frame_hold and b.is_frame_hold,
        is_non_linear_retime=a.is_non_linear_retime or b.is_non_linear_retime,
        retime_percentage=a.retime_percentage if a.retime_percentage is not None else b.retime_percentage,
        xml_source_min=xml_min,
        xml_source_max=xml_max,
        source_name=a.source_name if a.source_name is not None else b.source_name,
        version_names=merged_versions,
        current_version_name=a.current_version_name if a.current_version_name is not None else b.current_version_name,
        api_source_start=api_min,
        api_source_end=api_max,
    )


def merge_all_overlapping(clip_infos: list[ClipInfo], threshold: int) -> list[ClipInfo]:
    """Iteratively merge all overlapping clips for the same media pool item."""
    sort_by_start_frame(clip_infos)
    i = 0
    while i < len(clip_infos):
        j = i + 1
        merged = False
        while j < len(clip_infos):
            if check_overlap(clip_infos[i], clip_infos[j], threshold):
                print(f"Merging clips: {clip_infos[i].media_pool_item.GetName()}")
                clip_infos[i] = merge_two(clip_infos[i], clip_infos[j])
                clip_infos.pop(j)
                merged = True
            else:
                j += 1
        if not merged:
            i += 1
    return clip_infos


# ---------------------------------------------------------------------------
# Clip Identity
# ---------------------------------------------------------------------------

def clip_identity_key(media_pool_item, merge_by_source_file: bool) -> Optional[str]:
    """Bucket identity for a MediaPoolItem.

    - merge_by_source_file=True: key by File Path so two MediaPoolItems pointing
      at the same source file (the dual-import scenario from round-tripped
      conform timelines) land in the same bucket and get merged. Falls back to
      the MediaId when File Path is empty.
    - merge_by_source_file=False: key by MediaId (legacy behaviour).

    Returns None when neither is available; the caller skips the clip.

    Both the collection pass and the update pass's timeline scan go through this
    so a clip already on an All Clips timeline is recognised as the same shot the
    collector just produced. Any track suffix (preserve-track-layout mode) is the
    caller's business: an output-timeline track has no relationship to a source
    track and must never enter the identity.
    """
    media_id = None
    try:
        media_id = media_pool_item.GetMediaId()
    except Exception:
        media_id = None

    if merge_by_source_file:
        try:
            file_path = media_pool_item.GetClipProperty("File Path") or ""
        except Exception:
            file_path = ""
        if file_path:
            return f"FP:{file_path}"

    return media_id or None


# ---------------------------------------------------------------------------
# Duplicate Detection & Marking
# ---------------------------------------------------------------------------

def find_duplicate_groups(all_clip_infos: list[ClipInfo]) -> int:
    """Identify duplicate ClipInfo groups (same media pool item, 2+ entries
    after merging) and stamp duplicate_set_index / duplicate_position /
    duplicate_set_size onto each member. Returns the number of duplicate sets.
    """
    by_mid: dict[str, list[ClipInfo]] = {}
    for ci in all_clip_infos:
        try:
            mid = ci.media_pool_item.GetMediaId()
        except Exception:
            mid = None
        if not mid:
            continue
        by_mid.setdefault(mid, []).append(ci)

    set_index = 0
    for group in by_mid.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda c: (c.timeline_inpoint, c.source_track_index))
        size = len(group)
        for position, ci in enumerate(group):
            ci.duplicate_set_index = set_index
            ci.duplicate_position = position
            ci.duplicate_set_size = size
        set_index += 1

    return set_index


def get_all_timeline_clips(timeline) -> list[TimelineClipData]:
    """Extract all video clips from all tracks of a timeline."""
    clip_list = []
    if not timeline:
        return clip_list

    tracks_count = timeline.GetTrackCount("video")
    print(f"Found {tracks_count} video tracks")

    for track_index in range(1, tracks_count + 1):
        clips = timeline.GetItemListInTrack("video", track_index)
        if not clips:
            print(f"No clips found in track {track_index}")
            continue

        print(f"Track {track_index}: Found {len(clips)} clips")
        for clip_index, clip in enumerate(clips):
            try:
                media_pool_item = clip.GetMediaPoolItem()
            except Exception:
                media_pool_item = None

            if media_pool_item:
                file_path = media_pool_item.GetClipProperty("File Path") or "N/A"
                clip_name = clip.GetName()
                print(f"Clip {clip_index + 1} on track {track_index}: {clip_name}")
                print(f"  File Path: {file_path}")
                clip_list.append(TimelineClipData(
                    clip=clip,
                    media_pool_item=media_pool_item,
                    start_frame=clip.GetStart(),
                    name=clip_name,
                    track_index=track_index,
                ))
            else:
                print(f"Clip {clip_index + 1} on track {track_index} has no media pool item")

    print(f"Total clips found: {len(clip_list)}")
    return clip_list


# ---------------------------------------------------------------------------
# Retime Detection
# ---------------------------------------------------------------------------

def check_retime_properties(clip: TimelineClipData) -> tuple[bool, Optional[float], bool]:
    """Check clip properties for retime indicators.

    Returns: (is_retimed, retime_percentage, is_non_linear)
    """
    # Check Speed property
    speed = None
    try:
        speed = float(clip.clip.GetClipProperty("Speed"))
    except (ValueError, TypeError, Exception):
        pass

    if speed is not None and speed != 100.0:
        print(f"Detected retimed clip via property: {clip.name} with speed: {speed}%")
        return (True, speed, False)

    # Check for non-linear retime (speed curve/ramp)
    retime_curve = None
    try:
        retime_curve = clip.clip.GetClipProperty("Retime Curve")
    except Exception:
        pass
    if retime_curve and retime_curve != "" and retime_curve != "None":
        print(f"Detected non-linear retimed clip via Retime Curve: {clip.name} ({retime_curve})")
        return (True, None, True)

    # Check other retiming attributes
    for attr in ("Retime Process", "Motion Estimation", "Frame Interpolation"):
        value = None
        try:
            value = clip.clip.GetClipProperty(attr)
        except Exception:
            pass
        if value and value != "" and value != "None":
            print(f"Detected retimed clip via property: {clip.name} with {attr}: {value}")
            return (True, None, False)

    return (False, None, False)


def is_clip_retimed(clip: TimelineClipData) -> tuple[bool, Optional[float], bool]:
    """Comprehensive retime detection: duration comparison + property check.

    Returns: (is_retimed, retime_percentage, is_non_linear)
    """
    if not clip or not clip.clip:
        print("Warning: Invalid clip object in is_clip_retimed")
        return (False, None, False)

    # Safely get durations
    try:
        timeline_duration = clip.clip.GetEnd() - clip.clip.GetStart()
        source_duration = clip.clip.GetSourceEndFrame() - clip.clip.GetSourceStartFrame()
    except Exception:
        print(f"Warning: Could not calculate durations for clip: {clip.name}")
        return check_retime_properties(clip)

    # Guard against division by zero (frame holds have sourceDuration of 0 or 1)
    if source_duration <= 0:
        print(f"Warning: Source duration is {source_duration} for clip: {clip.name} (likely frame hold)")
        return (True, 0.0, False)

    # Calculate differences
    frame_diff = abs(timeline_duration - source_duration)
    retime_percentage = (timeline_duration / source_duration) * 100
    percent_diff = abs(retime_percentage - 100)

    if frame_diff > MIN_FRAME_DIFF and percent_diff > MIN_PERCENT_DIFF:
        print(f"Detected retimed clip: {clip.name} with speed: {retime_percentage:.1f}%")
        return (True, retime_percentage, False)

    # Fall back to property checks
    return check_retime_properties(clip)


# ---------------------------------------------------------------------------
# API Helper
# ---------------------------------------------------------------------------

def sanitize_for_api(clip_info: ClipInfo) -> dict:
    """Create a clean dict with only API-recognized fields for AppendToTimeline."""
    return {
        "mediaPoolItem": clip_info.media_pool_item,
        "startFrame": clip_info.start_frame,
        "endFrame": clip_info.end_frame,
    }


# ---------------------------------------------------------------------------
# Timeline Resolution
# ---------------------------------------------------------------------------

def get_timeline_for_media_pool_item(media_pool_item):
    """Return the Timeline a timeline-type MediaPoolItem points at, or None.

    MediaPoolItem.GetTimeline() arrived in DaVinci Resolve 21.0.4 and resolves the
    item exactly, with no dependence on timeline names being unique. Older builds
    resolve the unknown attribute to None instead of raising AttributeError, so
    probe with getattr + callable() and let the caller fall back to a name lookup.

    The 21.0.4 docs label the return type "TimelineItem" while the prose says
    "timeline object". Verified on 21.0.4.5: it is a real Timeline — GetTrackCount
    and GetItemListInTrack work, GetLeftOffset and GetSourceStartFrame are absent.
    The duck-type check keeps that verification honest: anything that does not
    behave like a Timeline falls through to the name lookup, so a future API
    surprise degrades to the old behaviour instead of crashing.
    """
    getter = getattr(media_pool_item, "GetTimeline", None)
    if not callable(getter):
        return None
    try:
        timeline = getter()
    except Exception:
        return None
    if timeline is None:
        return None
    if not callable(getattr(timeline, "GetTrackCount", None)):
        return None
    return timeline


# ---------------------------------------------------------------------------
# Update Manifest
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Current UTC time as 2026-08-06T12:22:33Z — the manifest's machine clock."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(timeline_uid, timeline_name, sources, settings, now_iso,
                   tool_version) -> dict:
    """A fresh manifest for an All Clips timeline.

    `sources` is [{"uid": ..., "name": ...}] for the timelines it was built from;
    the uid is exact and survives renames, the name is the only thing a human can
    act on once a uid stops resolving. `settings` is the generation settings that
    shaped the merged ranges — re-running with a different connection threshold
    reshapes nearly every clip, so the previous value has to be recoverable.
    """
    return {
        "schema": MANIFEST_SCHEMA,
        "tool": "Generate All Clips Timeline PRO",
        "tool_version": tool_version,
        "timeline_uid": timeline_uid or "",
        "timeline_name": timeline_name or "",
        "created_utc": now_iso,
        "last_run_utc": now_iso,
        "run_counter": 0,
        "adopted": False,
        "sources": list(sources or []),
        "settings": dict(settings or {}),
        "runs": [],
    }


def manifest_add_run(manifest: dict, run_record: dict,
                     max_runs: int = MANIFEST_MAX_RUNS) -> dict:
    """Append a run record, bump the counters, cap the history. Returns a copy."""
    updated = dict(manifest)
    runs = list(manifest.get("runs") or [])
    runs.append(dict(run_record))
    if max_runs >= 0:
        runs = runs[-max_runs:] if max_runs else []
    updated["runs"] = runs
    updated["run_counter"] = run_record.get("n", manifest.get("run_counter", 0) + 1)
    if run_record.get("utc"):
        updated["last_run_utc"] = run_record["utc"]
    return updated


def encode_manifest(manifest: dict) -> str:
    """Compact, key-sorted JSON so the same manifest always encodes identically."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def decode_manifest(text) -> Optional[dict]:
    """Parse a manifest payload, with or without the marker prefix.

    Returns None for anything unusable — absent, junk, not an object, or written
    by a schema this build does not understand. A newer schema is reported loudly
    rather than half-read, because acting on a manifest we only partly understand
    is how an update deletes the wrong clips.
    """
    if not text:
        return None
    if isinstance(text, dict):
        # GetThirdPartyMetadata can hand back {key: value} instead of the value.
        text = text.get(MANIFEST_KEY) or ""
    if not isinstance(text, str):
        return None
    payload = text.strip()
    if payload.startswith(MANIFEST_MARKER_PREFIX):
        payload = payload[len(MANIFEST_MARKER_PREFIX):]
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if schema != MANIFEST_SCHEMA:
        if schema is not None:
            print(f"  NOTE: ignoring manifest with unsupported schema {schema!r} "
                  f"(this build understands {MANIFEST_SCHEMA}).")
        return None
    return data


def trim_manifest_for_marker(manifest: dict,
                             max_chars: int = MANIFEST_MARKER_MAX_CHARS) -> dict:
    """Drop the oldest run records until the encoded manifest fits `max_chars`.

    Never drops sources or settings — those are the parts that cannot be
    re-derived. If it still does not fit with no runs left, the manifest is
    returned as-is and the caller writes what it can.
    """
    trimmed = dict(manifest)
    runs = list(manifest.get("runs") or [])
    while runs and len(MANIFEST_MARKER_PREFIX) + len(
            encode_manifest({**trimmed, "runs": runs})) > max_chars:
        runs = runs[1:]
    trimmed["runs"] = runs
    return trimmed


def normalised_markers(obj) -> dict:
    """GetMarkers() as {int frame: info}. Resolve returns float keys (96.0)."""
    getter = getattr(obj, "GetMarkers", None)
    if not callable(getter):
        return {}
    try:
        raw = getter()
    except Exception:
        return {}
    if not raw:
        return {}
    markers = {}
    try:
        for frame, info in raw.items():
            try:
                markers[int(frame)] = info
            except (TypeError, ValueError):
                continue
    except AttributeError:
        return {}
    return markers


def first_free_frame(existing_frames, preferred: int,
                     lower: Optional[int] = None,
                     upper: Optional[int] = None) -> int:
    """First frame free of `existing_frames` at or after `preferred`.

    Falls back to searching backwards towards `lower` when the forward range is
    exhausted. Returns -1 when every frame in [lower, upper] is taken. Resolve
    permits only one marker per frame, so any code adding a marker near a
    fraction of a clip has to be able to step off a collision.
    """
    taken = {int(f) for f in existing_frames}
    if upper is None:
        upper = preferred + len(taken) + 1
    start = preferred if lower is None else max(preferred, lower)
    for frame in range(start, upper + 1):
        if frame not in taken:
            return frame
    if lower is not None:
        for frame in range(min(preferred, upper), lower - 1, -1):
            if frame not in taken:
                return frame
    return -1


def find_manifest_marker_frame(markers: dict) -> Optional[int]:
    """Frame of the manifest marker in a normalised markers dict, or None.

    Scans for the customData prefix rather than using GetMarkerByCustomData,
    which matches the whole string exactly and so cannot find a payload whose
    tail changes on every run.
    """
    for frame in sorted(markers):
        info = markers[frame] or {}
        custom = info.get("customData") or ""
        if isinstance(custom, str) and custom.startswith(MANIFEST_MARKER_PREFIX):
            return frame
    return None


def read_manifest(timeline) -> tuple:
    """Read the manifest off a timeline. Returns (manifest, source).

    source is "metadata", "marker" or "none". Both stores are written on every
    update; whichever answers first wins. Third-party metadata is the primary
    because it is invisible, but it is a camera/sidecar concept and a timeline
    has no media file, so the marker is what makes this work if Resolve declines
    to persist it.
    """
    getter = getattr(timeline, "GetMediaPoolItem", None)
    if callable(getter):
        try:
            mpi = getter()
        except Exception:
            mpi = None
        if mpi is not None:
            reader = getattr(mpi, "GetThirdPartyMetadata", None)
            if callable(reader):
                try:
                    raw = reader(MANIFEST_KEY)
                except Exception:
                    raw = None
                manifest = decode_manifest(raw)
                if manifest is not None:
                    return manifest, "metadata"

    markers = normalised_markers(timeline)
    frame = find_manifest_marker_frame(markers)
    if frame is not None:
        manifest = decode_manifest((markers[frame] or {}).get("customData"))
        if manifest is not None:
            return manifest, "marker"

    return None, "none"


def write_manifest(timeline, manifest: dict) -> dict:
    """Write the manifest to both stores. Returns {"metadata": ok, "marker": ok}."""
    result = {"metadata": False, "marker": False}
    encoded = encode_manifest(manifest)

    getter = getattr(timeline, "GetMediaPoolItem", None)
    if callable(getter):
        try:
            mpi = getter()
        except Exception:
            mpi = None
        if mpi is not None:
            writer = getattr(mpi, "SetThirdPartyMetadata", None)
            if callable(writer):
                try:
                    result["metadata"] = bool(writer(MANIFEST_KEY, encoded))
                except Exception:
                    result["metadata"] = False

    markers = normalised_markers(timeline)
    existing = find_manifest_marker_frame(markers)
    if existing is not None:
        try:
            timeline.DeleteMarkerAtFrame(existing)
        except Exception:
            pass
        markers.pop(existing, None)

    trimmed = trim_manifest_for_marker(manifest)
    dropped = len(manifest.get("runs") or []) - len(trimmed.get("runs") or [])
    if dropped > 0:
        print(f"  NOTE: manifest marker trimmed by {dropped} old run record(s) "
              f"to stay under {MANIFEST_MARKER_MAX_CHARS} characters.")

    frame = existing if existing is not None else first_free_frame(markers, 0, lower=0)
    if frame < 0:
        print("  WARNING: no free frame for the manifest marker.")
        return result

    note = (f"Managed by Generate All Clips Timeline PRO.\n"
            f"Run {manifest.get('run_counter', 0)} · "
            f"{manifest.get('last_run_utc', '')}\n"
            f"Sources: "
            + ", ".join(s.get("name", "?") for s in manifest.get("sources") or []))
    try:
        result["marker"] = bool(timeline.AddMarker(
            frame, MANIFEST_MARKER_COLOR, MANIFEST_MARKER_NAME, note, 1,
            MANIFEST_MARKER_PREFIX + encode_manifest(trimmed),
        ))
    except Exception:
        result["marker"] = False

    if not result["metadata"] and not result["marker"]:
        print("  WARNING: could not persist the manifest — this timeline will "
              "not be recognised as an All Clips timeline on the next run.")
    return result


# ---------------------------------------------------------------------------
# Source Timeline Resolution
# ---------------------------------------------------------------------------

def resolve_source_timelines(media_pool, selection_method: str,
                             project_timelines: dict, progress_cb=None) -> list:
    """Resolve the user's Media Pool selection into [(name, Timeline), ...].

    Non-timeline items are reported and skipped. Returns an empty list when
    nothing usable was selected — the caller decides what to say about that.
    """

    def _p(stage: str, current: int = 0, total: int = 0) -> None:
        if progress_cb is not None:
            progress_cb(stage, current, total)

    # Get selected clips
    selected_clips = []
    if selection_method == "Current Bin":
        selected_bin = media_pool.GetCurrentFolder()
        if selected_bin:
            bin_clips = selected_bin.GetClipList()
            if bin_clips:
                selected_clips = bin_clips
    elif selection_method == "Current Selection":
        sel_clips = media_pool.GetSelectedClips()
        if sel_clips:
            selected_clips = sel_clips
    else:
        print("Unknown selection method.")
        return []

    if not selected_clips:
        print("No clips selected or found in bin. Please select clips or timelines.")
        return []

    print("Processing selected clips...")

    resolved = []
    total_selected = len(selected_clips)
    _p("Resolving timelines...", 0, total_selected)

    for sel_idx, media_pool_item in enumerate(selected_clips, start=1):
        if media_pool_item is None:
            continue

        # Check if it's a timeline
        clip_type = ""
        try:
            clip_type = media_pool_item.GetClipProperty("Type")
        except Exception:
            pass

        if clip_type != "Timeline":
            print(f"Skipping non-timeline item: {media_pool_item.GetName()}")
            _p(f"Skipping non-timeline item ({sel_idx}/{total_selected})",
               sel_idx, total_selected)
            continue

        timeline_name = media_pool_item.GetName()
        _p(f"Resolving: {timeline_name} ({sel_idx}/{total_selected})",
           sel_idx, total_selected)

        # Ask the item for its own timeline (Resolve 21.0.4+). The name lookup
        # silently picks the wrong timeline when two timelines in the project share
        # a name, which Resolve permits across different bins.
        curr_timeline = get_timeline_for_media_pool_item(media_pool_item)
        if curr_timeline is None:
            curr_timeline = project_timelines.get(timeline_name)
        if not curr_timeline:
            print(f"Timeline not found in project: {timeline_name}")
            continue

        resolved.append((timeline_name, curr_timeline))

    return resolved


# ---------------------------------------------------------------------------
# Clip Collection
# ---------------------------------------------------------------------------

def collect_desired_clips(
    source_timelines: list,
    *,
    connection_threshold: int,
    allow_disabled_clips: bool,
    mark_duplicates: bool,
    mark_retimed_clips: bool,
    use_xml_retime: bool,
    import_clip_names: bool,
    preserve_track_layout: bool,
    merge_by_source_file: bool,
    sorting_method: str,
    progress_cb=None,
) -> tuple:
    """Walk the source timelines and produce the desired clip set.

    Returns (all_clip_infos, timeline_block_markers, duplicate_set_count).

    This is the whole "what belongs on the output timeline" computation: track
    walk, retime and XML analysis, identity bucketing, overlap merging, the
    optional per-source-timeline layout, duplicate stamping and sorting. The
    create and update workflows both run it and differ only in what they do with
    the result.
    """

    def _p(stage: str, current: int = 0, total: int = 0) -> None:
        if progress_cb is not None:
            progress_cb(stage, current, total)

    # Collect clips from all selected timelines
    clips: dict[str, list[ClipInfo]] = {}
    total_selected = len(source_timelines)
    _p("Reading clips...", 0, total_selected)

    for sel_idx, (timeline_name, curr_timeline) in enumerate(source_timelines, start=1):
        print(f"Processing timeline: {timeline_name}")
        _p(f"Reading: {timeline_name} ({sel_idx}/{total_selected})",
           sel_idx, total_selected)

        # XML retime analysis for this timeline
        xml_lookup = {}
        xml_temp_path = None
        if use_xml_retime:
            xml_temp_path = export_timeline_to_xml(curr_timeline)
            if xml_temp_path:
                xml_lookup = build_xml_clip_lookup(xml_temp_path)
                print(f"  XML lookup built with {len(xml_lookup)} clip entries")

        # Walk video tracks
        num_tracks = curr_timeline.GetTrackCount("video")
        for track_idx in range(1, num_tracks + 1):
            track_items = curr_timeline.GetItemListInTrack("video", track_idx)
            if not track_items:
                print(f"No items in track {track_idx}")
                continue

            for item_index, track_item in enumerate(track_items):
                if track_item is None:
                    print(f"Track item #{item_index + 1} is nil")
                    continue

                # Get name
                item_name = "Unknown"
                try:
                    item_name = track_item.GetName()
                except Exception:
                    pass
                print(f"Processing item #{item_index + 1}: {item_name}")

                # Check enabled state
                is_enabled = True
                try:
                    is_enabled = track_item.GetClipEnabled()
                except Exception:
                    pass

                if not allow_disabled_clips and not is_enabled:
                    print(f"  Skipping disabled clip: {item_name}")
                    continue

                # Get media pool item and media ID
                try:
                    media_item = track_item.GetMediaPoolItem()
                except Exception:
                    media_item = None
                if not media_item:
                    print(f"  Could not retrieve media item for clip: {item_name}")
                    continue

                try:
                    media_id = media_item.GetMediaId()
                except Exception:
                    media_id = None
                if not media_id:
                    print(f"  Could not retrieve media ID for clip: {item_name}")
                    continue

                # Get raw source frames
                try:
                    raw_start = track_item.GetSourceStartFrame()
                    raw_end = track_item.GetSourceEndFrame()
                except Exception:
                    print(f"  Could not retrieve frame range for clip: {item_name}")
                    continue

                if raw_start is None or raw_end is None:
                    print(f"  Could not retrieve frame range for clip: {item_name}")
                    continue

                # Detect frame hold
                raw_source_duration = abs(raw_end - raw_start)
                is_frame_hold = raw_source_duration <= 1

                if is_frame_hold:
                    print(f"  FOUND FRAME HOLD CLIP: {item_name}")
                    print(f"  Source frames (raw): {raw_start} to {raw_end}")

                # Detect reversed
                is_reversed = raw_start > raw_end

                if is_reversed:
                    print(f"  FOUND REVERSED CLIP: {item_name}")
                    print(f"  Source frames (raw): {raw_start} to {raw_end}")

                # Normalize: always start <= end
                norm_start = min(raw_start, raw_end)
                norm_end = max(raw_start, raw_end)

                # GetSourceEndFrame() is inclusive in the Resolve API, and
                # AppendToTimeline.endFrame is inclusive too — pass norm_end through
                # unchanged. (is_frame_hold is still used downstream for marker text.)

                # Capture the raw API-reported range BEFORE any XML expansion
                # or merging — used by the Range Audit at the end of the run.
                api_source_start = norm_start
                api_source_end = norm_end

                # Retime detection
                is_retimed = False
                is_non_linear = False
                retime_pct = None
                xml_min = None
                xml_max = None

                if is_frame_hold:
                    is_retimed = True
                    retime_pct = 0.0
                    print(f"  Frame hold marked as retimed (0% speed)")
                elif mark_retimed_clips:
                    temp_clip = TimelineClipData(
                        clip=track_item,
                        media_pool_item=media_item,
                        name=item_name,
                        start_frame=track_item.GetStart(),
                        track_index=track_idx,
                    )
                    is_retimed, retime_pct, is_non_linear = is_clip_retimed(temp_clip)
                    if is_retimed:
                        print(f"  FOUND RETIMED CLIP: {item_name}")
                        if retime_pct is not None:
                            print(f"  Speed: {retime_pct}%")
                        if is_non_linear:
                            print(f"  Non-linear retime (speed curve) detected")

                # XML override if available
                if use_xml_retime and xml_lookup:
                    xml_data = match_xml_retime_to_clip(
                        item_name, norm_start, norm_end, xml_lookup,
                    )
                    if xml_data:
                        xml_min = xml_data["source_min"]
                        xml_max = xml_data["source_max"]
                        is_non_lin = xml_data["is_non_linear"]
                        print(f"  XML matched: keyframe range {xml_min}-{xml_max}, "
                              f"non_linear={is_non_lin}")
                        if is_non_lin:
                            is_non_linear = True
                            is_retimed = True
                            # For non-linear retimes (speed ramps) the Resolve API source
                            # in/out may not reflect the full frame range the ramp accesses.
                            # Only in this case do we expand using the XML-derived range.
                            if not is_frame_hold:
                                proposed_start = min(norm_start, xml_min)
                                proposed_end = max(norm_end, xml_max)
                                old_duration = max(1, norm_end - norm_start + 1)
                                new_duration = max(1, proposed_end - proposed_start + 1)
                                growth = new_duration / old_duration
                                if (proposed_start, proposed_end) == (norm_start, norm_end):
                                    pass  # XML didn't actually expand
                                elif growth > XML_EXPANSION_MAX_GROWTH:
                                    print(f"  XML EXPANSION SKIPPED: would grow "
                                          f"{old_duration}f -> {new_duration}f "
                                          f"({growth:.1f}x, cap is "
                                          f"{XML_EXPANSION_MAX_GROWTH:.0f}x). "
                                          f"Keeping API range {norm_start}-{norm_end}.")
                                else:
                                    added = new_duration - old_duration
                                    print(f"  XML EXPANDED range: "
                                          f"{norm_start}-{norm_end} ({old_duration}f) -> "
                                          f"{proposed_start}-{proposed_end} "
                                          f"({new_duration}f, +{added}f, {growth:.2f}x)")
                                    norm_start = proposed_start
                                    norm_end = proposed_end

                # Capture source clip name + color version names if option is on
                source_clip_name: Optional[str] = None
                version_names_list: list = []
                current_version_name: Optional[str] = None
                if import_clip_names:
                    try:
                        source_clip_name = track_item.GetName()
                    except Exception:
                        pass
                    try:
                        vlist = track_item.GetVersionNameList(0)
                        if vlist:
                            version_names_list = list(vlist)
                    except Exception:
                        pass
                    try:
                        cur_ver = track_item.GetCurrentVersion(0)
                        if isinstance(cur_ver, dict):
                            current_version_name = cur_ver.get("versionName")
                        elif isinstance(cur_ver, str):
                            current_version_name = cur_ver
                    except Exception:
                        pass

                # Get timeline endpoint for layout post-processing
                try:
                    track_item_endpoint = track_item.GetEnd()
                except Exception:
                    track_item_endpoint = track_item.GetStart() + 1

                # Create ClipInfo
                clip_info = ClipInfo(
                    media_pool_item=media_item,
                    start_frame=norm_start,
                    end_frame=norm_end,
                    timeline_inpoint=track_item.GetStart(),
                    timeline_endpoint=track_item_endpoint,
                    source_track_index=track_idx,
                    source_timeline_name=timeline_name,
                    is_reversed=is_reversed,
                    is_retimed=is_retimed,
                    is_frame_hold=is_frame_hold,
                    is_non_linear_retime=is_non_linear,
                    retime_percentage=retime_pct,
                    xml_source_min=xml_min,
                    xml_source_max=xml_max,
                    source_name=source_clip_name,
                    version_names=version_names_list,
                    current_version_name=current_version_name,
                    api_source_start=api_source_start,
                    api_source_end=api_source_end,
                )
                # Bucket key: see clip_identity_key(). When preserving track layout
                # we also include the track index to prevent cross-track merging.
                identity_key = clip_identity_key(media_item, merge_by_source_file) or media_id
                clips_key = f"{identity_key}_T{track_idx}" if preserve_track_layout else identity_key
                clips.setdefault(clips_key, []).append(clip_info)
                print(f"  Added clip to processing list: {item_name} "
                      f"(Start: {norm_start}, End: {norm_end}, Track: {track_idx})")

        # Clean up XML temp file
        if xml_temp_path:
            try:
                os.unlink(xml_temp_path)
                print(f"  Cleaned up temp XML: {xml_temp_path}")
            except OSError:
                pass

    if not clips:
        print("No valid clips found to process.")
        return [], [], 0

    # Cross-bucket scan: files appearing under multiple bucket keys. With
    # merge-by-file-path ON this should be empty (or only flag clips with
    # missing File Path); with it OFF this is the classic "two MediaPoolItems,
    # one source file" warning that explains unmerged subset/superset clips.
    path_to_buckets: dict[str, list[tuple[str, ClipInfo]]] = {}
    for bucket_key, bucket_clips in clips.items():
        for ci in bucket_clips:
            try:
                fp = ci.media_pool_item.GetClipProperty("File Path") or ""
            except Exception:
                fp = ""
            if not fp:
                continue
            path_to_buckets.setdefault(fp, []).append((bucket_key, ci))

    cross_bucket = []
    for fp, entries in path_to_buckets.items():
        distinct_keys = {bk for bk, _ in entries}
        if len(distinct_keys) > 1:
            cross_bucket.append((fp, entries, distinct_keys))

    if cross_bucket and not merge_by_source_file:
        print("")
        print(f"=== Pre-Merge Warning: {len(cross_bucket)} source file(s) "
              f"appear under multiple MediaPoolItems ===")
        print("(These won't merge across buckets — different MediaIds, often "
              "sub-clip vs full clip or the same file imported twice. Enable "
              "'Merge clips by source file path' to merge them.)")
        for fp, entries, distinct_keys in cross_bucket:
            print(f"  File: {fp}")
            print(f"  -> {len(distinct_keys)} distinct bucket(s):")
            for bk, ci in entries:
                try:
                    mid = ci.media_pool_item.GetMediaId() or "?"
                except Exception:
                    mid = "?"
                print(f"       bucket='{bk}'  MediaPoolItem='{ci.media_pool_item.GetName()}'  "
                      f"MediaId={mid}  range={ci.start_frame}-{ci.end_frame}  "
                      f"(from '{ci.source_timeline_name}')")
        print("")
    elif cross_bucket and merge_by_source_file:
        # Should be rare — only files with missing File Path can land here.
        print(f"NOTE: {len(cross_bucket)} file(s) still in multiple buckets despite "
              f"merge-by-file-path being on (likely missing File Path).")

    # Canonicalize MediaPoolItem per bucket: when merge-by-file-path is on, a
    # single bucket may contain clips from multiple MediaPoolItems pointing at
    # the same file. Pick the one with the widest source range (most Frames)
    # as the canonical item for the merged ClipInfo so AppendToTimeline can
    # place the merged range without hitting sub-clip range restrictions.
    if merge_by_source_file:
        def _item_frames(item) -> int:
            try:
                val = item.GetClipProperty("Frames")
                return int(val) if val else 0
            except (ValueError, TypeError, Exception):
                return 0

        canonicalized_buckets = 0
        for bucket_key, bucket_clips in clips.items():
            distinct_items: dict = {}
            for ci in bucket_clips:
                try:
                    mid = ci.media_pool_item.GetMediaId() or ""
                except Exception:
                    mid = ""
                if mid and mid not in distinct_items:
                    distinct_items[mid] = ci.media_pool_item
            if len(distinct_items) <= 1:
                continue
            canonical = max(distinct_items.values(), key=_item_frames)
            canonical_name = canonical.GetName()
            canonical_frames = _item_frames(canonical)
            try:
                canonical_path = canonical.GetClipProperty("File Path") or ""
            except Exception:
                canonical_path = ""
            print(f"INFO: Merging {len(distinct_items)} MediaPoolItems for "
                  f"'{canonical_path or canonical_name}'. Canonical: "
                  f"{canonical_name} ({canonical_frames}f)")
            for ci in bucket_clips:
                ci.media_pool_item = canonical
            canonicalized_buckets += 1
        if canonicalized_buckets:
            print(f"=== File-Path Merge: canonicalized {canonicalized_buckets} "
                  f"bucket(s) across multiple MediaPoolItems ===")
            print("")

    # Merge overlapping clips for each media ID
    _p("Merging overlapping ranges...", 0, 0)
    for media_id in clips:
        clips[media_id] = merge_all_overlapping(clips[media_id], connection_threshold)

    print("Merged Clips — creating flat list for sorting...")

    # Flatten into sorted list
    all_clip_infos: list[ClipInfo] = []
    for clip_infos in clips.values():
        all_clip_infos.extend(clip_infos)

    # Track-layout post-processing: lay each source timeline as a contiguous block
    timeline_markers: list[dict] = []
    if preserve_track_layout:
        # Preserve order in which source timelines were first encountered
        timeline_order: list[str] = []
        seen_tl: set = set()
        for ci in all_clip_infos:
            tname = ci.source_timeline_name or "__unknown__"
            if tname not in seen_tl:
                timeline_order.append(tname)
                seen_tl.add(tname)

        # Group clips by source timeline
        by_timeline: dict[str, list[ClipInfo]] = {tname: [] for tname in timeline_order}
        for ci in all_clip_infos:
            tname = ci.source_timeline_name or "__unknown__"
            by_timeline[tname].append(ci)

        rebuilt: list[ClipInfo] = []
        used_media_ids: set = set()
        output_cursor = 0

        for tl_idx, tname in enumerate(timeline_order):
            group = by_timeline[tname]
            if not group:
                continue

            # a) subtract this timeline's own minimum inpoint
            min_ip = min(ci.timeline_inpoint for ci in group)
            for ci in group:
                ci.timeline_inpoint -= min_ip
                ci.timeline_endpoint = (ci.timeline_endpoint or (ci.timeline_inpoint + 1)) - min_ip

            # b) re-merge by (media_id, track) within this group
            regroup: dict[str, list[ClipInfo]] = {}
            for ci in group:
                key = f"{ci.media_pool_item.GetMediaId()}_T{ci.source_track_index}"
                regroup.setdefault(key, []).append(ci)
            merged_group: list[ClipInfo] = []
            for subgroup in regroup.values():
                merged_group.extend(merge_all_overlapping(subgroup, connection_threshold))

            # c) filter out media already placed by an earlier timeline
            unique_group: list[ClipInfo] = []
            for ci in merged_group:
                mid = ci.media_pool_item.GetMediaId()
                if mid not in used_media_ids:
                    unique_group.append(ci)
                else:
                    print(f"  Skipping '{ci.media_pool_item.GetName()}' "
                          f"(already covered by earlier timeline)")
            for ci in merged_group:
                used_media_ids.add(ci.media_pool_item.GetMediaId())

            # Record block marker regardless of unique-clip count
            color = TIMELINE_BLOCK_MARKER_COLORS[tl_idx % len(TIMELINE_BLOCK_MARKER_COLORS)]
            timeline_markers.append({
                "frame": output_cursor,
                "name": tname,
                "color": color,
            })

            if not unique_group:
                print(f"Timeline '{tname}': no unique clips — marker only at frame {output_cursor}")
                output_cursor += 1
                continue

            block_end = max(
                (ci.timeline_endpoint or (ci.timeline_inpoint + 1)) for ci in unique_group
            )

            for ci in unique_group:
                ci.timeline_inpoint += output_cursor
                ci.timeline_endpoint = (ci.timeline_endpoint or (ci.timeline_inpoint + 1)) + output_cursor
                rebuilt.append(ci)

            output_cursor += block_end + INTER_TIMELINE_GAP
            print(f"Timeline '{tname}': {len(unique_group)} unique clips, "
                  f"block_end={block_end}, next_cursor={output_cursor}")

        all_clip_infos = rebuilt
        print(f"After per-timeline processing: {len(all_clip_infos)} unique clip entries")

    # Detect duplicate sets on the merged ClipInfo list (before timeline creation)
    if mark_duplicates:
        _p("Detecting duplicates...", 0, 0)
        dup_set_count = find_duplicate_groups(all_clip_infos)
        print(f"Detected {dup_set_count} duplicate set(s) across {len(all_clip_infos)} clips")
    else:
        dup_set_count = 0

    # Apply sorting
    if preserve_track_layout and sorting_method != "None":
        print(f"Preserve Source Track Layout is on — overriding sort '{sorting_method}' to 'None'")
        sorting_method = "None"

    print(f"Sorting using method: {sorting_method}")
    sort_fn = SORT_METHODS.get(sorting_method)
    if sort_fn:
        sort_fn(all_clip_infos)
    else:
        print(f"Unknown sorting method '{sorting_method}', using default (Source Name)")
        sort_by_source_name(all_clip_infos)

    return all_clip_infos, timeline_markers, dup_set_count


# ---------------------------------------------------------------------------
# Append Helper
# ---------------------------------------------------------------------------

def append_clip_with_slip_compensation(
    media_pool,
    timeline,
    clip_info: ClipInfo,
    *,
    video_only: bool,
    track_index: Optional[int] = None,
    record_frame: Optional[int] = None,
    timeline_fps: Optional[float] = None,
    allow_widening: bool = True,
) -> tuple:
    """Append one ClipInfo, compensating for AppendToTimeline source-frame slip.

    `timeline` must already be the project's current timeline — AppendToTimeline
    always targets the current one.

    Resolve sometimes places clips with their source in/out shifted by ±1 frame
    (codec/internal anchor quirks). We re-query the placed clip; if it differs
    from the target, delete it and retry with startFrame/endFrame adjusted by the
    inverse of the observed slip. Capped at SLIP_RETRY_LIMIT retries; if still
    off, fall back to widening the request by ±1 so the target range is fully
    contained in the placed range (extra handle frames are acceptable, missing
    frames are not). Pass allow_widening=False when the clip is going into a
    tight gap where that extra frame has nowhere to go.

    Returns (item, status, placed_source_start, placed_source_end). status is one
    of "exact", "corrected", "widened", "unfixed", "unknown" or "error";
    "unknown" covers an append that raised nothing but gave back no single
    usable item to verify.
    """
    mpi = clip_info.media_pool_item
    clip_name = mpi.GetName()

    # Quiet FPS-mismatch check (loud warning only when triggered).
    try:
        _clip_fps_raw = mpi.GetClipProperty("FPS")
        _clip_fps_val = float(_clip_fps_raw) if _clip_fps_raw not in (None, "") else None
    except (TypeError, ValueError, Exception):
        _clip_fps_val = None
    if (timeline_fps is not None and _clip_fps_val is not None
            and abs(timeline_fps - _clip_fps_val) > 0.01):
        print(
            f"  WARN: FPS mismatch on '{clip_name}' "
            f"(clip={_clip_fps_val}, timeline={timeline_fps})"
        )

    api_dict = sanitize_for_api(clip_info)
    # Audio filtering: mediaType=1 is the documented way to import video only and
    # works across all Resolve versions. importVideo/importAudio are kept as a
    # belt-and-suspenders hint for newer API builds.
    if video_only:
        api_dict["mediaType"] = 1  # 1 = video, 2 = audio
    api_dict["importVideo"] = True
    api_dict["importAudio"] = not video_only
    if track_index is not None:
        api_dict["trackIndex"] = track_index
    if record_frame is not None:
        api_dict["recordFrame"] = record_frame

    target_start = api_dict["startFrame"]
    target_end = api_dict["endFrame"]

    try:
        placed_items = media_pool.AppendToTimeline([api_dict])
    except Exception:
        print(f"  ERROR: Failed to add clip: {clip_name}")
        print(f"  Frame range attempted: {api_dict['startFrame']} to {api_dict['endFrame']}")
        return None, "error", None, None

    if record_frame is not None:
        print(f"  Placed on track {track_index} at frame {record_frame}")

    last_item = None
    retry_count = 0
    while placed_items and retry_count <= SLIP_RETRY_LIMIT:
        if len(placed_items) != 1:
            break  # unexpected multi-item return; don't try to fix
        placed = placed_items[0]
        last_item = placed
        try:
            actual_start = placed.GetSourceStartFrame()
            actual_end = placed.GetSourceEndFrame()
        except Exception:
            actual_start = None
            actual_end = None
        if actual_start is None or actual_end is None:
            break
        start_delta = actual_start - target_start
        end_delta = actual_end - target_end
        if start_delta == 0 and end_delta == 0:
            return (placed, "corrected" if retry_count > 0 else "exact",
                    actual_start, actual_end)
        if retry_count >= SLIP_RETRY_LIMIT:
            if not allow_widening:
                print(
                    f"  WARN: '{clip_name}' slip unfixable (target "
                    f"{target_start}-{target_end}, placed "
                    f"{actual_start}-{actual_end}); no room to widen here"
                )
                return placed, "unfixed", actual_start, actual_end
            # Widening fallback — extra frames are acceptable.
            try:
                timeline.DeleteClips([placed], False)
            except Exception:
                print(
                    f"  ERROR: '{clip_name}' slip unfixable and DeleteClips "
                    f"failed before widening; clip left at "
                    f"{actual_start}-{actual_end} "
                    f"(target {target_start}-{target_end})"
                )
                return placed, "unfixed", actual_start, actual_end
            wide_start = max(0, target_start - 1)
            wide_end = target_end + 1
            try:
                _media_end_raw = mpi.GetClipProperty("End")
                _media_end = int(_media_end_raw) if _media_end_raw not in (None, "") else None
            except (TypeError, ValueError, Exception):
                _media_end = None
            if _media_end is not None:
                wide_end = min(wide_end, _media_end)
            api_dict["startFrame"] = wide_start
            api_dict["endFrame"] = wide_end
            try:
                wide_items = media_pool.AppendToTimeline([api_dict])
            except Exception:
                print(
                    f"  ERROR: '{clip_name}' widening append raised; clip is "
                    f"no longer on the timeline (target "
                    f"{target_start}-{target_end})"
                )
                return None, "unfixed", None, None
            covers = False
            w_start = w_end = None
            wide_item = None
            if wide_items and len(wide_items) == 1:
                wide_item = wide_items[0]
                try:
                    w_start = wide_item.GetSourceStartFrame()
                    w_end = wide_item.GetSourceEndFrame()
                except Exception:
                    pass
                if w_start is not None and w_end is not None:
                    covers = (w_start <= target_start and w_end >= target_end)
            if covers:
                print(
                    f"  WARN: '{clip_name}' slip unfixable "
                    f"(target {target_start}-{target_end}); "
                    f"widened to {w_start}-{w_end} — "
                    f"target frames preserved with handles"
                )
                return wide_item, "widened", w_start, w_end
            print(
                f"  ERROR: '{clip_name}' slip unfixable; "
                f"widened to {w_start}-{w_end} but target "
                f"{target_start}-{target_end} NOT covered "
                f"(frames missing)"
            )
            return wide_item, "unfixed", w_start, w_end
        # Silent retry with inverse-of-observed-slip compensation.
        try:
            timeline.DeleteClips([placed], False)
        except Exception:
            print(
                f"  ERROR: '{clip_name}' DeleteClips failed during "
                f"retry; leaving slipped clip at "
                f"{actual_start}-{actual_end} "
                f"(target {target_start}-{target_end})"
            )
            return placed, "unfixed", actual_start, actual_end
        api_dict["startFrame"] = target_start - start_delta
        api_dict["endFrame"] = target_end - end_delta
        try:
            placed_items = media_pool.AppendToTimeline([api_dict])
        except Exception:
            print(
                f"  ERROR: '{clip_name}' retry append raised; clip is no "
                f"longer on the timeline (target {target_start}-{target_end})"
            )
            return None, "unfixed", None, None
        last_item = None
        retry_count += 1

    return last_item, "unknown", None, None


# ---------------------------------------------------------------------------
# Marker Text
# ---------------------------------------------------------------------------

def build_duplicate_marker_text(clip_info: ClipInfo) -> tuple:
    """(color, name, note) for a duplicate-set marker."""
    set_idx = clip_info.duplicate_set_index
    color = DUPLICATE_MARKER_COLORS[set_idx % len(DUPLICATE_MARKER_COLORS)]
    marker_text = (f"Dup Set #{set_idx + 1} "
                   f"({clip_info.duplicate_position + 1}/{clip_info.duplicate_set_size})")
    return color, marker_text, "Duplicate clip detected"


def build_retime_marker_text(clip_info: ClipInfo) -> tuple:
    """(name, note) for a retime marker. The colour is always Red."""
    if clip_info.is_frame_hold:
        marker_text = "Frame Hold"
        marker_note = ("Source clip used a frame hold (freeze frame). "
                       "Manual check recommended.")
    elif clip_info.is_non_linear_retime:
        marker_text = "Non-Linear Retime"
        marker_note = ("Speed curve/ramp detected. Source range may not "
                       "cover all frames used. Manual check recommended.")
        if clip_info.is_reversed:
            marker_note += " (originally reversed)"
    else:
        speed_value = clip_info.retime_percentage
        speed_str = f"{speed_value:.1f}" if speed_value is not None else "Unknown"
        marker_text = "Retimed Clip"
        marker_note = f"Manual check recommended. Speed: {speed_str}%"
        if clip_info.is_reversed:
            marker_note += " (originally reversed)"
    return marker_text, marker_note


# ---------------------------------------------------------------------------
# Update Diff
# ---------------------------------------------------------------------------

def read_accepted_range(markers: dict) -> tuple:
    """(accepted_start, accepted_end) from a clip's own update marker.

    Returns (None, None) when the clip has never been through an update. If more
    than one update marker survives, the highest run number wins.
    """
    best_run = None
    best = (None, None)
    for frame in sorted(markers):
        info = markers[frame] or {}
        custom = info.get("customData") or ""
        if not isinstance(custom, str) or not custom.startswith(UPDATE_MARKER_PREFIX):
            continue
        try:
            data = json.loads(custom[len(UPDATE_MARKER_PREFIX):])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        start, end = data.get("ds"), data.get("de")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        run = data.get("run")
        run = run if isinstance(run, int) else -1
        if best_run is None or run >= best_run:
            best_run = run
            best = (start, end)
    return best


def diff_ranges(desired_ranges: list, placed_ranges: list) -> tuple:
    """Greedy maximum-overlap matching between two lists of inclusive ranges.

    Returns (pairs, unmatched_desired, unmatched_placed) where pairs is
    [(desired_index, placed_index), ...].

    Merging legitimately leaves several disjoint ranges for one source file, so
    both sides are lists. Pairing them by index breaks the moment one range is
    deleted or a new one lands in the middle, hence the overlap match. Ties are
    broken by total endpoint distance and then by index, so the result is
    deterministic for a given input.
    """
    candidates = []
    for di, (d_start, d_end) in enumerate(desired_ranges):
        for pi, (p_start, p_end) in enumerate(placed_ranges):
            overlap = min(d_end, p_end) - max(d_start, p_start) + 1
            if overlap <= 0:
                continue
            distance = abs(d_start - p_start) + abs(d_end - p_end)
            candidates.append((-overlap, distance, di, pi))
    candidates.sort()

    pairs = []
    used_desired = set()
    used_placed = set()
    for _overlap, _distance, di, pi in candidates:
        if di in used_desired or pi in used_placed:
            continue
        used_desired.add(di)
        used_placed.add(pi)
        pairs.append((di, pi))
    pairs.sort()

    unmatched_desired = [i for i in range(len(desired_ranges)) if i not in used_desired]
    unmatched_placed = [i for i in range(len(placed_ranges)) if i not in used_placed]
    return pairs, unmatched_desired, unmatched_placed


def classify_change(desired: tuple, placed: tuple, accepted=None,
                    tolerance: int = UPDATE_CHANGE_TOLERANCE) -> str:
    """'unchanged' | 'extended' | 'shortened' | 'both' for one matched pair.

    Both ends are compared with abs(): the append helper's widening fallback can
    legitimately place a clip a frame WIDER than requested, and a signed test
    would read every widened clip as shortened forever and rebuild it on every
    run.

    `accepted` is the desired range this clip was last reconciled against. When
    the current desired range still matches it, the clip is unchanged no matter
    where it actually landed — that is the escape hatch for a target that is
    physically unreachable (media clamp, unfixable slip, sub-clip range limit),
    which would otherwise be retried forever.
    """
    desired_start, desired_end = desired
    if accepted is not None:
        accepted_start, accepted_end = accepted
        if (accepted_start is not None and accepted_end is not None
                and abs(desired_start - accepted_start) <= tolerance
                and abs(desired_end - accepted_end) <= tolerance):
            return "unchanged"

    placed_start, placed_end = placed
    head_delta = desired_start - placed_start
    tail_delta = desired_end - placed_end
    head_changed = abs(head_delta) > tolerance
    tail_changed = abs(tail_delta) > tolerance

    if not head_changed and not tail_changed:
        return "unchanged"

    grows = (head_changed and head_delta < 0) or (tail_changed and tail_delta > 0)
    shrinks = (head_changed and head_delta > 0) or (tail_changed and tail_delta < 0)
    if grows and shrinks:
        return "both"
    return "extended" if grows else "shortened"


def plan_rebuild_placement(record_start: int, old_src: tuple,
                           new_src: tuple) -> tuple:
    """(new_record, new_duration, head_need, tail_need) for a rebuilt clip.

    Anchored on the source frame, not the record frame: every source frame the
    clip keeps stays at the same timeline position. A head trim therefore opens
    the gap at the head instead of sliding the whole clip left, so anything the
    user lined up against it on another track stays lined up.
    """
    old_start, old_end = old_src
    new_start, new_end = new_src
    old_duration = max(1, old_end - old_start + 1)
    new_duration = max(1, new_end - new_start + 1)
    new_record = record_start + (new_start - old_start)
    head_need = max(0, record_start - new_record)
    tail_need = max(0, (new_record + new_duration) - (record_start + old_duration))
    return new_record, new_duration, head_need, tail_need


def compute_free_space(bounds: list, index: int, timeline_start: int = 0) -> tuple:
    """(free_before, free_after) around bounds[index] on a single track.

    `bounds` is [(start, end_exclusive), ...] for one track, sorted by start.
    The last clip on a track has unbounded room after it.
    """
    start, end = bounds[index]
    if index == 0:
        free_before = max(0, start - timeline_start)
    else:
        free_before = max(0, start - bounds[index - 1][1])
    if index >= len(bounds) - 1:
        free_after = FREE_SPACE_UNBOUNDED
    else:
        free_after = max(0, bounds[index + 1][0] - end)
    return free_before, free_after


def fits_in_place(free_before: int, free_after: int, head_need: int,
                  tail_need: int, slack: int = 0) -> bool:
    """Whether a rebuilt clip's growth fits the gaps around it.

    Head and tail are independent: a clip can grow left into a gap while having
    no room at all on the right. Call once with slack=0 to ask "does it fit",
    then again with slack=REBUILD_FIT_SLACK to ask "and may it also widen".
    Widening pushes out both ends at once, so it needs a spare frame on each
    side even when only one end grew.
    """
    return (head_need + slack <= free_before
            and tail_need + slack <= free_after)


def group_desired_by_identity(clip_infos: list, merge_by_source_file: bool) -> dict:
    """identity -> [ClipInfo] sorted by source start, for the diff."""
    grouped: dict = {}
    for clip_info in clip_infos:
        identity = clip_identity_key(clip_info.media_pool_item, merge_by_source_file)
        if not identity:
            continue
        grouped.setdefault(identity, []).append(clip_info)
    for entries in grouped.values():
        entries.sort(key=lambda ci: ci.start_frame)
    return grouped


def scan_timeline_placements(timeline, merge_by_source_file: bool) -> tuple:
    """(placed_by_identity, unmanaged_items) for an existing All Clips timeline.

    The timeline is the authority on what is currently placed — nothing is read
    from the manifest here, so any reordering, retracking or deletion the user
    did between runs is simply what we see.

    Items with no MediaPoolItem (titles, generators, compound and Fusion clips)
    cannot be identified as a shot and are returned separately so the caller can
    report them. They are never touched.
    """
    placed_by_identity: dict = {}
    unmanaged = []

    try:
        track_count = timeline.GetTrackCount("video")
    except Exception:
        track_count = 0

    for track_index in range(1, (track_count or 0) + 1):
        try:
            items = timeline.GetItemListInTrack("video", track_index)
        except Exception:
            items = None
        if not items:
            continue

        for item in items:
            if item is None:
                continue
            try:
                media_pool_item = item.GetMediaPoolItem()
            except Exception:
                media_pool_item = None
            if not media_pool_item:
                unmanaged.append(item)
                continue

            identity = clip_identity_key(media_pool_item, merge_by_source_file)
            if not identity:
                unmanaged.append(item)
                continue

            try:
                raw_start = item.GetSourceStartFrame()
                raw_end = item.GetSourceEndFrame()
                record_start = item.GetStart()
                duration = item.GetDuration()
            except Exception:
                unmanaged.append(item)
                continue
            if None in (raw_start, raw_end, record_start, duration):
                unmanaged.append(item)
                continue

            # A clip the user reversed by hand would otherwise scan backwards.
            source_start = min(raw_start, raw_end)
            source_end = max(raw_start, raw_end)

            accepted_start, accepted_end = read_accepted_range(normalised_markers(item))

            linked_count = 0
            linked_getter = getattr(item, "GetLinkedItems", None)
            if callable(linked_getter):
                try:
                    linked = linked_getter() or []
                    # An item lists itself among its linked items on some builds.
                    linked_count = max(0, len(linked) - 1)
                except Exception:
                    linked_count = 0

            name = ""
            try:
                name = item.GetName() or ""
            except Exception:
                name = ""

            placed_by_identity.setdefault(identity, []).append(PlacedClip(
                item=item,
                identity=identity,
                source_start=source_start,
                source_end=source_end,
                record_start=record_start,
                record_end=record_start + duration,
                track_index=track_index,
                name=name,
                accepted_start=accepted_start,
                accepted_end=accepted_end,
                linked_item_count=linked_count,
            ))

    for entries in placed_by_identity.values():
        entries.sort(key=lambda pc: pc.source_start)

    return placed_by_identity, unmanaged


def diff_desired_vs_placed(desired_by_identity: dict, placed_by_identity: dict,
                           tolerance: int = UPDATE_CHANGE_TOLERANCE) -> list:
    """Reconcile desired against placed. Returns [ClipDiff] in identity order."""
    diffs = []
    identities = sorted(set(desired_by_identity) | set(placed_by_identity))

    for identity in identities:
        desired = desired_by_identity.get(identity) or []
        placed = placed_by_identity.get(identity) or []
        desired_ranges = [(ci.start_frame, ci.end_frame) for ci in desired]
        placed_ranges = [(pc.source_start, pc.source_end) for pc in placed]

        pairs, only_desired, only_placed = diff_ranges(desired_ranges, placed_ranges)

        for desired_index, placed_index in pairs:
            clip_info = desired[desired_index]
            placed_clip = placed[placed_index]
            kind = classify_change(
                desired_ranges[desired_index], placed_ranges[placed_index],
                (placed_clip.accepted_start, placed_clip.accepted_end), tolerance,
            )
            diffs.append(ClipDiff(
                identity=identity,
                kind=kind,
                clip_info=clip_info,
                placed=placed_clip,
                head_delta=clip_info.start_frame - placed_clip.source_start,
                tail_delta=clip_info.end_frame - placed_clip.source_end,
            ))

        for desired_index in only_desired:
            diffs.append(ClipDiff(identity=identity, kind="new",
                                  clip_info=desired[desired_index]))

        for placed_index in only_placed:
            diffs.append(ClipDiff(identity=identity, kind="dropped",
                                  placed=placed[placed_index]))

    return diffs


def summarise_diffs(diffs: list) -> dict:
    """Counts per change kind, for logging and the manifest run record."""
    counts = {"unchanged": 0, "extended": 0, "shortened": 0, "both": 0,
              "new": 0, "dropped": 0}
    for diff in diffs:
        counts[diff.kind] = counts.get(diff.kind, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Main Workflow
# ---------------------------------------------------------------------------

def run_workflow(
    dst_timeline_name: str,
    selection_method: str,
    sorting_method: str,
    connection_threshold: int,
    allow_disabled_clips: bool,
    video_only: bool,
    mark_duplicates: bool,
    mark_retimed_clips: bool,
    use_xml_retime: bool,
    import_clip_names: bool,
    preserve_track_layout: bool,
    merge_by_source_file: bool = True,
    progress: Optional["ProgressUI"] = None,
) -> None:
    """Execute the complete timeline generation workflow."""

    def _p(stage: str, current: int = 0, total: int = 0) -> None:
        if progress is not None:
            progress.set(stage, current, total)

    # Get project context — resolve is a pre-injected global in Resolve's scripting environment
    project_manager = resolve.GetProjectManager()  # noqa: F821
    project = project_manager.GetCurrentProject()
    media_pool = project.GetMediaPool()
    num_timelines = project.GetTimelineCount()

    # Build timeline name lookup. Kept eager because it has a second consumer —
    # destination-timeline name deduplication further down — which runs on every
    # invocation. As a way of resolving a source timeline it is superseded by
    # get_timeline_for_media_pool_item() and only used as the pre-21.0.4
    # fallback. (The Lua variant builds this lazily; it has no second consumer.)
    project_timelines = {}
    for timeline_idx in range(1, num_timelines + 1):
        tl = project.GetTimelineByIndex(timeline_idx)
        if tl is not None:
            project_timelines[tl.GetName()] = tl

    source_timelines = resolve_source_timelines(
        media_pool, selection_method, project_timelines, progress_cb=_p,
    )
    if not source_timelines:
        return

    all_clip_infos, timeline_markers, dup_set_count = collect_desired_clips(
        source_timelines,
        connection_threshold=connection_threshold,
        allow_disabled_clips=allow_disabled_clips,
        mark_duplicates=mark_duplicates,
        mark_retimed_clips=mark_retimed_clips,
        use_xml_retime=use_xml_retime,
        import_clip_names=import_clip_names,
        preserve_track_layout=preserve_track_layout,
        merge_by_source_file=merge_by_source_file,
        sorting_method=sorting_method,
        progress_cb=_p,
    )
    if not all_clip_infos:
        return

    # Create new timeline — dedupe name against existing project timelines.
    # If "All_Sources" exists, try "All_Sources_01", "All_Sources_02", …
    if dst_timeline_name in project_timelines:
        counter = 1
        while True:
            candidate = f"{dst_timeline_name}_{counter:02d}"
            if candidate not in project_timelines:
                print(f"Timeline name '{dst_timeline_name}' already exists, "
                      f"using '{candidate}' instead")
                dst_timeline_name = candidate
                break
            counter += 1

    print("Adding all clips to new timeline...")
    new_timeline = media_pool.CreateEmptyTimeline(dst_timeline_name)
    assert project.SetCurrentTimeline(new_timeline), \
        "Couldn't set current timeline to the new timeline"

    # Capture timeline FPS so per-clip code can warn on FPS mismatches.
    _timeline_fps_str = ""
    try:
        _timeline_fps_str = new_timeline.GetSetting("timelineFrameRate") or ""
    except Exception:
        _timeline_fps_str = ""
    if not _timeline_fps_str:
        try:
            _timeline_fps_str = project.GetSetting("timelineFrameRate") or ""
        except Exception:
            _timeline_fps_str = ""
    try:
        _timeline_fps = float(_timeline_fps_str) if _timeline_fps_str else None
    except (TypeError, ValueError):
        _timeline_fps = None
    print(f"Timeline FPS: {_timeline_fps_str or '<unknown>'}")

    # Pre-create video tracks to cover the highest source_track_index
    if preserve_track_layout and all_clip_infos:
        max_track = max(ci.source_track_index for ci in all_clip_infos)
        cur = new_timeline.GetTrackCount("video")
        while cur < max_track:
            new_timeline.AddTrack("video")
            cur += 1
            print(f"Pre-created video track {cur}")

    # Add clips — single code path for all clip types
    append_success_count = 0
    append_error_count = 0
    # Slip-compensation telemetry (summarised at the end of the append phase).
    slip_corrected_count = 0  # clips that landed on target after retry
    slip_widened_count = 0    # clips that needed the ±1 widening fallback
    slip_unfixed_count = 0    # clips where even widening didn't cover target
    total_to_append = len(all_clip_infos)

    for i, clip_info in enumerate(all_clip_infos):
        clip_name = clip_info.media_pool_item.GetName()
        _p(f"Appending clip: {clip_name}", i + 1, total_to_append)
        print(f"Adding clip #{i + 1} to timeline:")
        print(f"  Clip name: {clip_name}")
        print(f"  Frame range: {clip_info.start_frame} to {clip_info.end_frame}")

        # Log original clip status
        status_parts = []
        if clip_info.is_reversed:
            status_parts.append("REVERSED")
        if clip_info.is_frame_hold:
            status_parts.append("FRAME HOLD")
        if clip_info.is_non_linear_retime:
            status_parts.append("NON-LINEAR RETIME")
        if clip_info.is_retimed:
            status_parts.append("RETIMED")
        if status_parts:
            print(f"  Original clip status: {', '.join(status_parts)}")
            print("  (Adding normalized forward-playing version)")

        _item, _status, _s, _e = append_clip_with_slip_compensation(
            media_pool, new_timeline, clip_info,
            video_only=video_only,
            track_index=clip_info.source_track_index if preserve_track_layout else None,
            record_frame=clip_info.timeline_inpoint if preserve_track_layout else None,
            timeline_fps=_timeline_fps,
        )
        if _status == "error":
            append_error_count += 1
        else:
            append_success_count += 1
        if _status == "corrected":
            slip_corrected_count += 1
        elif _status == "widened":
            slip_widened_count += 1
        elif _status == "unfixed":
            slip_unfixed_count += 1

    print(f"Clip addition summary: {append_success_count} succeeded, "
          f"{append_error_count} failed")
    if (slip_corrected_count or slip_widened_count or slip_unfixed_count):
        print(
            f"Slip compensation: {slip_corrected_count} corrected via retry, "
            f"{slip_widened_count} widened with handles, "
            f"{slip_unfixed_count} unfixable"
        )
    print(f"New timeline created: {dst_timeline_name}")

    # Post-processing: source-timeline ruler markers (preserve-layout mode)
    if preserve_track_layout and timeline_markers:
        print("Adding timeline block markers...")
        for m in timeline_markers:
            try:
                ok = new_timeline.AddMarker(
                    m["frame"], m["color"], m["name"],
                    f"Source timeline: {m['name']}", 1, "",
                )
            except Exception:
                ok = False
            if ok:
                print(f"  Marker '{m['name']}' at frame {m['frame']} ({m['color']})")
            else:
                print(f"  WARNING: failed to add marker '{m['name']}' at frame {m['frame']}")

    # Post-processing: mark duplicates (uses pre-computed metadata on ClipInfo)
    if mark_duplicates and dup_set_count > 0:
        print(f"Marking {dup_set_count} duplicate set(s) in the new timeline...")
        _p("Marking duplicates...", 0, 0)
        clip_list = get_all_timeline_clips(new_timeline)
        marker_count = 0
        success_count = 0
        total_clips = len(clip_list)

        for ci_idx, timeline_clip in enumerate(clip_list, start=1):
            _p(f"Marking duplicates: {timeline_clip.name}", ci_idx, total_clips)
            for clip_info in all_clip_infos:
                if clip_info.duplicate_set_index is None:
                    continue
                if (timeline_clip.media_pool_item.GetName() == clip_info.media_pool_item.GetName()
                        and timeline_clip.clip.GetSourceStartFrame() >= clip_info.start_frame - SLIP_TOLERANCE
                        and timeline_clip.clip.GetSourceEndFrame() <= clip_info.end_frame + SLIP_TOLERANCE):

                    color, marker_text, marker_note = build_duplicate_marker_text(clip_info)
                    source_start = timeline_clip.clip.GetSourceStartFrame()
                    clip_duration = timeline_clip.clip.GetDuration()
                    marker_position = source_start + math.floor(clip_duration * 0.25)

                    try:
                        success = timeline_clip.clip.AddMarker(
                            marker_position, color, marker_text,
                            marker_note, 1, "",
                        )
                    except Exception:
                        success = False

                    marker_count += 1
                    if success:
                        success_count += 1
                        print(f"  Added duplicate marker to: {timeline_clip.name}")
                    else:
                        print(f"  Failed to add duplicate marker to: {timeline_clip.name}")
                    break

        print(f"Successfully added {success_count} duplicate markers out of "
              f"{marker_count} attempts.")
    elif mark_duplicates:
        print("No duplicate clips were found.")

    # Post-processing: mark retimed clips
    if mark_retimed_clips:
        print("Marking retimed clips in the new timeline...")
        _p("Marking retimed clips...", 0, 0)
        clip_list = get_all_timeline_clips(new_timeline)
        marker_count = 0
        success_count = 0
        total_clips = len(clip_list)

        for ci_idx, timeline_clip in enumerate(clip_list, start=1):
            _p(f"Marking retimes: {timeline_clip.name}", ci_idx, total_clips)
            for clip_info in all_clip_infos:
                # Match by name and source frame range
                if (timeline_clip.media_pool_item.GetName() == clip_info.media_pool_item.GetName()
                        and timeline_clip.clip.GetSourceStartFrame() >= clip_info.start_frame - SLIP_TOLERANCE
                        and timeline_clip.clip.GetSourceEndFrame() <= clip_info.end_frame + SLIP_TOLERANCE):

                    if clip_info.is_retimed:
                        source_start = timeline_clip.clip.GetSourceStartFrame()
                        clip_duration = timeline_clip.clip.GetDuration()
                        marker_position = source_start + math.floor(clip_duration * 0.5)

                        # Distinguish marker types
                        marker_text, marker_note = build_retime_marker_text(clip_info)

                        try:
                            success = timeline_clip.clip.AddMarker(
                                marker_position, "Red", marker_text,
                                marker_note, 1, "",
                            )
                        except Exception:
                            success = False

                        marker_count += 1
                        if success:
                            success_count += 1
                            print(f"  Added retime marker to clip: {timeline_clip.name}")
                        else:
                            print(f"  Failed to add retime marker to clip: {timeline_clip.name}")

                        break  # Only mark first match

        print(f"Successfully added {success_count} retime markers out of "
              f"{marker_count} attempts.")

    # Post-processing: apply source clip names + color version names
    if import_clip_names:
        print("Applying source clip names and color version names to new timeline...")
        _p("Applying clip names...", 0, 0)
        clip_list = get_all_timeline_clips(new_timeline)
        name_apply_count = 0
        version_apply_count = 0
        total_clips = len(clip_list)

        for ci_idx, timeline_clip in enumerate(clip_list, start=1):
            _p(f"Applying names: {timeline_clip.name}", ci_idx, total_clips)
            for clip_info in all_clip_infos:
                if (timeline_clip.media_pool_item.GetName() == clip_info.media_pool_item.GetName()
                        and timeline_clip.clip.GetSourceStartFrame() >= clip_info.start_frame - SLIP_TOLERANCE
                        and timeline_clip.clip.GetSourceEndFrame() <= clip_info.end_frame + SLIP_TOLERANCE):

                    # Set clip name from source
                    if clip_info.source_name:
                        ok = False
                        try:
                            ok = timeline_clip.clip.SetName(clip_info.source_name)
                        except Exception:
                            pass
                        if ok:
                            print(f"  Renamed clip to: {clip_info.source_name}")
                            name_apply_count += 1
                        else:
                            print(f"  WARNING: SetName failed for: {timeline_clip.name}")

                    # Recreate color versions
                    vnames = clip_info.version_names or []
                    if vnames:
                        for vname in vnames:
                            try:
                                timeline_clip.clip.AddVersion(vname, 0)
                            except Exception:
                                pass
                        if clip_info.current_version_name:
                            set_ok = False
                            try:
                                set_ok = timeline_clip.clip.SetCurrentVersion(
                                    clip_info.current_version_name, 0,
                                )
                            except Exception:
                                pass
                            if set_ok:
                                print(f"  Set active version "
                                      f"'{clip_info.current_version_name}' on: "
                                      f"{timeline_clip.name}")
                            else:
                                print(f"  WARNING: SetCurrentVersion failed for: "
                                      f"{timeline_clip.name}")
                        version_apply_count += 1

                    break

        print(f"Clip names applied to {name_apply_count} clips.")
        print(f"Color version names applied to {version_apply_count} clips.")

    # Range Audit: flag any clip whose final source range on the new timeline
    # grew significantly versus the raw API-reported range. The only thing in
    # the pipeline that can cause growth past 1.0x is XML retime expansion of
    # a constituent — so this is the primary diagnostic when clips end up
    # longer than expected.
    audit_rows = []
    for ci in all_clip_infos:
        if ci.api_source_start is None or ci.api_source_end is None:
            continue
        api_dur = max(1, ci.api_source_end - ci.api_source_start + 1)
        final_dur = max(1, ci.end_frame - ci.start_frame + 1)
        if final_dur >= api_dur * RANGE_AUDIT_GROWTH_THRESHOLD:
            audit_rows.append((
                final_dur / api_dur,
                ci.media_pool_item.GetName(),
                ci.source_timeline_name,
                ci.api_source_start, ci.api_source_end, api_dur,
                ci.start_frame, ci.end_frame, final_dur,
                ci.is_non_linear_retime,
            ))

    if audit_rows:
        audit_rows.sort(reverse=True)
        print("")
        print(f"=== Range Audit: {len(audit_rows)} clip(s) grew "
              f">={RANGE_AUDIT_GROWTH_THRESHOLD:.1f}x vs raw API range ===")
        print("(Cause: XML retime expansion. Disable 'Use XML for Precise "
              "Retime Detection' to keep raw API ranges.)")
        for (growth, name, tl, api_s, api_e, api_d,
             fin_s, fin_e, fin_d, non_lin) in audit_rows:
            tag = " [non-linear]" if non_lin else ""
            print(f"  [{growth:5.1f}x]{tag} {name}  (from '{tl}')")
            print(f"           API: {api_s}-{api_e} ({api_d}f)  "
                  f"->  Final: {fin_s}-{fin_e} ({fin_d}f)")
        print("")
    elif use_xml_retime:
        print("Range Audit: no clips grew significantly beyond API range.")

    _p("Done!", 1, 1)
    print("Done!")


# ---------------------------------------------------------------------------
# Update Execution Helpers
# ---------------------------------------------------------------------------

def local_now_str() -> str:
    """Local wall clock as 2026-08-06 14:22, for human-readable changelogs."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def format_change_line(now_str: str, run_no: int, head_delta: int,
                       tail_delta: int, old_range: tuple,
                       new_range: tuple) -> str:
    """One changelog line. head_delta/tail_delta are desired minus placed.

    Reported from the clip's point of view: "head +12f" means twelve more frames
    at the head, which is a head_delta of -12.
    """
    parts = []
    head_grow = -head_delta
    if head_grow:
        parts.append(f"head {head_grow:+d}f")
    if tail_delta:
        parts.append(f"tail {tail_delta:+d}f")
    change = ", ".join(parts) if parts else "no range change"
    return CHANGELOG_SEPARATOR.join([
        now_str,
        f"run {run_no}",
        change,
        f"{old_range[0]}-{old_range[1]} -> {new_range[0]}-{new_range[1]}",
    ])


def merge_changelog(previous_note, new_line: str,
                    max_lines: int = CHANGELOG_MAX_LINES) -> str:
    """Prepend a change line to a marker note, keeping a bounded history.

    The previous note has to be read off the old marker before the clip is
    deleted — a rebuilt clip has no markers of its own until they are restored.
    Any earlier truncation count is folded in rather than lost.
    """
    earlier = 0
    kept = []
    for line in (previous_note or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(CHANGELOG_TRUNCATION_MARK):
            digits = "".join(ch for ch in stripped if ch.isdigit())
            if digits:
                earlier += int(digits)
            continue
        kept.append(stripped)

    room = max(0, max_lines - 1)
    lines = [new_line] + kept[:room]
    dropped = max(0, len(kept) - room) + earlier
    if dropped > 0:
        lines.append(f"{CHANGELOG_TRUNCATION_MARK}{dropped} earlier "
                     f"change{'s' if dropped != 1 else ''})")
    return "\n".join(lines)


def pick_marker_frame(source_start: int, duration: int, existing_frames,
                      fraction: float) -> int:
    """Frame for a clip marker at `fraction` through it, stepping off collisions.

    Resolve permits one marker per frame, and this file already puts markers at
    0.25 (duplicates) and 0.5 (retimes), so on a short clip the preferred frame
    is often taken. Returns -1 when every frame of the clip has a marker.
    """
    duration = max(1, duration)
    upper = source_start + duration - 1
    preferred = source_start + int(math.floor(duration * fraction))
    preferred = min(max(preferred, source_start), upper)
    return first_free_frame(existing_frames, preferred,
                            lower=source_start, upper=upper)


def format_update_track_name(run_no: int, date_str: str) -> str:
    """Track name for the clips a run adds, e.g. 'Update 3 - 2026-08-06'."""
    return f"Update {run_no} - {date_str}"[:60]


def build_update_customdata(run_no: int, kind: str, desired_start,
                            desired_end) -> str:
    """customData for a per-clip update marker.

    ds/de are the desired range the clip was reconciled against — the accepted
    range classify_change() reads back to stop an unreachable target being
    retried on every run.
    """
    payload = {"v": 1, "run": run_no, "kind": kind}
    if desired_start is not None and desired_end is not None:
        payload["ds"] = int(desired_start)
        payload["de"] = int(desired_end)
    return UPDATE_MARKER_PREFIX + json.dumps(payload, sort_keys=True,
                                             separators=(",", ":"))


def timeline_marker_offset(timeline, absolute_frame: int) -> int:
    """Timeline marker frames are offsets from GetStartFrame(), not absolutes.

    Isolated here so there is one place to correct if a live probe says
    otherwise — the docs say "timeline offset" but the existing preserve-layout
    pass writes block markers at raw record frames, which only agree when the
    timeline starts at zero.
    """
    start = 0
    getter = getattr(timeline, "GetStartFrame", None)
    if callable(getter):
        try:
            start = getter() or 0
        except Exception:
            start = 0
    return max(0, int(absolute_frame) - int(start))


def capture_item_state(item, track_index: int) -> ItemState:
    """Read back everything about a TimelineItem worth restoring after a rebuild."""
    state = ItemState(track_index=track_index)

    def _try(getter_name, default=None):
        getter = getattr(item, getter_name, None)
        if not callable(getter):
            return default
        try:
            value = getter()
        except Exception:
            return default
        return default if value is None else value

    state.name = _try("GetName")
    state.clip_color = _try("GetClipColor")
    state.flags = list(_try("GetFlagList", []) or [])
    state.enabled = _try("GetClipEnabled")
    state.markers = normalised_markers(item)
    state.fusion_comp_count = int(_try("GetFusionCompCount", 0) or 0)
    state.record_start = int(_try("GetStart", 0) or 0)
    state.duration = int(_try("GetDuration", 0) or 0)

    raw_start = _try("GetSourceStartFrame", 0) or 0
    raw_end = _try("GetSourceEndFrame", 0) or 0
    state.source_start = min(raw_start, raw_end)
    state.source_end = max(raw_start, raw_end)

    versions = getattr(item, "GetVersionNameList", None)
    if callable(versions):
        try:
            state.version_names = list(versions(0) or [])
        except Exception:
            state.version_names = []

    linked = getattr(item, "GetLinkedItems", None)
    if callable(linked):
        try:
            state.linked_item_count = max(0, len(linked() or []) - 1)
        except Exception:
            state.linked_item_count = 0

    return state


def restore_item_state(item, state: ItemState, new_source_start: int,
                       new_source_end: int,
                       skip_customdata_prefix: Optional[str] = None) -> dict:
    """Put a rebuilt clip's name, colour, flags and markers back.

    Markers whose frame falls outside the new source range are dropped rather
    than clamped: their picture is gone, so a marker at that frame would point
    at something else.
    """
    result = {"name": False, "color": False, "flags": 0, "markers": 0,
              "markers_dropped": 0, "enabled": False}

    if state.name:
        setter = getattr(item, "SetName", None)
        if callable(setter):
            try:
                result["name"] = bool(setter(state.name))
            except Exception:
                result["name"] = False

    if state.clip_color:
        setter = getattr(item, "SetClipColor", None)
        if callable(setter):
            try:
                result["color"] = bool(setter(state.clip_color))
            except Exception:
                result["color"] = False

    adder = getattr(item, "AddFlag", None)
    if callable(adder):
        for flag in state.flags:
            try:
                if adder(flag):
                    result["flags"] += 1
            except Exception:
                pass

    if state.enabled is False:
        setter = getattr(item, "SetClipEnabled", None)
        if callable(setter):
            try:
                result["enabled"] = bool(setter(False))
            except Exception:
                result["enabled"] = False

    marker_adder = getattr(item, "AddMarker", None)
    if callable(marker_adder):
        for frame in sorted(state.markers):
            info = state.markers[frame] or {}
            custom = info.get("customData") or ""
            if (skip_customdata_prefix and isinstance(custom, str)
                    and custom.startswith(skip_customdata_prefix)):
                continue
            if frame < new_source_start or frame > new_source_end:
                result["markers_dropped"] += 1
                continue
            try:
                ok = marker_adder(
                    frame, info.get("color") or "Blue", info.get("name") or "",
                    info.get("note") or "", int(info.get("duration") or 1),
                    custom if isinstance(custom, str) else "",
                )
            except Exception:
                ok = False
            if ok:
                result["markers"] += 1

    return result


def add_update_track(timeline, track_name: str) -> Optional[int]:
    """Append a video track and name it. Returns its index, or None on failure.

    Deliberately never passes {"index": n}: that INSERTS a track and renumbers
    every track above it, which would invalidate every track index held by the
    run in flight.
    """
    try:
        before = timeline.GetTrackCount("video")
    except Exception:
        return None
    try:
        if not timeline.AddTrack("video"):
            return None
    except Exception:
        return None
    try:
        after = timeline.GetTrackCount("video")
    except Exception:
        return None
    if after <= before:
        return None

    setter = getattr(timeline, "SetTrackName", None)
    if callable(setter):
        try:
            setter("video", after, track_name)
        except Exception:
            pass
    print(f"  Added video track {after} '{track_name}'")
    return after


def build_track_occupancy(timeline) -> dict:
    """track index -> sorted [[start, end_exclusive], ...] for every video item.

    Includes items the diff cannot identify (titles, generators, compound clips):
    they still occupy the track, so a rebuilt clip must not be planned on top of
    them. Maintained in memory for the rest of the run — we are the only writer,
    and re-querying per clip would be O(n^2) bridge calls.
    """
    occupancy: dict = {}
    try:
        track_count = timeline.GetTrackCount("video")
    except Exception:
        return occupancy

    for track_index in range(1, (track_count or 0) + 1):
        bounds = []
        try:
            items = timeline.GetItemListInTrack("video", track_index)
        except Exception:
            items = None
        for item in items or []:
            if item is None:
                continue
            try:
                start = item.GetStart()
                duration = item.GetDuration()
            except Exception:
                continue
            if start is None or duration is None:
                continue
            bounds.append([int(start), int(start) + int(duration)])
        bounds.sort()
        occupancy[track_index] = bounds
    return occupancy


def occupancy_index(bounds: list, record_start: int) -> int:
    """Index of the entry starting at `record_start`, or -1."""
    for index, (start, _end) in enumerate(bounds):
        if start == record_start:
            return index
    return -1


def insert_occupancy(bounds: list, start: int, end: int) -> None:
    """Insert [start, end) keeping the track's bounds sorted."""
    position = len(bounds)
    for index, (existing_start, _existing_end) in enumerate(bounds):
        if existing_start > start:
            position = index
            break
    bounds.insert(position, [int(start), int(end)])


def resolve_timelines_by_source_record(project, sources: list) -> tuple:
    """Resolve manifest source records to timelines. Returns (resolved, missing).

    Matches on GetUniqueId() first — it survives renames — and falls back to the
    recorded name, which is the only thing a human can act on when a uid stops
    resolving.
    """
    by_uid = {}
    by_name = {}
    try:
        count = project.GetTimelineCount()
    except Exception:
        count = 0
    for index in range(1, (count or 0) + 1):
        try:
            timeline = project.GetTimelineByIndex(index)
        except Exception:
            timeline = None
        if timeline is None:
            continue
        try:
            name = timeline.GetName()
        except Exception:
            continue
        uid = None
        getter = getattr(timeline, "GetUniqueId", None)
        if callable(getter):
            try:
                uid = getter()
            except Exception:
                uid = None
        if uid:
            by_uid[uid] = (name, timeline)
        by_name.setdefault(name, (name, timeline))

    resolved = []
    missing = []
    for source in sources or []:
        uid = source.get("uid")
        name = source.get("name")
        hit = by_uid.get(uid) if uid else None
        if hit is None and name:
            hit = by_name.get(name)
            if hit is not None and uid:
                print(f"  NOTE: source '{name}' matched by name — its unique id "
                      f"changed since the last run.")
        if hit is None:
            missing.append(name or uid or "?")
        else:
            resolved.append(hit)
    return resolved, missing


def timeline_source_record(name: str, timeline) -> dict:
    """{"uid": ..., "name": ...} for the manifest's source list."""
    uid = ""
    getter = getattr(timeline, "GetUniqueId", None)
    if callable(getter):
        try:
            uid = getter() or ""
        except Exception:
            uid = ""
    return {"uid": uid, "name": name}


def clamp_desired_to_media(clip_info: ClipInfo) -> tuple:
    """(start, end) clamped to the media's own extents.

    Asking for frames past the end of the file produces a placement that can
    never match the request, which without clamping reads as a change forever.
    Update-path only — create mode's behaviour is deliberately untouched.
    """
    start = max(0, clip_info.start_frame)
    end = clip_info.end_frame
    try:
        raw = clip_info.media_pool_item.GetClipProperty("End")
        media_end = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError, Exception):
        media_end = None
    if media_end is not None:
        end = min(end, media_end)
    if end < start:
        end = start
    return start, end


def write_update_marker(item, kind: str, run_no: int, note: str,
                        marker_name: str, desired_range=None) -> bool:
    """Add this run's marker to a clip, stepping off any frame already taken."""
    adder = getattr(item, "AddMarker", None)
    if not callable(adder):
        return False
    try:
        source_start = item.GetSourceStartFrame()
        duration = item.GetDuration()
    except Exception:
        return False
    if source_start is None or duration is None:
        return False

    existing = normalised_markers(item)
    frame = pick_marker_frame(int(source_start), int(duration), existing,
                              UPDATE_MARKER_FRACTION)
    if frame < 0:
        print(f"  WARNING: no free frame for an update marker on "
              f"'{marker_name}'.")
        return False

    custom = build_update_customdata(
        run_no, kind,
        desired_range[0] if desired_range else None,
        desired_range[1] if desired_range else None,
    )
    try:
        return bool(adder(frame, UPDATE_MARKER_COLORS.get(kind, "Blue"),
                          marker_name, note, 1, custom))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Update Workflow
# ---------------------------------------------------------------------------

def previous_update_note(markers: dict) -> str:
    """The note off a clip's own update marker, so the changelog can accumulate."""
    for frame in sorted(markers, reverse=True):
        info = markers[frame] or {}
        custom = info.get("customData") or ""
        if isinstance(custom, str) and custom.startswith(UPDATE_MARKER_PREFIX):
            return info.get("note") or ""
    return ""


def previous_update_kind(markers: dict) -> Optional[str]:
    """The kind recorded by a clip's own update marker, or None.

    Used to avoid stacking a fresh "no longer used" marker onto a clip on every
    subsequent run — it stays unused, and one marker saying so is enough.
    """
    for frame in sorted(markers, reverse=True):
        info = markers[frame] or {}
        custom = info.get("customData") or ""
        if not isinstance(custom, str) or not custom.startswith(UPDATE_MARKER_PREFIX):
            continue
        try:
            data = json.loads(custom[len(UPDATE_MARKER_PREFIX):])
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("kind"), str):
            return data["kind"]
    return None


def run_update_workflow(
    source_selection_mode: str,
    selection_method: str,
    connection_threshold: int,
    allow_disabled_clips: bool,
    video_only: bool,
    mark_duplicates: bool,
    mark_retimed_clips: bool,
    use_xml_retime: bool,
    import_clip_names: bool,
    merge_by_source_file: bool,
    protect_graded_clips: bool,
    dry_run: bool,
    progress: Optional["ProgressUI"] = None,
) -> None:
    """Reconcile the current All Clips timeline against its source timelines.

    Minimal disturbance: only clips whose source range actually moved are
    rebuilt, because Resolve has no trim or move and a rebuild costs that clip
    its grade and Fusion comps. Everything else is left exactly as it is.
    """

    def _p(stage: str, current: int = 0, total: int = 0) -> None:
        if progress is not None:
            progress.set(stage, current, total)

    project_manager = resolve.GetProjectManager()  # noqa: F821
    project = project_manager.GetCurrentProject()
    if project is None:
        print("No project is open.")
        return
    media_pool = project.GetMediaPool()

    target = project.GetCurrentTimeline()
    if target is None:
        print("No current timeline. Open the All Clips timeline you want to update.")
        return

    target_name = target.GetName()
    target_uid = ""
    uid_getter = getattr(target, "GetUniqueId", None)
    if callable(uid_getter):
        try:
            target_uid = uid_getter() or ""
        except Exception:
            target_uid = ""
    print(f"Update target: '{target_name}' (uid {target_uid or '<unavailable>'})")

    current_settings = {
        "connection_threshold": connection_threshold,
        "merge_by_source_file": merge_by_source_file,
        "video_only": video_only,
        "allow_disabled_clips": allow_disabled_clips,
        "use_xml_retime": use_xml_retime,
        "import_clip_names": import_clip_names,
        "mark_duplicates": mark_duplicates,
        "mark_retimed_clips": mark_retimed_clips,
    }

    manifest, store = read_manifest(target)
    adopted = manifest is None
    forked = False

    if manifest is None:
        print("This timeline carries no All Clips manifest.")
        if source_selection_mode == "Recorded":
            print("  Nothing records where its clips came from, so there is nothing "
                  "to re-read. Choose 'Current selection' as the source to adopt "
                  "this timeline, or use Create New Timeline instead.")
            return
        print("  ADOPTING unmanaged timeline — sources come from the current selection.")
    else:
        print(f"Manifest found via {store}: run {manifest.get('run_counter', 0)}, "
              f"last {manifest.get('last_run_utc', '?')}, "
              f"{len(manifest.get('sources') or [])} recorded source timeline(s)")
        recorded = manifest.get("settings") or {}
        for key in ("connection_threshold", "merge_by_source_file",
                    "use_xml_retime", "allow_disabled_clips"):
            if key in recorded and recorded[key] != current_settings[key]:
                print(f"  WARNING: {key} changed since the last run "
                      f"({recorded[key]!r} -> {current_settings[key]!r}). Merged "
                      f"ranges will differ and many clips may read as changed.")
        if recorded.get("preserve_track_layout"):
            print("  NOTE: this timeline was built with Preserve Source Track "
                  "Layout. Update mode reconciles by clip identity only — block "
                  "boundaries are not re-derived.")
        if target_uid and manifest.get("timeline_uid") \
                and manifest["timeline_uid"] != target_uid:
            print("  NOTE: this is a copy of the timeline the manifest was written "
                  "for. Taking it over; the history is kept.")
            forked = True

    # Build the pre-21.0.4 name fallback map once, for resolve_source_timelines.
    project_timelines = {}
    try:
        timeline_count = project.GetTimelineCount()
    except Exception:
        timeline_count = 0
    for index in range(1, (timeline_count or 0) + 1):
        try:
            tl = project.GetTimelineByIndex(index)
        except Exception:
            tl = None
        if tl is not None:
            project_timelines[tl.GetName()] = tl

    # ---- source timelines -------------------------------------------------
    sources = []
    if manifest is not None and source_selection_mode in ("Recorded", "Union"):
        recorded_sources, missing = resolve_timelines_by_source_record(
            project, manifest.get("sources") or [])
        sources.extend(recorded_sources)
        if missing:
            print(f"  WARNING: {len(missing)} recorded source timeline(s) no longer "
                  f"resolve: {', '.join(missing)}")
            print("  Every shot that came only from them will be reported as no "
                  "longer used. Cancel now if that is not what you meant.")

    if manifest is None or source_selection_mode in ("Selection", "Union"):
        sources.extend(resolve_source_timelines(
            media_pool, selection_method, project_timelines, progress_cb=_p))

    unique_sources = []
    seen_keys = set()
    for name, timeline in sources:
        record = timeline_source_record(name, timeline)
        if target_uid and record["uid"] == target_uid:
            print(f"  Skipping '{name}': that is the timeline being updated.")
            continue
        key = record["uid"] or f"name:{name}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_sources.append((name, timeline))

    if not unique_sources:
        print("No source timelines to read. Nothing to do.")
        return
    print(f"Reading {len(unique_sources)} source timeline(s): "
          f"{', '.join(name for name, _tl in unique_sources)}")

    # ---- desired state ----------------------------------------------------
    all_clip_infos, _blocks, _dup_count = collect_desired_clips(
        unique_sources,
        connection_threshold=connection_threshold,
        allow_disabled_clips=allow_disabled_clips,
        mark_duplicates=mark_duplicates,
        mark_retimed_clips=mark_retimed_clips,
        use_xml_retime=use_xml_retime,
        import_clip_names=import_clip_names,
        preserve_track_layout=False,
        merge_by_source_file=merge_by_source_file,
        sorting_method="Source Name",
        progress_cb=_p,
    )
    if not all_clip_infos:
        return

    for clip_info in all_clip_infos:
        clip_info.start_frame, clip_info.end_frame = clamp_desired_to_media(clip_info)

    # ---- current state ----------------------------------------------------
    _p("Scanning the timeline...", 0, 0)
    placed_by_identity, unmanaged = scan_timeline_placements(
        target, merge_by_source_file)
    placed_total = sum(len(v) for v in placed_by_identity.values())
    print(f"Scanned {placed_total} placed clip(s) on '{target_name}'")
    if unmanaged:
        print(f"NOTE: {len(unmanaged)} timeline item(s) have no source clip "
              f"(titles, generators, compound or Fusion clips). Left untouched.")

    desired_by_identity = group_desired_by_identity(
        all_clip_infos, merge_by_source_file)
    diffs = diff_desired_vs_placed(desired_by_identity, placed_by_identity)
    counts = summarise_diffs(diffs)

    run_no = (manifest.get("run_counter", 0) if manifest else 0) + 1
    now_local = local_now_str()
    now_utc = utc_now_iso()

    # Shrinking frees space a later grow can use, so range changes are applied
    # by net duration change ascending: every shortening before any lengthening.
    def _net_change(diff):
        old = diff.placed.source_end - diff.placed.source_start
        new = diff.clip_info.end_frame - diff.clip_info.start_frame
        return new - old

    changed = sorted(
        [d for d in diffs if d.kind in ("extended", "shortened", "both")],
        key=_net_change)
    added = [d for d in diffs if d.kind == "new"]

    # A shot that dropped out stays dropped. Once it carries a marker saying so,
    # it is not actionable again — otherwise every later run would report it and
    # stack another marker on it.
    removed = []
    already_flagged = 0
    for diff in diffs:
        if diff.kind != "dropped":
            continue
        if previous_update_kind(normalised_markers(diff.placed.item)) in (
                "dropped", "superseded"):
            already_flagged += 1
        else:
            removed.append(diff)

    print("")
    print(f"=== Update plan for '{target_name}' (run {run_no}) ===")
    print(f"  unchanged      {counts.get('unchanged', 0)}")
    print(f"  extended       {counts.get('extended', 0)}")
    print(f"  shortened      {counts.get('shortened', 0)}")
    print(f"  both ends      {counts.get('both', 0)}")
    print(f"  new            {len(added)}")
    print(f"  no longer used {len(removed)}")
    if already_flagged:
        print(f"  ({already_flagged} already flagged as unused by an earlier run)")
    for diff in diffs:
        if diff.kind == "unchanged":
            continue
        if diff.kind == "new":
            print(f"  + new       {diff.clip_info.media_pool_item.GetName()} "
                  f"{diff.clip_info.start_frame}-{diff.clip_info.end_frame}")
        elif diff.kind == "dropped":
            print(f"  - unused    {diff.placed.name or diff.identity} "
                  f"{diff.placed.source_start}-{diff.placed.source_end}")
        else:
            print(f"  ~ {diff.kind:<9} {diff.placed.name or diff.identity} "
                  f"{diff.placed.source_start}-{diff.placed.source_end} -> "
                  f"{diff.clip_info.start_frame}-{diff.clip_info.end_frame} "
                  f"(head {-diff.head_delta:+d}f, tail {diff.tail_delta:+d}f)")
    print("")

    source_records = [timeline_source_record(n, t) for n, t in unique_sources]

    def _save_manifest(run_record) -> None:
        """Write the manifest back. run_record=None records no run.

        A run that changed nothing still refreshes the sources, settings and
        timestamp — that is what makes adopting an already-matching timeline
        useful — but it does not bump the run counter or add to the history,
        which would fill the log with no-ops.
        """
        nonlocal manifest
        if manifest is None:
            manifest = build_manifest(target_uid, target_name, source_records,
                                      current_settings, now_utc, TOOL_VERSION)
            manifest["adopted"] = True
        else:
            manifest = dict(manifest)
            manifest["timeline_uid"] = target_uid
            manifest["timeline_name"] = target_name
            manifest["sources"] = source_records
            manifest["settings"] = current_settings
            manifest["tool_version"] = TOOL_VERSION
        if run_record is None:
            manifest["last_run_utc"] = now_utc
        else:
            manifest = manifest_add_run(manifest, run_record)
        write_manifest(target, manifest)

    actionable = changed + added + removed
    if not actionable:
        print("Nothing to do — the timeline already matches its sources.")
        if not dry_run:
            _save_manifest(None)
        _p("Done!", 1, 1)
        return

    if dry_run:
        print("DRY RUN — nothing was changed. Untick 'Dry run' to apply this plan.")
        _p("Done!", 1, 1)
        return

    # ---- execute ----------------------------------------------------------
    if not project.SetCurrentTimeline(target):
        print("ERROR: could not make the target timeline current. AppendToTimeline "
              "always targets the current timeline, so this run would edit the "
              "wrong one. Aborting without changes.")
        return

    timeline_fps = None
    for getter, name in ((target, "timelineFrameRate"), (project, "timelineFrameRate")):
        try:
            raw = getter.GetSetting(name)
        except Exception:
            raw = None
        if raw:
            try:
                timeline_fps = float(raw)
            except (TypeError, ValueError):
                timeline_fps = None
            break

    timeline_start = 0
    start_getter = getattr(target, "GetStartFrame", None)
    if callable(start_getter):
        try:
            timeline_start = int(start_getter() or 0)
        except Exception:
            timeline_start = 0

    occupancy = build_track_occupancy(target)

    end_frame = timeline_start
    end_getter = getattr(target, "GetEndFrame", None)
    if callable(end_getter):
        try:
            end_frame = int(end_getter() or timeline_start)
        except Exception:
            end_frame = timeline_start
    for bounds in occupancy.values():
        for _start, bound_end in bounds:
            end_frame = max(end_frame, bound_end)

    track_name = format_update_track_name(run_no, now_local.split(" ")[0])
    state_box = {
        "track": None,
        "cursor": end_frame + INTER_TIMELINE_GAP,
        "warned_record": False,
    }
    tally = {"extended": 0, "shortened": 0, "both": 0, "new": 0, "superseded": 0,
             "dropped": 0, "skipped": 0, "failed": 0}

    def _ensure_update_track():
        if state_box["track"] is None:
            index = add_update_track(target, track_name)
            if index is None:
                print("ERROR: could not add a video track for this run's clips.")
                return None
            state_box["track"] = index
            occupancy.setdefault(index, [])
        return state_box["track"]

    def _record_placement(item, track_index, requested_record):
        """Book a placed item into the occupancy map. Returns its duration."""
        try:
            actual_start = int(item.GetStart())
            duration = int(item.GetDuration())
        except Exception:
            actual_start, duration = requested_record, 1
        if actual_start != requested_record and not state_box["warned_record"]:
            print(f"  WARNING: asked for record frame {requested_record} but the "
                  f"clip landed at {actual_start}. recordFrame and GetStart() "
                  f"appear to use different frame spaces on this build; "
                  f"placement may be off by {actual_start - requested_record} "
                  f"frames for the rest of this run.")
            state_box["warned_record"] = True
        insert_occupancy(occupancy.setdefault(track_index, []),
                         actual_start, actual_start + max(1, duration))
        return duration

    def _accepted_for(clip_info, achieved_start, achieved_end):
        """The desired range, but only when the placement could not reach it."""
        if achieved_start is None or achieved_end is None:
            return (clip_info.start_frame, clip_info.end_frame)
        if (abs(achieved_start - clip_info.start_frame) > UPDATE_CHANGE_TOLERANCE
                or abs(achieved_end - clip_info.end_frame) > UPDATE_CHANGE_TOLERANCE):
            return (clip_info.start_frame, clip_info.end_frame)
        return None

    def _annotate_new(item, clip_info):
        """Duplicate/retime markers and imported names for a clip we just placed."""
        try:
            source_start = int(item.GetSourceStartFrame())
            duration = int(item.GetDuration())
        except Exception:
            return
        if mark_duplicates and clip_info.duplicate_set_index is not None:
            color, text, note = build_duplicate_marker_text(clip_info)
            frame = pick_marker_frame(source_start, duration,
                                      normalised_markers(item), 0.25)
            if frame >= 0:
                try:
                    item.AddMarker(frame, color, text, note, 1, "")
                except Exception:
                    pass
        if mark_retimed_clips and clip_info.is_retimed:
            text, note = build_retime_marker_text(clip_info)
            frame = pick_marker_frame(source_start, duration,
                                      normalised_markers(item), 0.5)
            if frame >= 0:
                try:
                    item.AddMarker(frame, "Red", text, note, 1, "")
                except Exception:
                    pass
        if import_clip_names and clip_info.source_name:
            try:
                item.SetName(clip_info.source_name)
            except Exception:
                pass
            for version_name in clip_info.version_names or []:
                try:
                    item.AddVersion(version_name, 0)
                except Exception:
                    pass
            if clip_info.current_version_name:
                try:
                    item.SetCurrentVersion(clip_info.current_version_name, 0)
                except Exception:
                    pass

    def _place_on_update_track(clip_info, kind, marker_name, note):
        track = _ensure_update_track()
        if track is None:
            return None
        requested = state_box["cursor"]
        item, status, achieved_start, achieved_end = \
            append_clip_with_slip_compensation(
                media_pool, target, clip_info, video_only=video_only,
                track_index=track, record_frame=requested,
                timeline_fps=timeline_fps)
        if item is None:
            print(f"  ERROR: could not place '{clip_info.media_pool_item.GetName()}' "
                  f"on the update track (status {status}).")
            return None
        duration = _record_placement(item, track, requested)
        state_box["cursor"] = requested + max(1, duration) + INTER_TIMELINE_GAP
        _annotate_new(item, clip_info)
        write_update_marker(item, kind, run_no, note, marker_name,
                            _accepted_for(clip_info, achieved_start, achieved_end))
        return item

    def _mark_in_place(placed, kind, marker_name, note):
        write_update_marker(placed.item, kind, run_no, note, marker_name)

    def _rebuild(diff) -> str:
        placed = diff.placed
        clip_info = diff.clip_info
        state = capture_item_state(placed.item, placed.track_index)
        label = state.name or placed.name or clip_info.media_pool_item.GetName()
        old_src = (placed.source_start, placed.source_end)
        new_src = (clip_info.start_frame, clip_info.end_frame)
        line = format_change_line(now_local, run_no, diff.head_delta,
                                  diff.tail_delta, old_src, new_src)
        history = merge_changelog(previous_update_note(state.markers), line)

        if state.linked_item_count > 0:
            print(f"  SKIP '{label}': it has linked audio. Deleting the video item "
                  f"would orphan it, and re-appending gives no control over the "
                  f"audio track. Update it by hand.")
            _mark_in_place(placed, "manual", f"Needs manual update (run {run_no})",
                           history)
            return "skipped"

        loses_work = state.fusion_comp_count > 0 or len(state.version_names) > 1
        if loses_work and protect_graded_clips:
            print(f"  SKIP '{label}': rebuilding would lose "
                  f"{state.fusion_comp_count} Fusion comp(s) and "
                  f"{len(state.version_names)} colour version(s). Untick "
                  f"'Protect clips with Fusion comps' to rebuild it anyway.")
            _mark_in_place(placed, "manual", f"Needs manual update (run {run_no})",
                           history)
            return "skipped"
        if loses_work:
            print(f"  WARNING: '{label}' loses {state.fusion_comp_count} Fusion "
                  f"comp(s) and {len(state.version_names)} colour version(s) — "
                  f"they cannot survive a delete and re-append.")

        new_record, new_duration, head_need, tail_need = plan_rebuild_placement(
            placed.record_start, old_src, new_src)

        bounds = occupancy.setdefault(placed.track_index, [])
        index = occupancy_index(bounds, placed.record_start)
        if index < 0:
            free_before = free_after = 0
        else:
            free_before, free_after = compute_free_space(bounds, index,
                                                         timeline_start)
        fits = fits_in_place(free_before, free_after, head_need, tail_need)
        may_widen = fits_in_place(free_before, free_after, head_need, tail_need,
                                  REBUILD_FIT_SLACK)

        if not fits:
            print(f"  '{label}' needs {head_need}f before and {tail_need}f after "
                  f"but has {free_before}f/{free_after}f — placing the updated "
                  f"version on the update track instead.")
            new_item = _place_on_update_track(
                clip_info, diff.kind, f"Updated (moved, run {run_no})", history)
            if new_item is None:
                return "failed"
            _mark_in_place(placed, "superseded", f"Superseded (run {run_no})",
                           merge_changelog(
                               previous_update_note(state.markers),
                               f"{now_local}{CHANGELOG_SEPARATOR}run {run_no}"
                               f"{CHANGELOG_SEPARATOR}superseded by the updated "
                               f"copy on '{track_name}'"))
            return "superseded"

        original_mpi = None
        try:
            original_mpi = placed.item.GetMediaPoolItem()
        except Exception:
            original_mpi = None

        if index >= 0:
            bounds.pop(index)
        try:
            deleted = target.DeleteClips([placed.item], False)
        except Exception:
            deleted = False
        if not deleted:
            print(f"  ERROR: DeleteClips failed for '{label}'; leaving it alone.")
            if index >= 0:
                insert_occupancy(bounds, placed.record_start, placed.record_end)
            return "failed"

        item, status, achieved_start, achieved_end = \
            append_clip_with_slip_compensation(
                media_pool, target, clip_info, video_only=video_only,
                track_index=placed.track_index, record_frame=new_record,
                timeline_fps=timeline_fps, allow_widening=may_widen)

        if item is None:
            print(f"  ERROR: re-append failed for '{label}' (status {status}); "
                  f"restoring the previous version.")
            rollback = ClipInfo(media_pool_item=original_mpi or clip_info.media_pool_item,
                                start_frame=old_src[0], end_frame=old_src[1])
            back, back_status, _bs, _be = append_clip_with_slip_compensation(
                media_pool, target, rollback, video_only=video_only,
                track_index=placed.track_index, record_frame=placed.record_start,
                timeline_fps=timeline_fps)
            if back is None:
                print(f"  ERROR: rollback ALSO failed for '{label}'. The clip is "
                      f"gone from the timeline. Re-add "
                      f"{old_src[0]}-{old_src[1]} on track "
                      f"{placed.track_index} at frame {placed.record_start}.")
            else:
                _record_placement(back, placed.track_index, placed.record_start)
                restore_item_state(back, state, old_src[0], old_src[1],
                                   UPDATE_MARKER_PREFIX)
            return "failed"

        _record_placement(item, placed.track_index, new_record)
        restored = restore_item_state(item, state, new_src[0], new_src[1],
                                      UPDATE_MARKER_PREFIX)
        if restored["markers_dropped"]:
            print(f"  {restored['markers_dropped']} marker(s) on '{label}' fell "
                  f"outside the new range and were not restored.")
        _annotate_new(item, clip_info)
        write_update_marker(item, diff.kind, run_no, history,
                            f"Updated ({diff.kind}, run {run_no})",
                            _accepted_for(clip_info, achieved_start, achieved_end))
        return diff.kind

    total_steps = len(changed) + len(added) + len(removed)
    step = 0

    print(f"Applying {len(changed)} range change(s)...")
    for diff in changed:
        step += 1
        _p(f"Updating: {diff.placed.name or diff.identity}", step, total_steps)
        outcome = _rebuild(diff)
        tally[outcome] = tally.get(outcome, 0) + 1

    print(f"Adding {len(added)} new shot(s)...")
    for diff in added:
        step += 1
        name = diff.clip_info.media_pool_item.GetName()
        _p(f"Adding: {name}", step, total_steps)
        note = (f"{now_local}{CHANGELOG_SEPARATOR}run {run_no}"
                f"{CHANGELOG_SEPARATOR}new shot"
                f"{CHANGELOG_SEPARATOR}"
                f"{diff.clip_info.start_frame}-{diff.clip_info.end_frame}")
        item = _place_on_update_track(diff.clip_info, "new",
                                      f"New (run {run_no})", note)
        tally["new" if item is not None else "failed"] += 1

    print(f"Marking {len(removed)} shot(s) that are no longer used...")
    for diff in removed:
        step += 1
        _p(f"Marking unused: {diff.placed.name or diff.identity}", step, total_steps)
        markers = normalised_markers(diff.placed.item)
        note = merge_changelog(
            previous_update_note(markers),
            f"{now_local}{CHANGELOG_SEPARATOR}run {run_no}"
            f"{CHANGELOG_SEPARATOR}no longer used in any source timeline")
        if write_update_marker(diff.placed.item, "dropped", run_no, note,
                               f"No longer used (run {run_no})"):
            tally["dropped"] += 1

    # ---- record the run ---------------------------------------------------
    run_record = {
        "n": run_no,
        "utc": now_utc,
        "track": state_box["track"],
        "track_name": track_name if state_box["track"] else "",
        "unchanged": counts.get("unchanged", 0),
        "extended": tally.get("extended", 0),
        "shortened": tally.get("shortened", 0),
        "both": tally.get("both", 0),
        "new": tally.get("new", 0),
        "superseded": tally.get("superseded", 0),
        "dropped": tally.get("dropped", 0),
        "skipped": tally.get("skipped", 0),
        "failed": tally.get("failed", 0),
        "sources": [timeline_source_record(n, t)["uid"] for n, t in unique_sources],
    }
    if forked:
        run_record["forked"] = True
    _save_manifest(run_record)

    run_marker_frame = first_free_frame(normalised_markers(target), 1, lower=1)
    if run_marker_frame >= 0:
        try:
            target.AddMarker(
                run_marker_frame, RUN_MARKER_COLOR,
                f"Update run {run_no} - {now_local}",
                (f"{tally.get('extended', 0)} extended, "
                 f"{tally.get('shortened', 0)} shortened, "
                 f"{tally.get('both', 0)} both ends, "
                 f"{tally.get('new', 0)} new, "
                 f"{tally.get('superseded', 0)} superseded, "
                 f"{tally.get('dropped', 0)} no longer used, "
                 f"{tally.get('skipped', 0)} skipped, "
                 f"{tally.get('failed', 0)} failed.\nSources: "
                 + ", ".join(n for n, _t in unique_sources)),
                1, RUN_MARKER_PREFIX + json.dumps(
                    {"v": 1, "run": run_no, "utc": now_utc},
                    sort_keys=True, separators=(",", ":")),
            )
        except Exception:
            pass

    print("")
    print(f"=== Update run {run_no} complete ===")
    for key in ("extended", "shortened", "both", "new", "superseded", "dropped",
                "skipped", "failed"):
        print(f"  {key:<11} {tally.get(key, 0)}")
    if tally.get("failed"):
        print("  Check the log above for the clips that failed.")
    _p("Done!", 1, 1)
    print("Done!")


# ---------------------------------------------------------------------------
# Progress UI
# ---------------------------------------------------------------------------

class ProgressUI:
    """Modeless progress window built with Resolve's UIManager.

    Fusion's UI doesn't tick during blocking Python — we call Fusion:Wait(0)
    after each update to flush pending events so the window stays responsive.
    If Wait isn't available, the label/slider values still update; refresh
    happens on the next natural event-loop tick.
    """

    def __init__(self) -> None:
        try:
            self.ui = fu.UIManager  # noqa: F821
            self.disp = bmd.UIDispatcher(self.ui)  # noqa: F821
            self.win = self.disp.AddWindow({
                "ID": "GenAllClipsProgress",
                "WindowTitle": "Generate All Clips Timeline PRO — Progress",
                "Geometry": [150, 150, 520, 120],
                "Spacing": 8,
            }, [
                self.ui.VGroup({"ID": "root"}, [
                    self.ui.Label({"ID": "StageLabel", "Text": "Starting..."}),
                    self.ui.Slider({
                        "ID": "ProgressBar",
                        "Integer": True,
                        "Minimum": 0,
                        "Maximum": 100,
                        "Value": 0,
                    }),
                    self.ui.Label({"ID": "CounterLabel", "Text": ""}),
                ]),
            ])
            self.itm = self.win.GetItems()
            self.win.Show()
            self._tick()
            self._ok = True
        except Exception as e:
            print(f"Progress UI unavailable: {e}")
            self._ok = False

    def set(self, stage: str, current: int = 0, total: int = 0) -> None:
        if not self._ok:
            return
        try:
            self.itm["StageLabel"].Text = stage
            if total > 0:
                pct = int(100 * current / total) if total else 0
                if pct < 0:
                    pct = 0
                elif pct > 100:
                    pct = 100
                self.itm["ProgressBar"].Value = pct
                self.itm["CounterLabel"].Text = f"{current} / {total}  ({pct}%)"
            else:
                self.itm["ProgressBar"].Value = 0
                self.itm["CounterLabel"].Text = ""
        except Exception:
            pass
        self._tick()

    def _tick(self) -> None:
        try:
            fusion = bmd.scriptapp("Fusion")  # noqa: F821
            if fusion is not None:
                fusion.Wait(0)
        except Exception:
            pass

    def close(self) -> None:
        if not self._ok:
            return
        try:
            self.win.Hide()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# UI Dialog
# ---------------------------------------------------------------------------

def build_and_show_ui() -> Optional[dict]:
    """Build the UI dialog and return user selections, or None if cancelled."""
    # fu and bmd are pre-injected globals in Resolve's scripting environment
    ui = fu.UIManager  # noqa: F821
    disp = bmd.UIDispatcher(ui)  # noqa: F821
    width, height = 540, 510

    win = disp.AddWindow({
        "ID": "MyWin",
        "WindowTitle": "Generate All Clips Timeline PRO",
        "Geometry": [100, 100, width, height],
        "Spacing": 10,
    }, [
        ui.VGroup({"ID": "root"}, [
            ui.HGroup({"ID": "dst"}, [
                ui.Label({"ID": "DstLabel", "Text": "New Timeline Name"}),
                ui.TextEdit({
                    "ID": "DstTimelineName",
                    "Text": "All_Sources",
                    "PlaceholderText": "All_Sources",
                }),
            ]),
            ui.HGroup({}, [
                ui.Label({"ID": "selectionMethodLabel", "Text": "Select Timelines By:"}),
                ui.ComboBox({"ID": "selectionMethod", "Text": "Current Selection"}),
            ]),
            ui.HGroup({}, [
                ui.Label({"ID": "sortingMethodLabel", "Text": "Sort Clips By:"}),
                ui.ComboBox({"ID": "sortingMethod", "Text": "Source Name"}),
            ]),
            ui.HGroup({}, [
                ui.Label({
                    "ID": "ConnectionThresholdLabel",
                    "Text": "Connection Threshold (Frames)",
                }),
                ui.TextEdit({
                    "ID": "ConnectionThreshold",
                    "Text": "25",
                    "PlaceholderText": "25",
                }),
            ]),
            ui.CheckBox({"ID": "includeDisabledItems", "Text": "Include Disabled Clips"}),
            ui.CheckBox({
                "ID": "videoOnly",
                "Text": "Video Only (No Audio)",
                "Checked": True,
            }),
            ui.CheckBox({
                "ID": "markDuplicates",
                "Text": "Mark Duplicate Clips with Colored Markers",
                "Checked": True,
            }),
            ui.CheckBox({
                "ID": "markRetimedClips",
                "Text": "Mark Retimed Clips with Red Markers",
                "Checked": True,
            }),
            ui.CheckBox({
                "ID": "useXmlRetime",
                "Text": "Use XML for Precise Retime Detection",
                "Checked": True,
            }),
            ui.CheckBox({
                "ID": "importClipNames",
                "Text": "Import Clip Names from Source Timelines",
                "Checked": False,
            }),
            ui.CheckBox({
                "ID": "preserveTrackLayout",
                "Text": "Preserve Source Track Layout",
                "Checked": False,
            }),
            ui.CheckBox({
                "ID": "mergeBySourceFile",
                "Text": "Merge Clips by Source File Path (across MediaPool entries)",
                "Checked": True,
                "ToolTip": (
                    "When ON (default): clips that reference the same source "
                    "file on disk are merged into one max-length entry, even "
                    "if Resolve sees them as separate Media Pool items "
                    "(different MediaIds). This is the common case when each "
                    "source timeline was prepped or round-tripped separately "
                    "and the same file ended up imported into the bin under "
                    "two entries. The Media Pool item with the widest source "
                    "range is used for the merged clip so AppendToTimeline "
                    "can place the full range.\n\n"
                    "When OFF: clips are bucketed strictly by MediaId. Two "
                    "imports of the same file stay separate on the output "
                    "timeline. Use this only if you intentionally want "
                    "duplicate Media Pool entries kept distinct (e.g. you "
                    "rely on sub-clips being treated as their own sources)."
                ),
            }),
            ui.HGroup({"ID": "buttons"}, [
                ui.Button({"ID": "cancelButton", "Text": "Cancel"}),
                ui.Button({"ID": "goButton", "Text": "Go"}),
            ]),
        ]),
    ])

    run_export = {"value": False}

    def on_close(ev):
        disp.ExitLoop()
        run_export["value"] = False

    def on_cancel(ev):
        print("Cancel Clicked")
        disp.ExitLoop()
        run_export["value"] = False

    def on_go(ev):
        print("Go Clicked")
        disp.ExitLoop()
        run_export["value"] = True

    win.On.MyWin.Close = on_close
    win.On.cancelButton.Clicked = on_cancel
    win.On.goButton.Clicked = on_go

    itm = win.GetItems()

    # Populate combo boxes
    itm["selectionMethod"].AddItem("Current Selection")
    itm["selectionMethod"].AddItem("Current Bin")

    itm["sortingMethod"].AddItem("Source Name")
    itm["sortingMethod"].AddItem("Source Inpoint")
    itm["sortingMethod"].AddItem("Inpoint on Timeline")
    itm["sortingMethod"].AddItem("Reel Name")
    itm["sortingMethod"].AddItem("None")

    win.Show()
    disp.RunLoop()
    win.Hide()

    if not run_export["value"]:
        return None

    timeline_name = itm["DstTimelineName"].PlainText.strip() or "All_Sources"

    threshold_text = itm["ConnectionThreshold"].PlainText
    try:
        threshold = int(threshold_text)
    except (ValueError, TypeError):
        threshold = DEFAULT_CONNECTION_THRESHOLD

    return {
        "dst_timeline_name": timeline_name,
        "selection_method": itm["selectionMethod"].CurrentText,
        "sorting_method": itm["sortingMethod"].CurrentText,
        "connection_threshold": threshold,
        "allow_disabled_clips": itm["includeDisabledItems"].Checked,
        "video_only": itm["videoOnly"].Checked,
        "mark_duplicates": itm["markDuplicates"].Checked,
        "mark_retimed_clips": itm["markRetimedClips"].Checked,
        "use_xml_retime": itm["useXmlRetime"].Checked,
        "import_clip_names": itm["importClipNames"].Checked,
        "preserve_track_layout": itm["preserveTrackLayout"].Checked,
        "merge_by_source_file": itm["mergeBySourceFile"].Checked,
    }


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    """Script entry point: show UI, collect parameters, run workflow."""
    params = build_and_show_ui()
    if params is None:
        return
    progress = ProgressUI()
    try:
        run_workflow(progress=progress, **params)
    finally:
        progress.close()


main()
