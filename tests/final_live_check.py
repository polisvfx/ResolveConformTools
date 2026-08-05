"""
Final live verification: exercise the tier-1 selection paths of the real,
committed code against a real timeline selection.

Waits (up to 6 min) for a non-empty timeline selection, then runs:
  - Copy Clip to Nuke        pick_export_item()  + the full extractor
  - Find Clip in Timelines   get_target_clip()

Read-only: nothing is written to the project or the clipboard.
"""

import ast
import os
import sys
import time

api = os.environ.get(
    "RESOLVE_SCRIPT_API",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
)
sys.path.append(os.path.join(api, "Modules"))
os.environ.setdefault(
    "RESOLVE_SCRIPT_LIB",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
)

def _repo_root():
    """Repo root = the parent of this tests/ directory.

    Derived from this file's path so the suite runs from any checkout rather
    than one hardcoded Resolve install. __file__ is undefined in some Resolve
    embedded-Python contexts (see commit 5e768cb), so fall back to the current
    working directory, which covers being run from the repo root or tests/.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        cwd = os.path.abspath(os.getcwd())
        if os.path.basename(cwd) == "tests":
            here = cwd
        else:
            here = os.path.join(cwd, "tests")
    return os.path.dirname(here)


REPO = _repo_root()


def extract(path, names, extra_globals=None):
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    nodes = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    missing = set(names) - {n.name for n in nodes}
    if missing:
        raise SystemExit(f"not found in {os.path.basename(path)}: {sorted(missing)}")
    ns = dict(extra_globals or {})
    ns.setdefault("print", print)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), ns)
    return ns


import DaVinciResolveScript as dvr  # noqa: E402

fails = []


def connect():
    """Fresh handles every poll.

    Every one of these can go stale if Resolve is closed or the project is
    swapped mid-wait, and a dead bridge object resolves its attributes to None
    rather than raising - so each step is re-fetched and re-checked instead of
    being cached across the loop.
    """
    try:
        resolve = dvr.scriptapp("Resolve")
    except Exception:
        return None, None, None
    if resolve is None:
        return None, None, None
    pm = resolve.GetProjectManager()
    if not callable(getattr(pm, "GetCurrentProject", None)):
        return resolve, None, None
    project = pm.GetCurrentProject()
    if not callable(getattr(project, "GetCurrentTimeline", None)):
        return resolve, project, None
    return resolve, project, project.GetCurrentTimeline()


# --- wait for a selection -------------------------------------------------
print("Waiting for a timeline selection (up to 6 minutes)...")
deadline = time.time() + 360
sel = []
tl = None
project = None
warned = None
while time.time() < deadline:
    resolve, project, tl = connect()
    if resolve is None:
        if warned != "app":
            print("  Resolve is not running (or scripting is unreachable) - "
                  "waiting for it to come back...")
            warned = "app"
    elif project is None:
        if warned != "project":
            print("  No project open - waiting...")
            warned = "project"
    elif tl is None:
        if warned != "timeline":
            print("  No timeline open - waiting...")
            warned = "timeline"
    else:
        warned = None
        getter = getattr(tl, "GetSelectedClips", None)
        if callable(getter):
            try:
                sel = list(getter() or [])
            except Exception:
                sel = []
        if sel:
            break
    time.sleep(2)

if not sel:
    print("\nTimed out without a usable timeline selection - the tier-1 paths "
          "are NOT verified.")
    sys.exit(2)

print(f"\nTimeline: {tl.GetName()!r}")
print(f"Selected: {len(sel)} item(s)")
for it in sel:
    try:
        tti = it.GetTrackTypeAndIndex()
        where = f"{tti[0]}{tti[1]}"
    except Exception:
        where = "?"
    print(f"  {where:<8} start={it.GetStart():<7} {it.GetName()}")

# --- Part A: Copy Clip to Nuke -------------------------------------------
print("\n" + "=" * 68)
print("  Copy Clip to Nuke - pick_export_item()")
print("=" * 68)


# Load the WHOLE real module rather than ast-extracting pieces of it. The script
# guards its own main() behind _NUKE_EXPORT_IMPORTED, which is exactly how
# "Copy Clip to Nuke (Quick).py" consumes it - so this gets the true module
# namespace (imports, constants, every function) with no risk of a missing global.
_nuke_path = os.path.join(REPO, "Copy Clip to Nuke.py")
nuke_ns: dict = {"_NUKE_EXPORT_IMPORTED": True, "resolve": resolve}
with open(_nuke_path, "r", encoding="utf-8") as _f:
    exec(compile(_f.read(), _nuke_path, "exec"), nuke_ns)

pick = nuke_ns["pick_export_item"](tl)
if pick is None:
    fails.append("Nuke: pick_export_item returned None despite a selection")
    print("  FAIL: returned None")
else:
    print(f"  picked : {pick.item.GetName()!r}")
    print(f"  label  : {pick.label!r}")
    print(f"  count  : {pick.selected_count}")

    # Must be the topmost video track among the selected video clips.
    tracks = []
    for it in sel:
        idx = nuke_ns["_video_track_index"](it)
        if idx is not None:
            tracks.append(idx)
    if tracks:
        want_top = max(tracks)
        got = nuke_ns["_video_track_index"](pick.item)
        if got != want_top:
            fails.append(f"Nuke: picked V{got}, expected topmost V{want_top}")
        print(f"  topmost video track selected = V{want_top}, picked V{got}"
              f"  -> {'ok' if got == want_top else 'MISMATCH'}")
        if pick.selected_count != len(tracks):
            fails.append("Nuke: selected_count != number of video clips selected")
    else:
        print("  (no video clips in the selection; playhead fallback expected)")
        if pick.selected_count != 0:
            fails.append("Nuke: fallback should report selected_count 0")

    # Run the REAL extractor on the picked item - the whole point of the change.
    print("\n  running the real get_selected_clip_data() on the picked item...")
    raised = False
    try:
        clip = nuke_ns["get_selected_clip_data"](tl, pick.item)
    except Exception as exc:
        clip = None
        raised = True
        fails.append(f"Nuke: extractor raised {type(exc).__name__}: {exc}")
        print(f"  FAIL: raised {type(exc).__name__}: {exc}")
    if clip is None and not raised:
        # A None return is a legitimate outcome (generator / compound clip /
        # unreadable File Path) and must not be conflated with an exception.
        print("  extractor returned None - unsupported clip, which is a valid "
              "outcome. Note it now bails BEFORE the dialog opens.")
    elif clip is not None:
        print(f"    nuke_path   : {clip.nuke_path}")
        print(f"    resolution  : {clip.width}x{clip.height} @ {clip.fps}")
        print(f"    src in/out  : {clip.start_frame} - {clip.end_frame}")
        print(f"    timeline    : {clip.start_tc} - {clip.end_tc}")
        if clip.end_tc - clip.start_tc != pick.item.GetEnd() - pick.item.GetStart():
            fails.append("Nuke: timeline duration mismatch in extracted data")

# --- Part B: Find Clip in Timelines --------------------------------------
print("\n" + "=" * 68)
print("  Find Clip in Timelines - get_target_clip()")
print("=" * 68)

find_ns = extract(
    os.path.join(REPO, "Find Clip in Timelines.py"),
    ["_as_list", "get_media_pool_selection", "get_timeline_selection",
     "_earliest_media_pool_item", "get_target_clip"],
)
mp_sel = find_ns["get_media_pool_selection"](project)
print(f"  media pool selection : {len(mp_sel)} item(s)")
print(f"  timeline selection   : {len(find_ns['get_timeline_selection'](tl))} item(s)")

target, label = find_ns["get_target_clip"](project)
print(f"  target : {target.GetName() if target else None!r}")
print(f"  label  : {label!r}")

if mp_sel:
    if "media pool" not in label.lower():
        fails.append("Find: bin selection active but tier 1 did not win")
    print("  (bin selection is active, so tier 1 correctly wins)")
else:
    if "timeline selection" not in label.lower():
        fails.append(f"Find: expected the timeline-selection tier, got {label!r}")
    else:
        print("  -> timeline-selection tier fired, as intended")
    # The earliest selected item's MediaPoolItem should be the target.
    earliest = min(sel, key=lambda i: i.GetStart())
    want = earliest.GetMediaPoolItem()
    if want is not None and target is not None:
        if want.GetUniqueId() != target.GetUniqueId():
            fails.append("Find: target is not the earliest selected clip's source")
        else:
            print(f"  -> matches the earliest selected clip "
                  f"(start={earliest.GetStart()})")

print("\n" + "=" * 68)
if fails:
    print(f"RESULT: FAIL ({len(fails)})")
    for f in fails:
        print(f"  - {f}")
else:
    print("RESULT: PASS")
print("=" * 68)
sys.exit(0 if not fails else 1)
