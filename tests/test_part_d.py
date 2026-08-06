"""
Functional test for Part D against the live project.

Extracts get_timeline_for_media_pool_item() verbatim from the real script via ast
(the script calls main() at import time, so it cannot simply be imported), then
runs it over every timeline-type MediaPoolItem in the open project and compares
the result against the old name-map path.
"""

import ast
import os
import sys

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
SCRIPT = os.path.join(REPO, "Generate All Clips Timeline PRO.py")
FUNC = "get_timeline_for_media_pool_item"


def extract(path, func_name):
    """Return the real function object, compiled from the real file's source."""
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {}
            exec(compile(mod, path, "exec"), ns)
            print(f"  extracted {func_name}() from source lines "
                  f"{node.lineno}-{node.end_lineno}")
            return ns[func_name]
    raise SystemExit(f"{func_name} not found in {path}")


get_timeline_for_media_pool_item = extract(SCRIPT, FUNC)

import DaVinciResolveScript as dvr  # noqa: E402

# This test compares the new and old timeline-resolution paths against real
# project data, so unlike the other suites it cannot run without Resolve. Exit 2
# (distinct from a real failure) with an actionable message rather than letting a
# dead bridge object surface as an AttributeError. Note the bridge resolves
# attributes on a stale handle to None, so check each step rather than assuming.
SKIP = 2

resolve = dvr.scriptapp("Resolve")
if resolve is None:
    print("SKIPPED: Resolve is not running, or external scripting is disabled.")
    print("  Start DaVinci Resolve, open a project containing at least one")
    print("  timeline, and set Preferences > System > General >")
    print("  'External scripting using' to Local.")
    sys.exit(SKIP)

pm = resolve.GetProjectManager()
project = pm.GetCurrentProject() if callable(
    getattr(pm, "GetCurrentProject", None)) else None
if not callable(getattr(project, "GetTimelineCount", None)):
    print("SKIPPED: no project is open in Resolve.")
    print("  Open a project containing at least one timeline and re-run.")
    sys.exit(SKIP)

media_pool = project.GetMediaPool()

print(f"  project: {project.GetName()!r}")

# Old path: the eager name map.
name_map = {}
for i in range(1, project.GetTimelineCount() + 1):
    tl = project.GetTimelineByIndex(i)
    if tl is not None:
        name_map[tl.GetName()] = tl
print(f"  name map: {len(name_map)} entries from "
      f"{project.GetTimelineCount()} timelines")
if len(name_map) != project.GetTimelineCount():
    print(f"  *** NAME COLLISION: {project.GetTimelineCount() - len(name_map)} "
          f"timeline(s) are unreachable via the name map ***")


def walk(folder, out, depth=0):
    if depth > 8 or folder is None:
        return out
    for c in (folder.GetClipList() or []):
        out.append(c)
    for sub in (folder.GetSubFolderList() or []):
        walk(sub, out, depth + 1)
    return out


clips = walk(media_pool.GetRootFolder(), [])
tl_items = [c for c in clips
            if (c.GetClipProperty("Type") or "") == "Timeline"]
other_items = [c for c in clips
               if (c.GetClipProperty("Type") or "") != "Timeline"]

print(f"  media pool: {len(clips)} clips, {len(tl_items)} of type Timeline\n")

agree = new_only = old_only = neither = mismatch = 0
for mpi in tl_items:
    name = mpi.GetName()
    new = get_timeline_for_media_pool_item(mpi)
    old = name_map.get(name)

    if new is not None and old is not None:
        # Compare by identity-independent means: name + track count + item count.
        def sig(tl):
            try:
                n = tl.GetTrackCount("video")
                return (tl.GetName(), n,
                        len(tl.GetItemListInTrack("video", 1) or []))
            except Exception as exc:
                return ("<err>", str(exc), None)
        if sig(new) == sig(old):
            agree += 1
        else:
            mismatch += 1
            print(f"  MISMATCH {name!r}: new={sig(new)} old={sig(old)}")
    elif new is not None:
        new_only += 1
        print(f"  NEW ONLY  {name!r}  (name map missed it)")
    elif old is not None:
        old_only += 1
        print(f"  OLD ONLY  {name!r}  (GetTimeline returned None)")
    else:
        neither += 1
        print(f"  NEITHER   {name!r}")

print(f"\n  timeline items: agree={agree} mismatch={mismatch} "
      f"new_only={new_only} old_only={old_only} neither={neither}")

# The gate must still return None for non-timeline items.
bad = 0
for mpi in other_items[:40]:
    if get_timeline_for_media_pool_item(mpi) is not None:
        bad += 1
        print(f"  *** non-timeline item returned a Timeline: {mpi.GetName()!r}")
print(f"  non-timeline items sampled: {min(40, len(other_items))}, "
      f"unexpected Timeline returns: {bad}")

ok = (mismatch == 0 and old_only == 0 and neither == 0 and bad == 0
      and agree == len(tl_items))

# ---------------------------------------------------------------------------
# Update-mode capability probes
# ---------------------------------------------------------------------------
#
# These answer questions the scripting docs leave open, and which the update
# workflow was designed around rather than against. They WRITE to the open
# project - a scratch timeline that is deleted again - so they only run when
# RCT_LIVE_WRITE=1 is set.
#
# A "no" here is information, not a failure of this repo's code: every one of
# them has a fallback. Only the recordFrame probe changes what the feature can
# do, and it says so loudly.

if os.environ.get("RCT_LIVE_WRITE") != "1":
    print("\n  capability probes: skipped (set RCT_LIVE_WRITE=1 to run them;")
    print("  they create and then delete a scratch timeline in the open project)")
