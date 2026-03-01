#!/usr/bin/env python
"""
Copy Clip to Nuke
Copies the selected DaVinci Resolve timeline clip's file path, editorial data,
and metadata into a Nuke-ready format on the clipboard.

Two output modes:
  - Python (Script Editor): Full setup including Nuke project settings
    (format, FPS, frame range). Paste into Nuke's Script Editor and execute.
  - TCL (Node Graph Paste): Nuke node paste format. Paste directly into
    the Node Graph with Ctrl+V / Cmd+V.

Both modes create: Read -> ModifyMetaData -> ShotSetup group.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HANDLES = 12
DEFAULT_COLORSPACE = "ARRI LogC4"
DEFAULT_FORMAT_NAME = "Plate"
DEFAULT_START_FRAME = 1001

COLORSPACE_PRESETS = [
    "ARRI LogC4",
    "ARRI LogC3 (EI800)",
    "REDLog3G10 / REDWideGamutRGB",
    "S-Log3 / S-Gamut3.Cine",
    "ACEScg",
    "linear",
    "sRGB",
]


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ClipData:
    """All data extracted from the selected DaVinci Resolve timeline item."""
    file_path: str
    nuke_path: str
    reel_name: str
    width: int
    height: int
    fps: float
    first_frame: int
    last_frame: int
    start_frame: int
    end_frame: int
    start_tc: int
    end_tc: int


@dataclass
class NukeSettings:
    """User-configurable settings from the UI dialog."""
    handles: int = DEFAULT_HANDLES
    colorspace: str = DEFAULT_COLORSPACE
    format_name: str = DEFAULT_FORMAT_NAME
    clear_existing_nodes: bool = False
    set_project_settings: bool = True
    output_mode: str = "Python"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def format_timecode(frames: int, fps: float) -> str:
    """Convert a frame count to HH:MM:SS:FF timecode string."""
    fps_int = max(1, int(round(fps)))
    total_seconds = frames // fps_int
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    remaining_frames = frames % fps_int
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{remaining_frames:02d}"


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard. Returns True on success."""
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)

            if os.name == "nt":
                subprocess.run(
                    f'type "{tmp_path}" | clip',
                    shell=True,
                    check=True,
                )
            elif sys.platform == "darwin":
                subprocess.run(
                    f'cat "{tmp_path}" | pbcopy',
                    shell=True,
                    check=True,
                )
            else:
                subprocess.run(
                    f'cat "{tmp_path}" | xclip -selection clipboard',
                    shell=True,
                    check=True,
                )
            return True
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    except Exception as e:
        print(f"Error copying to clipboard: {e}")
        return False


# ---------------------------------------------------------------------------
# Resolve Data Extraction
# ---------------------------------------------------------------------------

