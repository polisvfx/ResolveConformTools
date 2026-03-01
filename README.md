# Resolve VFX Conform Helpers

A small assortment of scripts that might be helpful for everyday conform and onlining tasks. This is being develeoped from the viewpoint of a VFX artist working in the realm of car, beauty and other commericals/ads/TVCs.

## Create all Clips Timeline
This script will create a Timeline with all used clips based on selection or bin.
This script builds upon work done by [Thatcher Freeman](https://github.com/thatcherfreeman/resolve-scripts "Thatcher Freeman"), definetly check him out!
- Custom sorting
- Threshold to combine clips (eg. 25 frames would merge multiple edits of the same source that have a gap of less then 25 frames between them)
- Option to mark multiple instances of the same source
- Exclude Audio if you are only interested in the video
- Option to Include clips that are disabled on the timeline

![grafik](https://github.com/user-attachments/assets/1388dbcd-ec09-4f14-9353-a3994669dbf1)

### Python Version (Generate All Clips Timeline.py)
A full Python rewrite of the Lua script with improved retime handling and XML-based source range analysis.
- **Retime handling**: Correctly detects and handles retimed clips, reversed clips, and frame holds (freeze frames). All clips are normalized to forward-playing source ranges on the master timeline.
- **XML retime analysis**: Optionally exports the source timeline to FCP 7 XML and parses Time Remap keyframes to compute precise source frame ranges — particularly useful for speed ramps and non-linear retimes where the API-reported range may be inaccurate.
- **Distinct retime markers**: Red markers distinguish between "Frame Hold", "Non-Linear Retime", and "Retimed Clip" (with speed percentage), with a note when the source was originally reversed.
- **Clean API calls**: Only API-recognized fields are passed to `AppendToTimeline`, preventing silent failures caused by extra metadata.
- All features from the Lua version (sorting, merging, duplicate marking, audio removal) are fully preserved.

## Shot Naming
The script will insert custom numbering into the "Shot" Metadata field. A shortcoming of Resolve is that theres no API access to set any custom timeline based values to a clip/event that can also be read via Tokens (eg. on the Deliver Page) so we are stuck with setting this data on a global/media bin level. This is problematic if you deal with source material that is used multiple times as the unique numbering can only be applied once and not for each instance.
For each additional instance of a source clip, resolve will skip a count and add a marker to the clip in question containing the would be number.

Once the script has run its course you could batch rename the clip events by selecting them and entering the Clip Attributes. There you would use any naming of your choice in combination with the %Shot token.

If you are only using trims, duplicates shouldnt be of concern.

![grafik](https://github.com/user-attachments/assets/46afb03e-5933-418f-8643-7c8608643081)

### Shot Numbering - Clip Name (Resolve 20.2+)
An alternative approach that uses the `TimelineItem:SetName()` API introduced in DaVinci Resolve 20.2 to apply sequential shot numbers directly to the clip name on the timeline. Unlike the metadata-based version, each timeline instance is renamed independently — duplicate source clips are not an issue.
- Configurable prefix (default: `SH_`), padding, and increment
- Option to append the original clip name as a suffix
- Restore button to revert all timeline clips back to their original Media Pool names
- Does not modify the source Media Pool item


## Copy Clip to Nuke (Python)
A unified Python rewrite that replaces both the "Copy to Nuke Simple" and "Copy Clip and Settings to Nuke Python" Lua scripts. Copies the selected clip's file path, editorial data, and metadata to the clipboard in a Nuke-ready format.

Features a UI dialog with configurable settings:
- **Output Mode**: Python (Script Editor) or TCL (Node Graph Paste)
- **Handles**: Configurable frame handles (default 12)
- **Colorspace**: Dropdown presets (ARRI LogC4, LogC3, REDLog3G10, S-Log3, ACEScg, etc.) with a free-text field for custom values
- **Format Name**: Name for the Nuke format entry (default "Plate")
- **Set Project Settings**: Optionally sets Nuke project format, FPS, and frame range (Python mode only)
- **Clear Existing Nodes**: Optional destructive clear of all nodes before setup (off by default)

Both modes create: Read node -> ModifyMetaData (reel name) -> ShotSetup group (frame range management, TimeOffset to rebase to frame 1001 +/- handles).

### Legacy Lua Scripts (Deprecated)
The original Lua scripts (`Copy Clip to Nuke Simple.lua` and `Copy Clip and Settings to Nuke Python.lua`) are still included but deprecated in favour of the unified Python version above.


## Future Features
The nuke integration is in its infancy and I am contemplating making it a bit more robust and useful, maybe creating a script that will generate a project config file that handles all variables like, project resolution, handles and so on. You could theoretically also reference nukescript templates and generate nuke scripts out of Resolve.
