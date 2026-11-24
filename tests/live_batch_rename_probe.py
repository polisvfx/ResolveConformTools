"""
Live check for Batch Rename's Timeline Clips tab, in a throwaway project.

Writes, so it only runs when opted in:

    RCT_LIVE_WRITE=1 RCT_PROBE_MEDIA=<video file, 100+ frames> py -3.12 -u tests/live_batch_rename_probe.py

It saves the project you have open, creates RCT_RENAME_<pid>, cuts three clips
from one source onto V1 and V2, and drives the script's real engine functions
against them:

  - TimelineItem.SetName renames the instance and leaves the MediaPoolItem alone
  - two instances of one source can hold different names
  - GetUniqueId() is stable after a rename (undo and selection depend on it)
  - GetClipColor / GetFlagList / GetMarkers on a TimelineItem have the shapes
    the filters read
  - undo and Restore by uid, and a Media Pool rename + undo for comparison

Then it reloads your project and deletes the throwaway one (RCT_KEEP_PROBE=1
keeps it). Needs PySide6 in the Python used (py -3.12 here), because the
script's module is executed whole, minus its main() call.

Exit 0 pass, 1 fail, 2 skipped.
"""

import ast
import os
import sys
import traceback
import types

SKIP = 2
if os.environ.get("RCT_LIVE_WRITE") != "1":
    print("SKIPPED: set RCT_LIVE_WRITE=1 to run (works in a throwaway project).")
    sys.exit(SKIP)
media_path = os.environ.get("RCT_PROBE_MEDIA", "")
if not os.path.isfile(media_path):
    print("SKIPPED: set RCT_PROBE_MEDIA to a video file (100+ frames).")
    sys.exit(SKIP)

api = os.environ.get(
    "RESOLVE_SCRIPT_API",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
)
sys.path.append(os.path.join(api, "Modules"))
os.environ.setdefault(
    "RESOLVE_SCRIPT_LIB",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "Batch Rename.py")


def load_script(resolve_obj):
    """Every definition in the script, without the trailing main() call."""
    with open(SCRIPT, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    body = [n for n in tree.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                    and getattr(n.value.func, "id", None) == "main")]
    mod = types.ModuleType("batch_rename_probe")
    sys.modules[mod.__name__] = mod
    mod.__dict__["resolve"] = resolve_obj
    exec(compile(ast.Module(body=body, type_ignores=[]), SCRIPT, "exec"), mod.__dict__)
    return mod


import DaVinciResolveScript as dvr  # noqa: E402

resolve = dvr.scriptapp("Resolve")
if resolve is None:
    print("SKIPPED: Resolve is not running, or external scripting is disabled.")
    sys.exit(SKIP)
pm = resolve.GetProjectManager()
home = pm.GetCurrentProject()
if not callable(getattr(home, "GetName", None)):
    print("SKIPPED: no project is open in Resolve.")
    sys.exit(SKIP)
home_name = home.GetName()
print(f"Resolve {resolve.GetVersionString()}, home project {home_name!r}", flush=True)

br = load_script(resolve)
scratch_name = f"RCT_RENAME_{os.getpid()}"
fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"),
          flush=True)
    if not ok:
        fails.append(label)


def note(label, value):
    print(f"  ..   {label}: {value!r}", flush=True)