def get_selected_clip_data(timeline, selected_item) -> Optional[ClipData]:
    """Extract all clip data from the selected timeline item.

    Returns None if the item has no media pool item (e.g. generators,
    compound clips) or if required properties cannot be read.
    """
    media_pool_item = selected_item.GetMediaPoolItem()
    if media_pool_item is None:
        print("Error: Selected item has no media pool item "
              "(generators and compound clips are not supported).")
        return None

    file_path = media_pool_item.GetClipProperty("File Path")
    if not file_path:
        print("Error: Could not read File Path from the selected clip.")
        return None

    # Convert backslashes to forward slashes for Nuke
    nuke_path = file_path.replace("\\", "/")

    # -- Reel name ----------------------------------------------------------
    reel_name = media_pool_item.GetClipProperty("Reel Name") or ""
    if not reel_name:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        m = re.match(r"([A-Z]\d+[A-Z]\d+_\d+_[A-Z]+)", base_name)
        reel_name = m.group(1) if m else base_name

    # -- Resolution ---------------------------------------------------------
    width, height = 1920, 1080
    res_x = media_pool_item.GetClipProperty("Resolution X")
    res_y = media_pool_item.GetClipProperty("Resolution Y")
    if res_x and res_y and str(res_x).strip() and str(res_y).strip():
        try:
            width = int(res_x)
            height = int(res_y)
        except (ValueError, TypeError):
            pass
    else:
        resolution = media_pool_item.GetClipProperty("Resolution")
        if resolution:
            m = re.match(r"(\d+)\s*x\s*(\d+)", str(resolution))
            if m:
                width, height = int(m.group(1)), int(m.group(2))
        print(f"  Using default resolution: {width}x{height}")

    # -- FPS ----------------------------------------------------------------
    clip_fps = 24.0
    fps_val = media_pool_item.GetClipProperty("FPS")
    if fps_val and str(fps_val).strip():
        try:
            clip_fps = float(fps_val)
        except (ValueError, TypeError):
            pass
    else:
        timeline_fps = timeline.GetSetting("timelineFrameRate")
        if timeline_fps and str(timeline_fps).strip():
            try:
                clip_fps = float(timeline_fps)
            except (ValueError, TypeError):
                pass
        print(f"  Using FPS: {clip_fps}")

    # -- Frame range --------------------------------------------------------
    first_frame = 1
    last_frame: Optional[int] = None

    frame_match = re.search(r"\[(\d+)-(\d+)\]", nuke_path)
    if frame_match:
        first_frame = int(frame_match.group(1))
        last_frame = int(frame_match.group(2))
        nuke_path = re.sub(r"\[\d+-\d+\]", "####", nuke_path)
    else:
        for prop in ("Frames", "Full Duration", "Duration"):
            val = media_pool_item.GetClipProperty(prop)
            if val:
                try:
                    last_frame = int(val)
                    break
                except (ValueError, TypeError):
                    continue

        if last_frame is None:
            last_frame = 1000
            print("  Warning: Could not determine clip length. Using default.")

    # -- Editorial frames ---------------------------------------------------
    start_tc = selected_item.GetStart()
    end_tc = selected_item.GetEnd()
    start_frame = selected_item.GetLeftOffset()
    end_frame = start_frame + (end_tc - start_tc)

    return ClipData(
        file_path=file_path,
        nuke_path=nuke_path,
        reel_name=reel_name,
        width=width,
        height=height,
        fps=clip_fps,
        first_frame=first_frame,
        last_frame=last_frame,
        start_frame=start_frame,
        end_frame=end_frame,
        start_tc=start_tc,
        end_tc=end_tc,
    )


# ---------------------------------------------------------------------------
# Nuke Python Generator
# ---------------------------------------------------------------------------

