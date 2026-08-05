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
print("\n  RESULT: " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