else:
    print("\n  === update-mode capability probes ===")
    probe_name = f"RCT_PROBE_{os.getpid()}"
    probe_tl = None
    findings = []

    def note(key, answer, detail=""):
        findings.append((key, answer, detail))
        print(f"  {key:<28} {answer}{('  - ' + detail) if detail else ''}")

    video_clips = [c for c in other_items
                   if (c.GetClipProperty("Type") or "") == "Video"]
    if not video_clips:
        video_clips = other_items

    try:
        probe_tl = media_pool.CreateEmptyTimeline(probe_name)
        if probe_tl is None:
            print("  could not create a scratch timeline; probes skipped")
        else:
            project.SetCurrentTimeline(probe_tl)
            start_frame = probe_tl.GetStartFrame()

            # R1 - does third-party metadata persist on a timeline's MediaPoolItem?
            tl_mpi = probe_tl.GetMediaPoolItem() if callable(
                getattr(probe_tl, "GetMediaPoolItem", None)) else None
            if tl_mpi is None:
                note("R1 timeline metadata", "NO", "Timeline.GetMediaPoolItem absent")
            else:
                wrote = False
                try:
                    wrote = bool(tl_mpi.SetThirdPartyMetadata("RCT_PROBE", "hello"))
                except Exception as exc:
                    note("R1 timeline metadata", "NO", f"raised {exc}")
                if wrote:
                    back = None
                    try:
                        back = tl_mpi.GetThirdPartyMetadata("RCT_PROBE")
                    except Exception:
                        back = None
                    if isinstance(back, dict):
                        back = back.get("RCT_PROBE")
                    note("R1 timeline metadata",
                         "YES" if back == "hello" else "NO",
                         f"read back {back!r}")
                elif not findings or findings[-1][0] != "R1 timeline metadata":
                    note("R1 timeline metadata", "NO", "Set returned False")

            # R7 - how much customData survives a round trip?
            big = "x" * 16000
            probe_tl.AddMarker(0, "Cream", "probe", "", 1, big)
            got = probe_tl.GetMarkerCustomData(0) if callable(
                getattr(probe_tl, "GetMarkerCustomData", None)) else ""
            note("R7 customData 16k",
                 "YES" if got == big else "NO",
                 f"kept {len(got or '')} of {len(big)} chars")

            # R2 - are timeline marker frames offsets from GetStartFrame()?
            markers = probe_tl.GetMarkers() or {}
            note("R2 marker frame space",
                 "OFFSET" if 0.0 in markers or 0 in markers else "ABSOLUTE",
                 f"asked for 0, timeline starts at {start_frame}, "
                 f"got keys {sorted(markers)[:3]}")
            probe_tl.DeleteMarkerAtFrame(0)

            if not video_clips:
                note("R5 recordFrame into gap", "UNKNOWN", "no video clip to place")
            else:
                clip = video_clips[0]
                # Two clips with a deliberate 200-frame hole between them.
                media_pool.AppendToTimeline([
                    {"mediaPoolItem": clip, "startFrame": 0, "endFrame": 49,
                     "mediaType": 1, "trackIndex": 1,
                     "recordFrame": start_frame},
                    {"mediaPoolItem": clip, "startFrame": 0, "endFrame": 49,
                     "mediaType": 1, "trackIndex": 1,
                     "recordFrame": start_frame + 250},
                ])
                before = probe_tl.GetItemListInTrack("video", 1) or []
                placed = media_pool.AppendToTimeline([
                    {"mediaPoolItem": clip, "startFrame": 100, "endFrame": 179,
                     "mediaType": 1, "trackIndex": 1,
                     "recordFrame": start_frame + 100},
                ])
                after = probe_tl.GetItemListInTrack("video", 1) or []
                if placed and len(after) == len(before) + 1:
                    landed = placed[0].GetStart()
                    note("R5 recordFrame into gap", "YES",
                         f"asked {start_frame + 100}, landed {landed}")
                    # R4 / recordFrame frame space in one shot.
                    note("R4 GetEnd vs start+duration",
                         "EXCLUSIVE" if placed[0].GetEnd() ==
                         placed[0].GetStart() + placed[0].GetDuration()
                         else "INCLUSIVE",
                         f"start={placed[0].GetStart()} "
                         f"dur={placed[0].GetDuration()} "
                         f"end={placed[0].GetEnd()}")
                    note("recordFrame frame space",
                         "SAME AS GetStart" if landed == start_frame + 100
                         else "DIFFERS",
                         f"offset {landed - (start_frame + 100)}")

                    # R3 - what frame space do TimelineItem markers use?
                    item = placed[0]
                    src = item.GetSourceStartFrame()
                    added_source = item.AddMarker(src + 5, "Blue", "src", "", 1, "")
                    item_markers = sorted(item.GetMarkers() or {})
                    note("R3 item marker frame space",
                         "SOURCE" if added_source and item_markers
                         and int(item_markers[0]) == src + 5 else "OTHER",
                         f"source start {src}, marker keys {item_markers[:3]}")
                else:
                    note("R5 recordFrame into gap", "NO",
                         f"{len(before)} clips before, {len(after)} after - "
                         f"in-place growth is not available on this build; "
                         f"ACTION NEEDED: route every change to the update track")

            print("\n  Probe answers are recorded for the update-mode design;")
            print("  none of them fail this test on their own.")
    finally:
        if probe_tl is not None:
            deleted = False
            try:
                deleted = bool(media_pool.DeleteTimelines([probe_tl]))
            except Exception:
                deleted = False
            if not deleted:
                print(f"  *** could not delete the scratch timeline "
                      f"{probe_name!r}; remove it by hand ***")

print("\n  RESULT: " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