def generate_nuke_python(clip: ClipData, settings: NukeSettings) -> str:
    """Generate Nuke Python code for the Script Editor."""
    sections: list[str] = []

    sections.append("import nuke\n")

    # Optional: clear existing nodes
    if settings.clear_existing_nodes:
        sections.append(
            "# Clear existing nodes\n"
            "for n in nuke.allNodes():\n"
            "    nuke.delete(n)\n"
        )

    # Optional: project settings
    if settings.set_project_settings:
        clip_length = clip.end_frame - clip.start_frame
        first = DEFAULT_START_FRAME - settings.handles
        last = DEFAULT_START_FRAME + clip_length + settings.handles

        sections.append(f"""\
def modify_project_settings():
    print("Modifying project settings...")

    # Set frame range
    nuke.root()["first_frame"].setValue({first})
    print("Set first_frame to {first}")

    nuke.root()["last_frame"].setValue({last})
    print("Set last_frame to {last}")

    # Set FPS
    nuke.root()["fps"].setValue({clip.fps})
    print("Set fps to {clip.fps}")

    # Create and set format
    try:
        format_string = "{clip.width} {clip.height} {settings.format_name}"
        nuke.addFormat(format_string)
        nuke.root()["format"].setValue("{settings.format_name}")
        print("Set project format to {settings.format_name}")
    except Exception as e:
        print(f"Error setting format: {{e}}")

    print("Project settings modification completed")

modify_project_settings()
""")

    # Read node
    sections.append(f"""\
# Create Read node
read = nuke.createNode("Read")
read["file"].setValue("{clip.nuke_path}")
read["first"].setValue({clip.first_frame})
read["last"].setValue({clip.last_frame})
read["origfirst"].setValue({clip.first_frame})
read["origlast"].setValue({clip.last_frame})
read["colorspace"].setValue("{settings.colorspace}")
read["name"].setValue("ReadFromResolve1")
""")

    # ModifyMetaData node
    sections.append(f"""\
# Create ModifyMetaData node
try:
    metadata_node = nuke.createNode("ModifyMetaData")
    metadata_node.setName("ModifyMetaData1")
    metadata_str = "{{set exr/reelName {clip.reel_name}}}\\n{{set exr/owner {clip.reel_name}}}"
    metadata_node["metadata"].fromScript(metadata_str)
    print("Created ModifyMetaData node")
except Exception as e:
    print(f"Error creating ModifyMetaData node: {{e}}")
""")

    # ShotSetup group
    sections.append(f"""\
# Create ShotSetup group
group = nuke.createNode("Group")
group.setName("ShotSetup")

# Connect node chain
read.setInput(0, None)
metadata_node.setInput(0, read)
group.setInput(0, metadata_node)

# Build group internals
group.begin()

# Group knobs
tab = nuke.Tab_Knob("Shotdata", "Shot Settings")
group.addKnob(tab)

length_from_input = nuke.Boolean_Knob("length_from_input", "Input Length")
group.addKnob(length_from_input)

inpoint = nuke.Int_Knob("inpoint", "")
inpoint.setTooltip("Source first frame.")
group.addKnob(inpoint)
inpoint.setExpression("[value length_from_input] > 0 ? first_frame : Input1.firstframe")

outpoint = nuke.Int_Knob("outpoint", "")
group.addKnob(outpoint)
outpoint.setExpression("[value length_from_input] > 0 ? last_frame : Input1.lastframe")

firstframe_link = nuke.Link_Knob("firstframe_1", "First Frame")
firstframe_link.setLink("Input1.firstframe")
group.addKnob(firstframe_link)

lastframe_link = nuke.Link_Knob("lastframe", "Last Frame")
lastframe_link.setLink("Input1.lastframe")
group.addKnob(lastframe_link)

handles_link = nuke.Link_Knob("handles", "Included Handles")
handles_link.setLink("Input1.handles")
group.addKnob(handles_link)

addhandles_link = nuke.Link_Knob("addhandles", "Add Handles")
addhandles_link.setLink("Input1.addhandles")
group.addKnob(addhandles_link)

metadatafilter_link = nuke.Link_Knob("metadatafilter", "search metadata for")
metadatafilter_link.setLink("ViewMetaData1.metadatafilter")
group.addKnob(metadatafilter_link)

# Input node
input_node = nuke.createNode("Input")
input_node.setName("Input1")

tab_input = nuke.Tab_Knob("Shotdata", "Shot Settings")
input_node.addKnob(tab_input)

firstframe_knob = nuke.Int_Knob("firstframe", "First Frame")
input_node.addKnob(firstframe_knob)
input_node["firstframe"].setValue({clip.start_frame})

lastframe_knob = nuke.Int_Knob("lastframe", "Last Frame")
input_node.addKnob(lastframe_knob)
input_node["lastframe"].setValue({clip.end_frame})

handles_knob = nuke.Int_Knob("handles", "Included Handles")
input_node.addKnob(handles_knob)

addhandles_knob = nuke.Int_Knob("addhandles", "Add Handles")
input_node.addKnob(addhandles_knob)
input_node["addhandles"].setValue({settings.handles})

# FrameRange nodes
original_range = nuke.createNode("FrameRange")
original_range.setName("OriginalFramerange")
original_range["first_frame"].setExpression("parent.Input1.first_frame")
original_range["last_frame"].setExpression("parent.Input1.last_frame")
original_range["disable"].setExpression("!parent.length_from_input")

custom_range = nuke.createNode("FrameRange")
custom_range.setName("CustomFramerange")
custom_range["first_frame"].setExpression("parent.Input1.firstframe+1")
custom_range["last_frame"].setExpression("parent.Input1.lastframe")
custom_range["disable"].setExpression("parent.length_from_input")

# TimeOffset node
time_offset = nuke.createNode("TimeOffset")
time_offset.setName("TimeOffset")
time_offset["time_offset"].setExpression("(parent.length_from_input < 1 ? -parent.CustomFramerange.knob.first_frame+1001 : -parent.OriginalFramerange.knob.first_frame+1001)-Input1.handles")

setup_tab = nuke.Tab_Knob("setup", "Setup")
time_offset.addKnob(setup_tab)
handles_to = nuke.Int_Knob("handles", "Handles")
time_offset.addKnob(handles_to)
time_offset["handles"].setExpression("parent.Input1.handles")

# Target FrameRange
target_range = nuke.createNode("FrameRange")
target_range.setName("TargetRange")
target_range["first_frame"].setExpression("input.first_frame-Input1.addhandles")
target_range["last_frame"].setExpression("input.last_frame+Input1.addhandles")

# ViewMetaData
view_metadata = nuke.createNode("ViewMetaData")
view_metadata.setName("ViewMetaData1")
view_metadata["metadatafilter"].setValue("timecode")

# Output
output = nuke.createNode("Output")
output.setName("Output1")

group.end()
""")

    # Final arrangement
    sections.append("""\
# Arrange nodes
nuke.autoplace_all()

# Close all node panels
for node in nuke.allNodes():
    try:
        node.hideControlPanel()
    except Exception:
        pass

print("Nuke setup created successfully")
""")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Nuke TCL Generator
# ---------------------------------------------------------------------------

