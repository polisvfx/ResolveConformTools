#!/usr/bin/env python
"""
Generate All Clips Timeline PRO
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

import math
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# Constants
MIN_FRAME_DIFF = 3
MIN_PERCENT_DIFF = 3.0
DEFAULT_CONNECTION_THRESHOLD = 25
INTER_TIMELINE_GAP = 25  # frames between source-timeline blocks in preserve-layout mode

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
    selected_bin = media_pool.GetCurrentFolder()

    # Build timeline name lookup
    project_timelines = {}
    for timeline_idx in range(1, num_timelines + 1):
        tl = project.GetTimelineByIndex(timeline_idx)
        project_timelines[tl.GetName()] = tl

    # Get selected clips
    selected_clips = []
    if selection_method == "Current Bin":
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
        return

    if not selected_clips:
        print("No clips selected or found in bin. Please select clips or timelines.")
        return

    print("Processing selected clips...")

    # Collect clips from all selected timelines
    clips: dict[str, list[ClipInfo]] = {}
    total_selected = len(selected_clips)
    _p("Reading clips...", 0, total_selected)

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
        print(f"Processing timeline: {timeline_name}")
        _p(f"Reading: {timeline_name} ({sel_idx}/{total_selected})",
           sel_idx, total_selected)

        curr_timeline = project_timelines.get(timeline_name)
        if not curr_timeline:
            print(f"Timeline not found in project: {timeline_name}")
            continue

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

                # Apply -1 adjustment for exclusive-to-inclusive (not for frame holds)
                if not is_frame_hold:
                    norm_end -= 1

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
                # Bucket key strategy:
                # - merge_by_source_file=True: key by File Path so two MediaPoolItems
                #   pointing at the same source file (dual-import scenario from
                #   round-tripped conform timelines) land in the same bucket and get
                #   merged. Falls back to media_id when File Path is empty.
                # - merge_by_source_file=False: key by media_id (legacy behavior).
                # When preserving track layout, we also include the track index in the
                # key to prevent cross-track merging.
                identity_key = media_id
                if merge_by_source_file:
                    try:
                        fp = media_item.GetClipProperty("File Path") or ""
                    except Exception:
                        fp = ""
                    if fp:
                        identity_key = f"FP:{fp}"
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
        return

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

        api_dict = sanitize_for_api(clip_info)
        # Audio filtering: mediaType=1 is the documented way to import video
        # only and works across all Resolve versions. importVideo/importAudio
        # are kept as a belt-and-suspenders hint for newer API builds.
        if video_only:
            api_dict["mediaType"] = 1  # 1 = video, 2 = audio
        api_dict["importVideo"] = True
        api_dict["importAudio"] = not video_only
        if preserve_track_layout:
            api_dict["recordFrame"] = clip_info.timeline_inpoint
            api_dict["trackIndex"] = clip_info.source_track_index
        try:
            media_pool.AppendToTimeline([api_dict])
            append_success_count += 1
            if preserve_track_layout:
                print(f"  Placed on track {clip_info.source_track_index} "
                      f"at frame {clip_info.timeline_inpoint}")
        except Exception:
            print(f"  ERROR: Failed to add clip: {clip_info.media_pool_item.GetName()}")
            print(f"  Frame range attempted: {api_dict['startFrame']} to {api_dict['endFrame']}")
            append_error_count += 1

    print(f"Clip addition summary: {append_success_count} succeeded, "
          f"{append_error_count} failed")
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
                        and timeline_clip.clip.GetSourceStartFrame() >= clip_info.start_frame
                        and timeline_clip.clip.GetSourceEndFrame() <= clip_info.end_frame + 1):

                    set_idx = clip_info.duplicate_set_index
                    color = DUPLICATE_MARKER_COLORS[set_idx % len(DUPLICATE_MARKER_COLORS)]
                    marker_text = (f"Dup Set #{set_idx + 1} "
                                   f"({clip_info.duplicate_position + 1}/{clip_info.duplicate_set_size})")
                    source_start = timeline_clip.clip.GetSourceStartFrame()
                    clip_duration = timeline_clip.clip.GetDuration()
                    marker_position = source_start + math.floor(clip_duration * 0.25)

                    try:
                        success = timeline_clip.clip.AddMarker(
                            marker_position, color, marker_text,
                            "Duplicate clip detected", 1, "",
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
                        and timeline_clip.clip.GetSourceStartFrame() >= clip_info.start_frame
                        and timeline_clip.clip.GetSourceEndFrame() <= clip_info.end_frame + 1):

                    if clip_info.is_retimed:
                        source_start = timeline_clip.clip.GetSourceStartFrame()
                        clip_duration = timeline_clip.clip.GetDuration()
                        marker_position = source_start + math.floor(clip_duration * 0.5)

                        # Distinguish marker types
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
                        and timeline_clip.clip.GetSourceStartFrame() >= clip_info.start_frame
                        and timeline_clip.clip.GetSourceEndFrame() <= clip_info.end_frame + 1):

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
