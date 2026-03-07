#!/usr/bin/env python
"""
Generate All Clips Timeline
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
from dataclasses import dataclass
from typing import Optional

# Constants
MIN_FRAME_DIFF = 3
MIN_PERCENT_DIFF = 3.0
DEFAULT_CONNECTION_THRESHOLD = 25

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
    is_reversed: bool = False
    is_retimed: bool = False
    is_frame_hold: bool = False
    is_non_linear_retime: bool = False
    retime_percentage: Optional[float] = None
    xml_source_min: Optional[int] = None
    xml_source_max: Optional[int] = None


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

    return ClipInfo(
        media_pool_item=a.media_pool_item,
        start_frame=merged_min,
        end_frame=merged_max,
        timeline_inpoint=min(a.timeline_inpoint, b.timeline_inpoint),
        is_reversed=a.is_reversed or b.is_reversed,
        is_retimed=a.is_retimed or b.is_retimed,
        is_frame_hold=a.is_frame_hold and b.is_frame_hold,
        is_non_linear_retime=a.is_non_linear_retime or b.is_non_linear_retime,
        retime_percentage=a.retime_percentage if a.retime_percentage is not None else b.retime_percentage,
        xml_source_min=xml_min,
        xml_source_max=xml_max,
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
# Audio Removal
# ---------------------------------------------------------------------------

def remove_all_audio_tracks(timeline) -> None:
    """Remove all audio content from the timeline."""
    if timeline is None:
        print("Error: No timeline provided.")
        return

    print("Removing audio tracks from timeline...")
    try:
        track_count = timeline.GetTrackCount("audio")
        print(f"Found {track_count} audio tracks")

        for i in range(1, track_count + 1):
            items = timeline.GetItemListInTrack("audio", i)
            if items:
                print(f"Deleting items from track {i}")
                timeline.DeleteClips(items)

        for i in range(track_count, 1, -1):
            timeline.DeleteTrack("audio", i)
            print(f"Deleted track {i}")

        print("Successfully cleared all audio tracks.")
    except Exception as e:
        print(f"Error: An unexpected error occurred: {e}")


# ---------------------------------------------------------------------------
# Duplicate Detection & Marking
# ---------------------------------------------------------------------------

def are_clips_duplicates(clip1: TimelineClipData, clip2: TimelineClipData) -> bool:
    """Check if two clips reference the same media at different timeline positions."""
    try:
        path1 = clip1.media_pool_item.GetClipProperty("File Path")
        path2 = clip2.media_pool_item.GetClipProperty("File Path")
    except Exception:
        return False

    if path1 and path2 and path1 == path2:
        name1 = clip1.media_pool_item.GetName()
        name2 = clip2.media_pool_item.GetName()
        if name1 == name2:
            if clip1.start_frame != clip2.start_frame or clip1.track_index != clip2.track_index:
                return True
        return False
    return False


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


def find_and_mark_duplicates(timeline) -> None:
    """Find duplicate clips and mark them with colored markers."""
    if not timeline:
        print("No timeline is open. Please open a timeline.")
        return

    clips = get_all_timeline_clips(timeline)
    if not clips:
        print("No clips found in the timeline.")
        return

    print(f"Scanning for duplicates among {len(clips)} clips...")

    # Find duplicate sets
    duplicate_sets = []
    processed = set()

    for i in range(len(clips)):
        if i in processed:
            continue
        current_set = [clips[i]]
        processed.add(i)

        for j in range(i + 1, len(clips)):
            if j in processed:
                continue
            print(f"Comparing clip {i + 1} ({clips[i].name}) with clip {j + 1} ({clips[j].name})")
            if are_clips_duplicates(clips[i], clips[j]):
                print("  MATCH FOUND!")
                current_set.append(clips[j])
                processed.add(j)

        if len(current_set) > 1:
            duplicate_sets.append(current_set)
            print(f"Found duplicate set with {len(current_set)} clips")

    # Mark duplicates with colored markers
    marker_count = 0
    success_count = 0

    for set_index, dup_set in enumerate(duplicate_sets):
        color_index = set_index % len(DUPLICATE_MARKER_COLORS)
        color = DUPLICATE_MARKER_COLORS[color_index]

        print(f"Marking duplicate set {set_index + 1} with {len(dup_set)} clips (Color: {color})")

        for i, clip_data in enumerate(dup_set):
            marker_text = f"Dup Set #{set_index + 1} ({i + 1}/{len(dup_set)})"
            source_start = clip_data.clip.GetSourceStartFrame()
            clip_duration = clip_data.clip.GetDuration()
            offset_frame = math.floor(clip_duration * 0.25)
            marker_position = source_start + offset_frame

            try:
                success = clip_data.clip.AddMarker(
                    marker_position, color, marker_text,
                    "Duplicate clip detected", 1, "",
                )
            except Exception:
                success = False

            marker_count += 1
            if success:
                success_count += 1
                print(f"  Added marker to clip: {clip_data.name}")
            else:
                print(f"  Failed to add marker to clip: {clip_data.name}")

    # Summary
    if duplicate_sets:
        print(f"Found {len(duplicate_sets)} sets of duplicates with {marker_count} total clips.")
        print(f"Successfully added {success_count} markers out of {marker_count} attempts.")
        if success_count == 0:
            print("WARNING: Unable to add markers. This could be due to timeline permissions.")
            for i, s in enumerate(duplicate_sets):
                names = [c.name for c in s]
                print(f"Set #{i + 1}: {', '.join(names)}")
    else:
        print("No duplicate clips were found.")


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
) -> None:
    """Execute the complete timeline generation workflow."""

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

    for media_pool_item in selected_clips:
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
            continue

        timeline_name = media_pool_item.GetName()
        print(f"Processing timeline: {timeline_name}")

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
                        if xml_data["is_non_linear"]:
                            is_non_linear = True
                            is_retimed = True
                            # For non-linear retimes (speed ramps) the Resolve API source
                            # in/out may not reflect the full frame range the ramp accesses.
                            # Only in this case do we expand using the XML-derived range.
                            if not is_frame_hold:
                                norm_start = min(norm_start, xml_min)
                                norm_end = max(norm_end, xml_max)

                # Create ClipInfo
                clip_info = ClipInfo(
                    media_pool_item=media_item,
                    start_frame=norm_start,
                    end_frame=norm_end,
                    timeline_inpoint=track_item.GetStart(),
                    is_reversed=is_reversed,
                    is_retimed=is_retimed,
                    is_frame_hold=is_frame_hold,
                    is_non_linear_retime=is_non_linear,
                    retime_percentage=retime_pct,
                    xml_source_min=xml_min,
                    xml_source_max=xml_max,
                )
                clips.setdefault(media_id, []).append(clip_info)
                print(f"  Added clip to processing list: {item_name} "
                      f"(Start: {norm_start}, End: {norm_end})")

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

    # Merge overlapping clips for each media ID
    for media_id in clips:
        clips[media_id] = merge_all_overlapping(clips[media_id], connection_threshold)

    print("Merged Clips — creating flat list for sorting...")

    # Flatten into sorted list
    all_clip_infos: list[ClipInfo] = []
    for clip_infos in clips.values():
        all_clip_infos.extend(clip_infos)

    # Apply sorting
    print(f"Sorting using method: {sorting_method}")
    sort_fn = SORT_METHODS.get(sorting_method)
    if sort_fn:
        sort_fn(all_clip_infos)
    else:
        print(f"Unknown sorting method '{sorting_method}', using default (Source Name)")
        sort_by_source_name(all_clip_infos)

    # Create new timeline
    print("Adding all clips to new timeline...")
    new_timeline = media_pool.CreateEmptyTimeline(dst_timeline_name)
    assert project.SetCurrentTimeline(new_timeline), \
        "Couldn't set current timeline to the new timeline"

    # Add clips — single code path for all clip types
    append_success_count = 0
    append_error_count = 0

    for i, clip_info in enumerate(all_clip_infos):
        print(f"Adding clip #{i + 1} to timeline:")
        print(f"  Clip name: {clip_info.media_pool_item.GetName()}")
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
        try:
            media_pool.AppendToTimeline([api_dict])
            append_success_count += 1
        except Exception:
            print(f"  ERROR: Failed to add clip: {clip_info.media_pool_item.GetName()}")
            print(f"  Frame range attempted: {api_dict['startFrame']} to {api_dict['endFrame']}")
            append_error_count += 1

    print(f"Clip addition summary: {append_success_count} succeeded, "
          f"{append_error_count} failed")
    print(f"New timeline created: {dst_timeline_name}")

    # Post-processing: remove audio
    if video_only:
        print("Video Only option selected — removing all audio tracks...")
        remove_all_audio_tracks(new_timeline)

    # Post-processing: mark duplicates
    if mark_duplicates:
        print("Marking duplicates in the new timeline...")
        find_and_mark_duplicates(new_timeline)

    # Post-processing: mark retimed clips
    if mark_retimed_clips:
        print("Marking retimed clips in the new timeline...")
        clip_list = get_all_timeline_clips(new_timeline)
        marker_count = 0
        success_count = 0

        for timeline_clip in clip_list:
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

    print("Done!")


# ---------------------------------------------------------------------------
# UI Dialog
# ---------------------------------------------------------------------------

def build_and_show_ui() -> Optional[dict]:
    """Build the UI dialog and return user selections, or None if cancelled."""
    # fu and bmd are pre-injected globals in Resolve's scripting environment
    ui = fu.UIManager  # noqa: F821
    disp = bmd.UIDispatcher(ui)  # noqa: F821
    width, height = 500, 420

    win = disp.AddWindow({
        "ID": "MyWin",
        "WindowTitle": "Generate All Clips Timeline",
        "Geometry": [100, 100, width, height],
        "Spacing": 10,
    }, [
        ui.VGroup({"ID": "root"}, [
            ui.HGroup({"ID": "dst"}, [
                ui.Label({"ID": "DstLabel", "Text": "New Timeline Name"}),
                ui.TextEdit({
                    "ID": "DstTimelineName",
                    "Text": "Sources-Sequence",
                    "PlaceholderText": "Sources-Sequence",
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

    timeline_name = itm["DstTimelineName"].PlainText.strip() or "Sources-Sequence"

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
    }


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    """Script entry point: show UI, collect parameters, run workflow."""
    params = build_and_show_ui()
    if params is None:
        return
    run_workflow(**params)


main()