def generate_nuke_tcl(clip: ClipData, settings: NukeSettings) -> str:
    """Generate Nuke TCL paste format for direct Node Graph paste."""
    lines: list[str] = []

    lines.append("set cut_paste_input [stack 0]")

    # Read node
    lines.append("Read {")
    lines.append(" inputs 0")
    lines.append(f' file "{clip.nuke_path}"')
    lines.append(f" first {clip.first_frame}")
    lines.append(f" last {clip.last_frame}")
    lines.append(f" origlast {clip.last_frame}")
    lines.append(" origset true")
    lines.append(f' colorspace "{settings.colorspace}"')
    lines.append(" name ReadFromResolve1")
    lines.append(" selected true")
    lines.append(" xpos 0")
    lines.append(" ypos 0")
    lines.append("}")

    # ModifyMetaData node
    lines.append("ModifyMetaData {")
    lines.append(" metadata {")
    lines.append(f"  {{set exr/reelName {clip.reel_name}}}")
    lines.append(f"  {{set exr/owner {clip.reel_name}}}")
    lines.append(" }")
    lines.append(" name ModifyMetaData1")
    lines.append(" xpos 0")
    lines.append(" ypos 100")
    lines.append("}")

    # ShotSetup group
    lines.append("Group {")
    lines.append(" name ShotSetup")
    lines.append(" selected true")
    lines.append(' addUserKnob {20 Shotdata l "Shot Settings"}')
    lines.append(' addUserKnob {6 length_from_input l "Input Length" +STARTLINE}')
    lines.append(' addUserKnob {3 inpoint l "" t "Source first frame." -STARTLINE}')
    lines.append(' inpoint {{"\\[value length_from_input] > 0 ? first_frame : Input1.firstframe"}}')
    lines.append(' addUserKnob {3 outpoint l "" -STARTLINE}')
    lines.append(' outpoint {{"\\[value length_from_input] > 0 ? last_frame : Input1.lastframe"}}')
    lines.append(' addUserKnob {41 firstframe_1 l "First Frame" T Input1.firstframe}')
    lines.append(' addUserKnob {41 lastframe l "Last Frame" T Input1.lastframe}')
    lines.append(' addUserKnob {41 handles l "Included Handles" T Input1.handles}')
    lines.append(' addUserKnob {41 addhandles l "Add Handles" T Input1.addhandles}')
    lines.append(' addUserKnob {41 shownmetadata l "" +STARTLINE T ViewMetaData1.shownmetadata}')
    lines.append(' addUserKnob {41 metadatafilter l "search metadata for" T ViewMetaData1.metadatafilter}')
    lines.append(" xpos 0")
    lines.append(" ypos 200")
    lines.append("}")

    # Input node (inside group)
    lines.append(" Input {")
    lines.append("  inputs 0")
    lines.append("  name Input1")
    lines.append("  xpos -29")
    lines.append('  addUserKnob {20 Shotdata l "Shot Settings"}')
    lines.append('  addUserKnob {3 firstframe l "First Frame"}')
    lines.append(f"  firstframe {clip.start_frame}")
    lines.append('  addUserKnob {3 lastframe l "Last Frame" -STARTLINE}')
    lines.append(f"  lastframe {clip.end_frame}")
    lines.append('  addUserKnob {3 handles l "Included Handles"}')
    lines.append('  addUserKnob {3 addhandles l "Add Handles"}')
    lines.append(f"  addhandles {settings.handles}")
    lines.append(" }")

    # OriginalFramerange
    lines.append(" FrameRange {")
    lines.append("  first_frame {{parent.Input1.first_frame}}")
    lines.append("  last_frame {{parent.Input1.last_frame}}")
    lines.append('  time ""')
    lines.append("  name OriginalFramerange")
    lines.append("  xpos -29")
    lines.append("  ypos 56")
    lines.append("  disable {{!parent.length_from_input}}")
    lines.append(" }")

    # CustomFramerange
    lines.append(" FrameRange {")
    lines.append("  first_frame {{parent.Input1.firstframe+1}}")
    lines.append("  last_frame {{parent.Input1.lastframe}}")
    lines.append('  time ""')
    lines.append("  name CustomFramerange")
    lines.append("  xpos -29")
    lines.append("  ypos 101")
    lines.append("  disable {{parent.length_from_input}}")
    lines.append(" }")

    # TimeOffset
    lines.append(" TimeOffset {")
    lines.append('  time_offset {{"(parent.length_from_input < 1 ? -parent.CustomFramerange.knob.first_frame+1001 : -parent.OriginalFramerange.knob.first_frame+1001)-Input1.handles"}}')
    lines.append('  time ""')
    lines.append("  name TimeOffset")
    lines.append("  selected true")
    lines.append("  xpos -29")
    lines.append("  ypos 160")
    lines.append("  addUserKnob {20 setup l Setup}")
    lines.append("  addUserKnob {3 handles l Handles}")
    lines.append("  handles {{parent.Input1.handles}}")
    lines.append(" }")

    # TargetRange
    lines.append(" FrameRange {")
    lines.append("  first_frame {{input.first_frame-Input1.addhandles}}")
    lines.append("  last_frame {{input.last_frame+Input1.addhandles}}")
    lines.append('  time ""')
    lines.append("  name TargetRange")
    lines.append("  xpos -29")
    lines.append("  ypos 222")
    lines.append(" }")

    # ViewMetaData
    lines.append(" ViewMetaData {")
    lines.append("  metadatafilter timecode")
    lines.append("  name ViewMetaData1")
    lines.append("  xpos -29")
    lines.append("  ypos 298")
    lines.append(" }")

    # Output
    lines.append(" Output {")
    lines.append("  name Output1")
    lines.append("  xpos -29")
    lines.append("  ypos 361")
    lines.append(" }")
    lines.append("end_group")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Console Logging
