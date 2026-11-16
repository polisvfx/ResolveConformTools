# Resolve VFX Conform Helpers

A small assortment of scripts that might be helpful for everyday conform and onlining tasks. This is being develeoped from the viewpoint of a VFX artist working in the realm of car, beauty and other commericals/ads/TVCs.

## Install

Grab the latest `ResolveConformTools-X.Y.Z.zip` from the
[Releases page](https://github.com/polisvfx/ResolveConformTools/releases) and unzip it
into Resolve's Utility scripts folder, so the scripts land in a `ResolveConformTools`
subfolder:

| | |
|---|---|
| Windows | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility` |
| macOS | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility` |
| Linux | `/opt/resolve/Fusion/Scripts/Utility` |

They then show up under **Workspace > Scripts > ResolveConformTools**. The zip carries
a `VERSIONS.txt` listing the version of every script in it.

Cloning the repo into that same folder works just as well — it only additionally brings
the test suite, which does nothing inside Resolve.

## Create all Clips Timeline
This script will create a Timeline with all used clips based on selection or bin.
This script builds upon work done by [Thatcher Freeman](https://github.com/thatcherfreeman/resolve-scripts "Thatcher Freeman"), definetly check him out!
- Custom sorting
- Threshold to combine clips (eg. 25 frames would merge multiple edits of the same source that have a gap of less then 25 frames between them)
- Option to mark multiple instances of the same source
- Exclude Audio if you are only interested in the video
- Option to Include clips that are disabled on the timeline

![grafik](https://github.com/user-attachments/assets/1388dbcd-ec09-4f14-9353-a3994669dbf1)

### Python Version (Generate All Clips Timeline PRO.py)
A full Python rewrite of the Lua script with improved retime handling and XML-based source range analysis.
- **Retime handling**: Correctly detects and handles retimed clips, reversed clips, and frame holds (freeze frames). All clips are normalized to forward-playing source ranges on the master timeline.
- **XML retime analysis (2.8+)**: Optionally exports the source timeline to FCP 7 XML and reads the Time Remap curve, then widens each clip's source range to every frame its ramp actually touches. This matters because `GetSourceStartFrame`/`GetSourceEndFrame` can only express an in and an out, so a **negative speed ramp — one that runs past its out-point and reverses back — is under-reported by the API**, and those frames would be silently missing from a pull. Measured on a real ramp: the API reported source 0-3119 while the curve reached 8750, so 5631 frames were recovered. The range is scoped to the clip's own window into the curve and unioned with the API range, so it only ever widens. Before 2.8 this pass detected nothing at all — see the release notes for that history.
- **Distinct retime markers**: Red markers distinguish between "Frame Hold", "Non-Linear Retime", "Retimed Clip" (with speed percentage) and "Extreme Retime", with a note when the source was originally reversed. A whip past 400% gets the last of those: the percentage stops meaning anything at that speed, so the marker reports the source span instead (`31 source frames (1717-1747) played over roughly 5 timeline frames`).
- **Transition handles (3.1+)**: A dissolve needs source frames beyond the cut, but the API's in and out describe only the cut. Each shot's range now includes the frames its transitions need, read from where each transition actually sits, so centred, start-on-edit and end-on-edit transitions all work: a 66-frame centred dissolve adds 33 frames to both shots it joins. Retimed shots are scaled by their speed, and a handle that runs past the start or end of the media is clamped and logged. Before 3.1 shots with dissolves came out too short to re-conform them. Update mode will now see such shots on older timelines as extended and grow them to match.
- **Clean API calls**: Only API-recognized fields are passed to `AppendToTimeline`, preventing silent failures caused by extra metadata.
- **Complete source ranges (2.3+)**: `AppendToTimeline`'s `endFrame` is *exclusive* — asking for `endFrame=E` places frames up to `E-1`. Every clip is now requested one frame past its last frame, so the range that lands is the range that was asked for, and freeze frames (a one-frame range, previously refused as zero-length) place correctly.
- All features from the Lua version (sorting, merging, duplicate marking, audio removal) are fully preserved.

#### Update Timeline mode (2.0+)

Update mode works on All Clips timelines created by **version 2.5 or later**,
which record their sources as they are built. Timelines from 2.0–2.4 were built
before create mode stamped anything, so they carry no record and have to be
adopted once (see *Where the state lives* below) or simply regenerated.

Open an All Clips timeline, set **Mode** to *Update Existing Timeline*, and the
script re-reads the source timelines and reconciles what is already there
against what they say now:

- shots whose source range grew or shrank are brought to the newest state,
- genuinely new shots are appended on a **new video track per run**, named for
  the run (`Update 3 - 2026-08-06`), starting after the existing content,
- shots that no longer appear in any source are **marked, never removed**,
- every shot it touches gets a marker with a timestamp and a short changelog,
  e.g. `2026-08-06 14:22 | run 3 | head +20f, tail +40f | 100-200 -> 80-240`,
  accumulating up to eight entries before the tail is summarised.

Marker colours: green extended, yellow shortened, sand both ends, mint new,
purple superseded, rose no longer used, fuchsia needs manual attention. A Sky
marker on the timeline ruler summarises each run.

**Run a dry run first.** Resolve's undo stack is not scriptable, so an update
cannot be undone in one step. Dry run prints the whole plan and writes nothing.

##### What it costs, and why

**On Resolve 21.1.2 and later (PRO 3.0+), a changed clip is trimmed in place.**
It stays the same clip, so its grade, colour versions, Fusion comps and markers
are all kept, and linked audio is trimmed along with it. Verified on 21.2.0.6.

A clip is only trimmed where it has room. Resolve does not refuse a trim that
runs into the next clip: it reports success and cuts into that clip. So the
script checks for free space on the clip's track, and on its linked audio track,
before every trim. Shortening always fits.

**On a gapless timeline a clip has nowhere to grow, so room is made for it.**
Either way it is still the same clip, with its grade, and there is only ever one
version of the shot. *Update only: make room by* chooses how:

- **Shift later clips (3.4+, the default).** Every clip after the shot, on every
  video and audio track, is pushed right by exactly the frames the shot needs,
  and the shot is lengthened into the gap. Shots stay on their own tracks and in
  the same order, and clips stacked above one another stay stacked. The cost is
  that everything after the shot changes timecode. Because all tracks move
  together, a shot growing on one track leaves a gap of that size on the others.
  Timeline ruler markers do not move with clips, so the run markers of earlier
  updates may no longer sit over the clips they were placed at. A timeline that
  has transitions on it is not shifted; its clips are moved up a track instead.
- **Move the clip up a track (3.3+).** Only that shot moves, to the lowest track
  above it that is free over the span it needs, or to a new track named for the
  run. Its kept frames stay at the same timeline position, so it sits directly
  above the gap it left, and nothing else changes position. The marker on the
  clip says which track it moved to.

In both, a shot at the very start of the timeline whose head grows never ends up
starting before the timeline does.

A full-range copy on the update track, with the original marked superseded, is
now only the fallback for a clip that can be neither trimmed nor moved.

Anything that cannot be trimmed falls back to the older behaviour: the clip is
deleted and re-appended, and **a rebuilt clip loses its grade and its Fusion
comps** — there is no way to read a grade back out. That happens on Resolve
builds before 21.1.2, for retimed clips (their handles are not counted in source
frames), and when Resolve refuses a trim. Its name, clip colour, flags, enabled
state and your own markers *are* restored. On that path, clips carrying a Fusion
comp or more than one colour version are skipped by default and flagged for
manual attention; untick *skip clips with Fusion comps* to rebuild them anyway.

Other things to know:

- **Growing and shrinking are not treated alike.** A shot is updated once it
  needs more than 3 frames it does not have, but only once it is carrying more
  than 12 surplus frames. Missing frames break a pull; surplus frames are just
  handle, and on the rebuild path removing a few of them would cost that clip
  its grade for nothing. Both are constants near the top of the script
  (`UPDATE_CHANGE_TOLERANCE`, `UPDATE_SHRINK_TOLERANCE`) if your handles differ.
- **The tool owns source ranges.** A clip you trimmed by hand — by more than
  those thresholds — reads as changed and is put back to the collected range.
  Every such rebuild is marked.
- **Video only.** A clip with linked audio is trimmed together with its audio
  when both tracks have room. Moving up a track helps only the video: the audio
  stays on its own track and still needs room there. When it would need a
  rebuild or an update-track copy instead, it is skipped and marked for manual
  attention: deleting the
  video item would orphan the audio. *Video Only* on, the default, avoids that.
- **The connection threshold matters between runs.** It decides which source
  edits merge into one range, so changing it makes nearly everything read as
  changed. The value used is stored and offered back to you; the script warns
  if you change it.
- **Preserve Source Track Layout** is a create-time layout and is not available
  in update mode; a timeline built with it is reconciled by clip identity only.

##### Where the state lives

The source timelines and settings are stamped onto the timeline in two places —
third-party metadata on the timeline's Media Pool item, and a `Cream` marker at
the start of the timeline whose custom data holds the same JSON. The marker
doubles as a visible "this timeline is managed" badge. Both are written by
create mode as the timeline is built, and refreshed by every update. Which clips
are on the timeline is deliberately *not* stored: it is re-scanned every run, so
anything you rearrange between runs is respected rather than overwritten.

A timeline with no such record can be **adopted**: choose *Current selection*
(or *Recorded + current selection*) as the update source, and the timelines you
have selected in the Media Pool become its recorded sources. The script never
guesses them.

##### When a new cut arrives as a new timeline

Edit changes usually turn up as `..._V2` rather than as an edit to `..._V1`, so
the source set has to change. Pick the mode by what you mean:

- ***Current selection*** **replaces** the recorded sources with what you have
  selected. This is the one for a version bump: shots only in V1 are marked *no
  longer used*, shots new in V2 are added, shots in both are left alone.
  Select **every source that is still current**, not just the new one.
- ***Recorded + current selection*** **adds** to them. Right when you are
  genuinely bringing in another reel; wrong for a version bump, because V1 stays
  a source forever and nothing ever drops out.

Either way the run prints what changed about the source set before it does
anything, and warns when a recorded source is not part of this run:

```
Source set: 2 kept, 1 added, 1 dropped
  added:   VW_GW_30s_V2
  DROPPED: VW_GW_30s_V1
  WARNING: those were recorded sources and are not part of this run...
```

Sources are matched by timeline ID, so V2 is simply a different source — there
is no automatic "V2 supersedes V1" link, which is exactly why that warning
exists. Combined with dry run, a source you forgot to select is obvious before
anything is written.

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
- Restore button to revert timeline clips back to their original Media Pool names
- Does not modify the source Media Pool item

### Numbering only part of a timeline (Resolve 21.0.4+)
Both Shot Numbering scripts can limit their scope to the clips you have selected on the timeline, via **Apply To > Selected clips only**. This needs `Timeline:GetSelectedClips()`, added in DaVinci Resolve 21.0.4; on older builds the control is disabled and behaviour is unchanged.

When scoped, a **Numbering** control decides how the numbers are assigned:

- **Keep full-timeline numbers** (default) — the whole timeline is numbered as usual, but only the selected clips are written to. A selected clip gets the same number a full run would have given it, so a partial pass stays consistent with the rest of the timeline.
- **Renumber selection from the first number** — the selection is numbered from the start as though it were the entire timeline. Useful for an isolated section, but it can collide with numbers already in use elsewhere.

Select your clips before pressing Run — the selection is read when the dialog closes, so you can also select them while the dialog is open. The Clear Markers and Restore Names buttons honour the same scope, so they never reach outside your selection.


## Copy Clip to Nuke (Python)
A unified Python rewrite that replaces both the old "Copy to Nuke Simple" and "Copy Clip and Settings to Nuke Python" Lua scripts. Copies the selected clip's file path, editorial data, and metadata to the clipboard in a Nuke-ready format.

Which clip it uses: the clip **selected on the timeline** (needs `Timeline:GetSelectedClips()`, DaVinci Resolve 21.0.4+), falling back to the clip under the playhead on older builds or when nothing is selected. If several video clips are selected, the one on the topmost track wins — the same clip the Viewer is showing. The console always prints which clip was chosen.

Features a UI dialog with configurable settings:
- **Output Mode**: Python (Script Editor) or TCL (Node Graph Paste)
- **Handles**: Configurable frame handles (default 12)
- **Colorspace**: Dropdown presets (ARRI LogC4, LogC3, REDLog3G10, S-Log3, ACEScg, etc.) with a free-text field for custom values
- **Format Name**: Name for the Nuke format entry (default "Plate")
- **Set Project Settings**: Optionally sets Nuke project format, FPS, and frame range (Python mode only)
- **Clear Existing Nodes**: Optional destructive clear of all nodes before setup (off by default)

Both modes create: Read node -> ModifyMetaData (reel name) -> ShotSetup group (frame range management, TimeOffset to rebase to frame 1001 +/- handles).

Settings are remembered per-project across runs and even Resolve restarts.

### Copy Clip to Nuke — Quick
A companion script that skips the settings dialog entirely when settings have already been configured for the current project. On first use (no saved settings) it opens the full UI; after that it immediately copies to clipboard using the last-used settings. Use the main "Copy Clip to Nuke" script whenever you want to change settings.


## DCTL Report (Python, PySide6)
Scans the currently-open project for every LUT and DCTL reference, resolves each against Resolve's standard LUT directories **and** any custom directories configured in **Preferences > General > LUT Locations**, and reports what is present and what is missing. The same scan lists every OFX plugin the project uses and flags the ones not installed on this machine.

- **DRP-based scanning**: Exports the project to a .drp file and decodes zstd-compressed body blobs to recover DCTL/LUT filenames — the only reliable way to extract OFX/ResolveFX parameters
- **Scope modes**: Scan selected timelines only or all timelines in the project
- **Plugin check**: Every OFX plugin used is checked against Resolve's own plugin cache (`OFXPluginCacheV2.xml`). Installed plugins are shown with their name, version and bundle path; missing ones can only be shown by identifier (e.g. `co.videovillage.filmbox3ofx`), since the readable name lives in the plugin itself. ResolveFX is listed as built-in. Installed does not mean licensed.
- **Show Missing List**: Full expected paths of every missing file, selectable and copyable, followed by the identifiers of missing plugins
- **Show Complete List**: Every referenced LUT/DCTL with the resolved path it is being read from, then every plugin used; missing entries are marked `(missing)`
- **Copy Existing Files**: Copies every resolved LUT/DCTL into a destination folder, preserving parent directory structure (plugins are not copied)
- **Export Full Report**: Writes a categorised .txt report of all references with status and source
- Requires `zstandard` installed into Resolve's Python interpreter (usually just a `pip install zstandard`)

## Batch Rename (Python, PySide6)
A comprehensive batch renaming utility for media pool items in DaVinci Resolve. Provides Advanced Renamer-style composable operations with a live preview.

- **Operation Pipeline**: Chain multiple rename operations that execute sequentially — search & replace (plain text or regex), add prefix/suffix, remove N characters from start/end/position
- **Drag-and-Drop Reordering**: Operations can be reordered by dragging, with per-row delete (x) and enable/disable toggles
- **Type Filters**: Rename only specific item types — Timeline, Video, Audio, Still/Image, Compound Clip, Fusion Comp, Generator, or Other
- **Live Preview**: See the result of all operations before committing, with automatic collision detection for duplicate names
- **Undo History**: Up to 20 levels of undo, reverting renamed items back to their original names
- **Presets**: Save and load operation pipelines with filter states. Set a default preset that auto-loads on script startup
- **Date/Time Tokens**: Use `{date}`, `{time}`, `{year}`, `{month}`, `{day}`, `{hour}`, `{minute}`, `{second}` in prefix, suffix, and replace fields
- **Include Subfolders**: Optionally recurse into subfolders of the current media pool bin
- Uses PySide6 (bundled with Resolve) for a native Qt UI with dark theme

## Find Clip in Timelines
Searches every timeline in the current project for the selected clip and lists the timelines that use it. Run it with a clip selected in the Media Pool or on the active timeline.

- **Source detection**, in priority order: the Media Pool selection, then the timeline selection (needs `Timeline:GetSelectedClips()`, DaVinci Resolve 21.0.4+), then the clip under the playhead. The Media Pool wins because a bin selection stays visible from every page, whereas a timeline selection only exists on Cut/Edit and can sit stale while you work in the bin — ctrl/cmd-click to deselect in the bin if you want the timeline to win. Audio clips are valid search targets. The popup and console both state which source was used
- **Clickable results**: Double-click a timeline in the list to switch to it; the playhead jumps (best-effort) to the in-point of the clip's first occurrence on that timeline
- **Multi-hit indicator**: Timelines containing more than one instance show a `(N×)` badge — the jump targets the first occurrence
- **Drop-frame aware**: Playhead positioning handles 29.97 / 59.94 drop-frame timecode
- **Stay-open popup**: The results window remains open after opening a timeline, so you can jump between multiple matches without re-running the script
- Dark, Resolve-style UI built with tkinter — no extra dependencies


## Future Features
The nuke integration is in its infancy and I am contemplating making it a bit more robust and useful, maybe creating a script that will generate a project config file that handles all variables like, project resolution, handles and so on. You could theoretically also reference nukescript templates and generate nuke scripts out of Resolve.

## License

Copyright (C) 2026 Maris Polis - marispolis.com

Released under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).

Use it for anything, including paid client work, with no restriction. If you
distribute a modified version you must ship its source under the same licence,
which is what stops these scripts being folded into a closed product and sold.

This program comes with ABSOLUTELY NO WARRANTY. It edits timelines in your
project and some of what it does cannot be undone in one step — read the notes
on Update Timeline mode before running it on work you care about.
