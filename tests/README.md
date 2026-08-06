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
python tests/test_part_e.py     # Generate All Clips PRO update decisions
python tests/test_part_f.py     # Generate All Clips PRO update mode, end to end

# Live tests (Resolve must be running with a project open)
python tests/test_part_d.py     # Generate All Clips timeline resolution
python tests/final_live_check.py
```

`test_part_d.py` also carries capability probes for update mode, which **write to
the open project** — a scratch timeline that is created and deleted again. They
only run when opted in:

```bash
RCT_LIVE_WRITE=1 python tests/test_part_d.py
```

They answer questions the scripting docs leave open. A "no" is information rather
than a failure — each has a fallback in the script — so the probes report but
never fail the suite on their own.

All of them were run on **21.0.4.5** on 2026-08-06 (25 fps project, timelines
starting at frame 90000) and every answer came back the way the update path
assumes:

| Probe | Answer |
|---|---|
| `SetThirdPartyMetadata` on a timeline's Media Pool item | round-trips |
| `customData` capacity | 16,000 chars kept intact |
| Timeline marker frames | offsets from `GetStartFrame()` |
| `AppendToTimeline` recordFrame + trackIndex into a gap | places exactly |
| recordFrame frame space | absolute, same as `GetStart()` |
| `GetEnd()` | exclusive (`start + duration`) |
| TimelineItem marker frames | source/media frame space |

Re-run them after a Resolve upgrade; the frame-space answers in particular are
undocumented and are what the placement arithmetic rests on.

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

`test_part_f.py` is the other important one: it drives `run_update_workflow()` in
**Generate All Clips Timeline PRO.py** end to end against a stubbed Resolve —
adoption, in-place growth with the clip's name/colour/flags/markers restored, the
boxed-in case that lands on the update track instead, shortening leaving its
neighbours where they were, new and dropped shots, dry run, protected clips, and
the churn test: a second run over unchanged sources must do nothing at all. That
last one is what catches placement slip, the widening fallback and media clamps,
any of which would otherwise rebuild the whole timeline on every run.

Its `AppendToTimeline` stub refuses to place a clip over an existing one rather
than silently overwriting it, which is the conservative reading of undocumented
behaviour; the script never plans an overlapping placement either way.

`test_part_e.py` covers the decision functions underneath it — the identity key
shared with the collection pass, overlap matching, change classification, gap
arithmetic and the manifest round trip. Its `extract()` differs from the Part A/B
one in two ways it documents: it also pulls self-contained module constants
(functions default arguments to them), and it compiles with the `annotations`
future flag so return annotations are not evaluated at def time.

## Adding to them

If you change a script's behaviour, extend the matching test. Note that the Lua
harnesses locate the trailing `Main()` call by an exact `\nMain()\n` match and abort
if they do not find exactly one — so if you restructure a script's entry point,
update its test's strip step too.