# ---------------------------------------------------------------------------

def log_clip_info(clip: ClipData, settings: NukeSettings) -> None:
    """Print clip information to the DaVinci Resolve console."""
    print("Selected Clip Information:")
    print(f"  Filepath: {clip.file_path}")
    print(f"  Reel Name: {clip.reel_name}")
    print(f"  Resolution: {clip.width}x{clip.height}")
    print(f"  FPS: {clip.fps}")
    print(f"  Cut In TC: {format_timecode(clip.start_tc, clip.fps)}")
    print(f"  Cut Out TC: {format_timecode(clip.end_tc, clip.fps)}")
    print(f"  Start Frame: {clip.start_frame}")
    print(f"  End Frame: {clip.end_frame}")
    print(f"  Sequence First Frame: {clip.first_frame}")
    print(f"  Sequence Last Frame: {clip.last_frame}")
    print(f"  Handles: {settings.handles}")
    print(f"  Colorspace: {settings.colorspace}")
    print(f"  Output Mode: {settings.output_mode}")


# ---------------------------------------------------------------------------
# UI Dialog
# ---------------------------------------------------------------------------

def build_and_show_ui() -> Optional[NukeSettings]:
    """Build the Fusion UI dialog and return user settings, or None if cancelled."""
    ui = fu.UIManager  # noqa: F821
    disp = bmd.UIDispatcher(ui)  # noqa: F821
    width, height = 450, 380

    win = disp.AddWindow({
        "ID": "NukeExportWin",
        "WindowTitle": "Copy Clip to Nuke",
        "Geometry": [100, 100, width, height],
        "Spacing": 10,
    }, [
        ui.VGroup({"ID": "root"}, [
            ui.HGroup({}, [
                ui.Label({"ID": "OutputModeLabel", "Text": "Output Mode"}),
                ui.ComboBox({"ID": "OutputMode"}),
            ]),
            ui.HGroup({}, [
                ui.Label({"ID": "HandlesLabel", "Text": "Handles (frames)"}),
                ui.TextEdit({
                    "ID": "Handles",
                    "Text": str(DEFAULT_HANDLES),
                    "PlaceholderText": str(DEFAULT_HANDLES),
                }),
            ]),
            ui.HGroup({}, [
                ui.Label({"ID": "ColorspaceLabel", "Text": "Colorspace Preset"}),
                ui.ComboBox({"ID": "ColorspacePreset"}),
            ]),
            ui.HGroup({}, [
                ui.Label({"ID": "ColorspaceCustomLabel", "Text": "Colorspace"}),
                ui.TextEdit({
                    "ID": "ColorspaceCustom",
                    "Text": DEFAULT_COLORSPACE,
                    "PlaceholderText": DEFAULT_COLORSPACE,
                }),
            ]),
            ui.HGroup({}, [
                ui.Label({"ID": "FormatNameLabel", "Text": "Format Name"}),
                ui.TextEdit({
                    "ID": "FormatName",
                    "Text": DEFAULT_FORMAT_NAME,
                    "PlaceholderText": DEFAULT_FORMAT_NAME,
                }),
            ]),
            ui.CheckBox({
                "ID": "ClearNodes",
                "Text": "Clear existing nodes (destructive!)",
                "Checked": False,
            }),
            ui.CheckBox({
                "ID": "SetProjectSettings",
                "Text": "Set Nuke project settings (format, FPS, frame range)",
                "Checked": True,
            }),
            ui.HGroup({"ID": "buttons"}, [
                ui.Button({"ID": "cancelButton", "Text": "Cancel"}),
                ui.Button({"ID": "goButton", "Text": "Copy to Clipboard"}),
            ]),
        ]),
    ])

    run_export = {"value": False}

    def on_close(ev):
        disp.ExitLoop()
        run_export["value"] = False

    def on_cancel(ev):
        disp.ExitLoop()
        run_export["value"] = False

    def on_go(ev):
        disp.ExitLoop()
        run_export["value"] = True

    def on_colorspace_preset_changed(ev):
        preset = itm["ColorspacePreset"].CurrentText
        if preset:
            itm["ColorspaceCustom"].Text = preset

    def on_output_mode_changed(ev):
        is_python = itm["OutputMode"].CurrentIndex == 0
        itm["SetProjectSettings"].Checked = is_python
        itm["SetProjectSettings"].Enabled = is_python

    win.On.NukeExportWin.Close = on_close
    win.On.cancelButton.Clicked = on_cancel
    win.On.goButton.Clicked = on_go
    win.On.ColorspacePreset.CurrentIndexChanged = on_colorspace_preset_changed
    win.On.OutputMode.CurrentIndexChanged = on_output_mode_changed

    itm = win.GetItems()

    # Populate combo boxes
    itm["OutputMode"].AddItem("Python (Script Editor)")
    itm["OutputMode"].AddItem("TCL (Node Graph Paste)")

    for cs in COLORSPACE_PRESETS:
        itm["ColorspacePreset"].AddItem(cs)

    win.Show()
    disp.RunLoop()
    win.Hide()

    if not run_export["value"]:
        return None

    # Parse handles
    try:
        handles = int(itm["Handles"].PlainText)
    except (ValueError, TypeError):
        handles = DEFAULT_HANDLES

    colorspace = itm["ColorspaceCustom"].PlainText.strip() or DEFAULT_COLORSPACE
    format_name = itm["FormatName"].PlainText.strip() or DEFAULT_FORMAT_NAME
    output_mode = "Python" if itm["OutputMode"].CurrentIndex == 0 else "TCL"

    return NukeSettings(
        handles=handles,
        colorspace=colorspace,
        format_name=format_name,
        clear_existing_nodes=itm["ClearNodes"].Checked,
        set_project_settings=itm["SetProjectSettings"].Checked,
        output_mode=output_mode,
    )


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    """Script entry point: show UI, extract clip data, generate and copy Nuke code."""
    project = resolve.GetProjectManager().GetCurrentProject()  # noqa: F821
    timeline = project.GetCurrentTimeline()

    if not timeline:
        print("Error: No timeline is active. Please open a timeline.")
        return

    selected_item = timeline.GetCurrentVideoItem()
    if not selected_item:
        print("Error: No clip is currently selected. "
              "Please select a clip in the timeline.")
        return

    # Show UI dialog
    settings = build_and_show_ui()
    if settings is None:
        print("Cancelled by user.")
        return

    # Extract clip data
    clip = get_selected_clip_data(timeline, selected_item)
    if clip is None:
        return

    # Generate output
    if settings.output_mode == "Python":
        output = generate_nuke_python(clip, settings)
        output_label = "NUKE PYTHON CODE"
    else:
        output = generate_nuke_tcl(clip, settings)
        output_label = "NUKE TCL NODE SETUP"

    # Copy to clipboard
    if copy_to_clipboard(output):
        print(f"\n{'=' * 45}")
        print(f"{output_label} (Copied to clipboard)")
        print(f"{'=' * 45}\n")
    else:
        print("WARNING: Failed to copy to clipboard. Output printed below:")

    # Log clip info and output
    log_clip_info(clip, settings)
    print(f"\n{output}")
    print(f"\n{'=' * 45}")


main()
