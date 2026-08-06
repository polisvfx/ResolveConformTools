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

### Python Version (Generate All Clips Timeline PRO.py)
A full Python rewrite of the Lua script with improved retime handling and XML-based source range analysis.
- **Retime handling**: Correctly detects and handles retimed clips, reversed clips, and frame holds (freeze frames). All clips are normalized to forward-playing source ranges on the master timeline.
- **XML retime analysis**: Optionally exports the source timeline to FCP 7 XML and parses Time Remap keyframes to compute precise source frame ranges — particularly useful for speed ramps and non-linear retimes where the API-reported range may be inaccurate.
- **Distinct retime markers**: Red markers distinguish between "Frame Hold", "Non-Linear Retime", and "Retimed Clip" (with speed percentage), with a note when the source was originally reversed.
- **Clean API calls**: Only API-recognized fields are passed to `AppendToTimeline`, preventing silent failures caused by extra metadata.
- **Complete source ranges (2.3+)**: `AppendToTimeline`'s `endFrame` is *exclusive* — asking for `endFrame=E` places frames up to `E-1`. Every clip is now requested one frame past its last frame, so the range that lands is the range that was asked for.

  > **If you generated an All Clips timeline with version 2.2 or earlier**, every clip on it is missing the last frame of its range, and any freeze frame in the source is missing entirely — a one-frame request was zero-length under the exclusive rule and Resolve refused it outright. Update mode will *not* repair these: a single frame is below the change threshold, so those clips read as unchanged. Regenerate the timeline if the tail frame matters, which for a VFX pull it usually does.
- All features from the Lua version (sorting, merging, duplicate marking, audio removal) are fully preserved.

#### Update Timeline mode (2.0+)

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

Resolve's API has no trim and no move. Changing a clip's range therefore means
deleting it and re-appending it, and **a rebuilt clip loses its grade and its
Fusion comps** — `CopyGrades` needs the source clip still alive and there is no
way to read a grade back out. Its name, clip colour, flags, enabled state and
your own markers *are* restored. Clips carrying a Fusion comp or more than one
colour version are skipped by default and flagged for manual attention; untick
*skip clips with Fusion comps* to rebuild them anyway.

That constraint is why only clips whose range actually moved are touched, and it
shapes one behaviour worth expecting: **on a gapless timeline a clip has nowhere
to grow**, so extensions are placed as a full-range copy on the run's update
track and the original is marked superseded rather than moved. Growth happens in
place only where a gap exists — usually one that a shortening opened earlier in
the same run, which is why range changes are applied smallest-delta first.

Other things to know:

- **Growing and shrinking are not treated alike.** A shot is rebuilt once it
  needs more than 3 frames it does not have, but only once it is carrying more
  than 12 surplus frames. Missing frames break a pull; surplus frames are just
  handle, and rebuilding to remove a few of them would cost that clip its grade
  for nothing. Both are constants near the top of the script
  (`UPDATE_CHANGE_TOLERANCE`, `UPDATE_SHRINK_TOLERANCE`) if your handles differ.
- **The tool owns source ranges.** A clip you trimmed by hand — by more than
  those thresholds — reads as changed and is put back to the collected range.
  Every such rebuild is marked.
- **Video only.** A clip with linked audio is skipped: deleting the video item
  would orphan the audio. Generate with *Video Only* on, which is the default.
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
doubles as a visible "this timeline is managed" badge. Which clips are on the
timeline is deliberately *not* stored: it is re-scanned every run, so anything
you rearrange between runs is respected rather than overwritten.

A timeline with no such record can be **adopted**: choose *Current selection*
(or *Recorded + current selection*) as the update source, and the timelines you
have selected in the Media Pool become its recorded sources. The script never
guesses them. *Recorded + current selection* is also how you add a new reel to
an existing All Clips timeline.

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
Scans the currently-open project for every LUT and DCTL reference, resolves each against Resolve's standard LUT directories **and** any custom directories configured in **Preferences > General > LUT Locations**, and reports what is present and what is missing.

- **DRP-based scanning**: Exports the project to a .drp file and decodes zstd-compressed body blobs to recover DCTL/LUT filenames — the only reliable way to extract OFX/ResolveFX parameters
- **Scope modes**: Scan selected timelines only or all timelines in the project
- **Show Missing List**: Full expected paths of every missing file, selectable and copyable
- **Show Complete List**: Every referenced LUT/DCTL with the resolved path it is being read from; missing entries are marked `(missing)`
- **Copy Existing Files**: Copies every resolved LUT/DCTL into a destination folder, preserving parent directory structure
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
