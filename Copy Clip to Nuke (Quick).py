#!/usr/bin/env python
"""
Copy Clip to Nuke (Quick)
Skips the settings dialog when settings have already been configured
for the current project. Shows the full UI only on first use.

Use "Copy Clip to Nuke" (the non-Quick version) to change settings.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Load shared functions from the main script
# ---------------------------------------------------------------------------

_dir = os.path.dirname(os.path.realpath(__file__))
_main_path = os.path.join(_dir, "Copy Clip to Nuke.py")

_ns: dict = {"_NUKE_EXPORT_IMPORTED": True}
# Inject DaVinci Resolve globals into the namespace
for _g in ("resolve", "fu", "bmd"):
    if _g in globals():
        _ns[_g] = globals()[_g]

with open(_main_path, "r", encoding="utf-8") as _f:
    exec(compile(_f.read(), _main_path, "exec"), _ns)

# Pull out the functions we need
has_saved_settings = _ns["has_saved_settings"]
load_settings = _ns["load_settings"]
save_settings = _ns["save_settings"]
build_and_show_ui = _ns["build_and_show_ui"]
get_selected_clip_data = _ns["get_selected_clip_data"]
generate_nuke_python = _ns["generate_nuke_python"]
generate_nuke_tcl = _ns["generate_nuke_tcl"]
copy_to_clipboard = _ns["copy_to_clipboard"]
log_clip_info = _ns["log_clip_info"]


# ---------------------------------------------------------------------------
# Quick Entry Point
# ---------------------------------------------------------------------------

def main():
    """Quick mode: use saved settings if available, otherwise show UI."""
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

    # Check for saved settings — show UI only on first use
    if has_saved_settings(project):
        settings = load_settings(project)
        print("(Quick mode: using saved settings)")
    else:
        print("No saved settings found — opening settings dialog...")
        saved = load_settings(project)  # defaults
        settings = build_and_show_ui(saved)
        if settings is None:
            print("Cancelled by user.")
            return
        save_settings(project, settings)

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