ran = False
try:
    pm.SaveProject()
    project = pm.CreateProject(scratch_name)
    if project is None:
        print("SKIPPED: could not create the throwaway project.")
        sys.exit(SKIP)
    project.SetSetting("timelineFrameRate", "25")
    pool = project.GetMediaPool()
    imported = pool.ImportMedia([media_path]) or []
    if not imported:
        raise RuntimeError("ImportMedia returned nothing")
    mpi = imported[0]
    source_name = mpi.GetName()
    tl = pool.CreateEmptyTimeline("rename_probe")
    project.SetCurrentTimeline(tl)
    tl.AddTrack("video")
    t0 = tl.GetStartFrame()
    for s, e, rec, track in ((0, 25, 0, 1), (25, 50, 50, 1), (50, 75, 25, 2)):
        pool.AppendToTimeline([{"mediaPoolItem": mpi, "startFrame": s, "endFrame": e,
                                "trackIndex": track, "recordFrame": t0 + rec,
                                "mediaType": 1}])

    items = br.get_timeline_items(tl)
    check("three video items, in timeline order (start, then track)",
          [(i["track"], i["start"] - t0) for i in items], [(1, 0), (2, 25), (1, 50)])
    check("rename supported on this build", br.timeline_rename_supported(resolve, tl), True)
    check("source name and tokens",
          (items[0]["source_name"], items[1]["tokens"]["{track}"]), (source_name, "V2"))
    uids_before = [i["uid"] for i in items]

    # --- attrs on a TimelineItem -------------------------------------------
    first = items[0]["item"]
    note("SetClipColor", br._call(first, "SetClipColor", "Orange"))
    note("AddFlag", br._call(first, "AddFlag", "Red"))
    note("AddMarker", br._call(first, "AddMarker", 5, "Blue", "probe", "", 1))
    attrs = br.read_attrs(tl.GetItemListInTrack("video", 1)[0])
    note("read_attrs on a fresh wrapper", attrs)
    check("filter attrs read back",
          (attrs["clip_color"], "Red" in attrs["flags"], attrs["marker_colors"]),
          ("Orange", True, ["Blue"]))
    check("media pool clip did not take the timeline clip's colour",
          mpi.GetClipColor() in ("", None), True)

    # --- numbering rename ------------------------------------------------
    op = br.RenameOp(br.OpType.NUMBERING, num_template="SH_{n}")
    new = br.compute_new_names(items, [op])
    record, failed = br.execute_rename(list(zip(items, new)), kind="timeline",
                                       timeline_uid=tl.GetUniqueId(), timeline_name=tl.GetName())
    after = br.get_timeline_items(tl)
    check("timeline names after rename", [i["name"] for i in after],
          ["SH_0010", "SH_0020", "SH_0030"])
    check("no failures", failed, 0)
    check("media pool clip keeps its name", mpi.GetName(), source_name)
    check("two instances of one source hold different names",
          len({i["name"] for i in after if i["source_name"] == source_name}), 3)
    check("GetUniqueId stable across a rename", [i["uid"] for i in after], uids_before)

    # --- continue numbering sees the new names ----------------------------
    cont = br.RenameOp(br.OpType.NUMBERING, num_template="SH_{n}", num_start_mode="continue")
    check("continue after highest", br.numbering_base(cont, [i["name"] for i in after]), 40)

    # --- selection -------------------------------------------------------
    sel = br.get_timeline_selection(tl)
    note("GetSelectedClips after the appends", None if sel is None else len(sel))

    # --- undo ------------------------------------------------------------
    check("undo reverts all three", br.execute_undo(record, project), 3)
    check("names after undo", [i["name"] for i in br.get_timeline_items(tl)],
          [source_name] * 3)

    # --- restore ---------------------------------------------------------
    tl.GetItemListInTrack("video", 2)[0].SetName("custom")
    plan = br.restore_plan(br.get_timeline_items(tl))
    check("restore plan picks only the renamed clip", [n for _i, n in plan], [source_name])
    rec, _ = br.execute_rename(plan, kind="timeline", timeline_uid=tl.GetUniqueId(),
                               verb="Restored")
    check("restored", tl.GetItemListInTrack("video", 2)[0].GetName(), source_name)

    # --- media pool rename, for comparison --------------------------------
    mp_items = br.get_media_pool_items(project, recursive=True, from_root=True)
    clip = [i for i in mp_items if i["name"] == source_name]
    check("media pool item found", len(clip), 1)
    # A clip that was never renamed on the timeline, next to one that was (V1 #1
    # went through rename + undo, so it now carries a name of its own).
    pool.AppendToTimeline([{"mediaPoolItem": mpi, "startFrame": 75, "endFrame": 100,
                            "trackIndex": 1, "recordFrame": t0 + 100, "mediaType": 1}])
    rec, _ = br.execute_rename([(clip[0], "MP_RENAMED")], kind="media_pool")
    check("media pool rename", mpi.GetName(), "MP_RENAMED")
    v1_items = tl.GetItemListInTrack("video", 1)
    check("a never-renamed timeline clip follows the media pool rename",
          v1_items[-1].GetName(), "MP_RENAMED")
    note("a timeline clip that was renamed before keeps", v1_items[0].GetName())
    check("media pool undo", (br.execute_undo(rec, project), mpi.GetName()), (1, source_name))

    ran = True
except Exception:  # noqa: BLE001
    traceback.print_exc()
finally:
    if os.environ.get("RCT_KEEP_PROBE") == "1":
        print(f"\n  kept {scratch_name!r} (RCT_KEEP_PROBE=1); reload {home_name!r} yourself")
    else:
        print("\n  cleaning up", flush=True)
        try:
            pm.LoadProject(home_name)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        back = pm.GetCurrentProject()
        if back is not None and back.GetName() == home_name:
            if pm.DeleteProject(scratch_name):
                print(f"  reloaded {home_name!r}, deleted {scratch_name!r}")
            else:
                print(f"  *** could not delete throwaway project {scratch_name!r} ***")
        else:
            print(f"  *** did not get back to {home_name!r}; {scratch_name!r} left in place ***")

print(f"\n{'PASS' if ran and not fails else 'FAIL'}: {len(fails)} failure(s)")
sys.exit(0 if ran and not fails else 1)
