# Tests

These run **outside** DaVinci Resolve's UI. Most need no running Resolve at all.

## Prerequisites

- **Lua tests**: `fuscript.exe`, the Lua 5.1 interpreter that ships with Resolve. No
  separate Lua install, and Resolve does not need to be running.
  - Windows: `C:\Program Files\Blackmagic Design\DaVinci Resolve\fuscript.exe`
  - macOS: `/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/fuscript`
- **Python tests**: Python 3.6+. The two live tests additionally need Resolve running
  with a project open, and **Preferences > System > General > "External scripting
  using"** set to `Local`.

Paths are derived from each test file's own location, so any checkout works — no
editing required. The Lua tests also accept an explicit repo root as their first
argument.

The `RESOLVE_SCRIPT_API` / `RESOLVE_SCRIPT_LIB` defaults inside the Python tests are
Windows paths; set those environment variables to override on macOS or Linux.

## Running

```bash
# Parse-check every Lua script in the repo (fast; catches syntax errors)
fuscript.exe -l lua tests/luacheck.lua

# Shot Numbering semantics, against stubbed Resolve objects
fuscript.exe -l lua tests/test_shot_numbering.lua
fuscript.exe -l lua tests/test_shot_numbering_clipname.lua

# Python logic tests (no Resolve needed; live sections self-skip)
python tests/test_part_a.py     # Copy Clip to Nuke clip selection
python tests/test_part_b.py     # Find Clip in Timelines source tiers

# Live tests (Resolve must be running with a project open)
python tests/test_part_d.py     # Generate All Clips timeline resolution
python tests/final_live_check.py
```

Exit codes: `0` pass, `1` fail, `2` skipped because Resolve was unavailable. A skip
is not a pass — `test_part_d.py` verifies nothing without a live project.

`final_live_check.py` waits up to six minutes for a timeline selection, so you can
start it and then make the selection. For its Find Clip assertions to exercise the
timeline-selection tier, the **Media Pool selection must be empty** — otherwise the
Media Pool tier legitimately wins and that path is skipped.

## How these work

Every script in this repo runs its entry point at import (`main()` / `Main()` at the
bottom of the file), so the tests cannot simply import them:

- **Python** — the target functions are pulled from the real file with `ast` and
  `exec`'d individually, so the test exercises the shipped source rather than a copy.
  `test_part_a.py` instead `exec`s the whole `Copy Clip to Nuke.py` with
  `_NUKE_EXPORT_IMPORTED` set, which is the same guard `Copy Clip to Nuke (Quick).py`
  uses, giving the true module namespace.
- **Lua** — the script's source is read, its single trailing `Main()` call is
  stripped, and the rest is `loadstring`+`setfenv`'d into a table holding stub
  `bmd` / `resolve` / `project` / `timeline` objects. Because the scripts declare
  their functions global, they land in that environment; their file-level locals
  become upvalues bound to the stubs. `ShowConfigDialog` is then overridden to return
  a config table, so `Main()` can be driven end-to-end with no UI.

Stub MediaPoolItems and TimelineItems record every `SetMetadata`, `SetName` and
`AddMarker` call, which is what lets the numbering tests assert exact results.

## What is covered

`test_shot_numbering.lua` (9 scenarios) and `test_shot_numbering_clipname.lua`
(7 scenarios) are the important ones. Both assert that the **default settings
reproduce the pre-scope behaviour exactly**, so a regression in the common path is
caught immediately. They also cover both numbering modes, the duplicate-instance
consequence of "keep full-timeline numbers", scoped overwrite and scoped restore
staying inside the selection, audio items in a selection, and the fallbacks when the
selection API is missing or nothing is selected.

## Adding to them

If you change a script's behaviour, extend the matching test. Note that the Lua
harnesses locate the trailing `Main()` call by an exact `\nMain()\n` match and abort
if they do not find exactly one — so if you restructure a script's entry point,
update its test's strip step too.
