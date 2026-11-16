"""
Live end-to-end check of Generate All Clips Timeline PRO 3.0's in-place trim.

In a throwaway project: builds a source edit from three shots, runs the real
create workflow, grades the result (CDL + a Fusion comp), changes the source
edit, runs the real update workflow, and checks that

  - a shortened shot and an extended last shot are TRIMMED in place (same
    TimelineItem uid) with their grade, comp and linked audio intact,
  - a boxed-in shot is routed to the update track, overwriting nothing,
  - an immediate second update finds nothing to do.

It saves the project you have open, works in RCT_UPD_<pid>, then reloads yours
and deletes the throwaway one, so it only runs when RCT_LIVE_WRITE=1 is set.

    RCT_LIVE_WRITE=1 RCT_PROBE_MEDIA=<video with audio, 300+ frames> py -3.12 -u tests/live_update_trim.py

Runs with "Video Only" off (linked audio). Set RCT_VIDEO_ONLY=1 for the UI
default, where the boxed-in shot goes to the update track instead.

Exit codes: 0 pass, 1 fail, 2 skipped.
"""

import ast
import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
import traceback
import types

api = os.environ.get(
    "RESOLVE_SCRIPT_API",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
)
sys.path.append(os.path.join(api, "Modules"))
os.environ.setdefault(
    "RESOLVE_SCRIPT_LIB",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
)

SKIP = 2
if os.environ.get("RCT_LIVE_WRITE") != "1":
    print("SKIPPED: set RCT_LIVE_WRITE=1 to run (saves the open project, works")
    print("  in a throwaway project, then reloads yours).")
    sys.exit(SKIP)
media_src = os.environ.get("RCT_PROBE_MEDIA", "")
if not os.path.isfile(media_src):
    print("SKIPPED: set RCT_PROBE_MEDIA to a video file with audio (300+ frames).")
    sys.exit(SKIP)


def _repo_root():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        cwd = os.path.abspath(os.getcwd())
        here = cwd if os.path.basename(cwd) == "tests" else os.path.join(cwd, "tests")
    return os.path.dirname(here)


SCRIPT = os.path.join(_repo_root(), "Generate All Clips Timeline PRO.py")

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
print(f"  Resolve {resolve.GetVersionString()}  home project {home_name!r}", flush=True)


def load_script():
    with open(SCRIPT, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    body = [n for n in tree.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                    and getattr(n.value.func, "id", "") == "main")]
    # dataclasses look their module up in sys.modules, so register one.
    module = types.ModuleType("genallpro_live")
    module.resolve = resolve
    sys.modules["genallpro_live"] = module
    exec(compile(ast.Module(body=body, type_ignores=[]), SCRIPT, "exec"),
         module.__dict__)
    return module.__dict__


S = load_script()
print(f"  script version {S['TOOL_VERSION']}", flush=True)

pid = os.getpid()
scratch_name = f"RCT_UPD_{pid}"
failures = []


def check(label, got, want=True):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" +
          ("" if ok else f"   got {got!r}, want {want!r}"), flush=True)
    if not ok:
        failures.append(label)


def lut_hash(item, tag):
    path = os.path.join(tempfile.gettempdir(), f"rct_upd_{pid}_{tag}.cube")
    try:
        if not item.ExportLUT(resolve.EXPORT_LUT_17PTCUBE, path):
            return None
        with open(path, "rb") as fh:
            body = b"".join(l for l in fh.read().splitlines(True)
                            if l[:1].isdigit() or l[:1] in (b"-", b"."))
        return hashlib.md5(body).hexdigest()[:10]
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def run_quiet(fn, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(**kw)
    return buf.getvalue()


def by_name(timeline, kind="video"):
    out = {}
    for t in range(1, (timeline.GetTrackCount(kind) or 0) + 1):
        for item in timeline.GetItemListInTrack(kind, t) or []:
            mpi = item.GetMediaPoolItem()
            out.setdefault(mpi.GetName() if mpi else item.GetName(), []).append(
                (t, item))
    return out


def no_overlaps(timeline):
    for kind in ("video", "audio"):
        for t in range(1, (timeline.GetTrackCount(kind) or 0) + 1):
            spans = sorted((int(i.GetStart()), int(i.GetEnd()))
                           for i in timeline.GetItemListInTrack(kind, t) or [])
            for (_a, e1), (s2, _b) in zip(spans, spans[1:]):
                if s2 < e1:
                    return False
    return True


GRADE = {"NodeIndex": "1", "Slope": "1.4 0.8 0.9", "Offset": "0.02 0 -0.02",
         "Power": "1.1 1 0.9", "Saturation": "0.6"}
VIDEO_ONLY = os.environ.get("RCT_VIDEO_ONLY") == "1"
print(f"  video only: {VIDEO_ONLY}", flush=True)
SETTINGS = dict(selection_method="Current Bin", connection_threshold=25,
                allow_disabled_clips=False, video_only=VIDEO_ONLY,
                mark_duplicates=True, mark_retimed_clips=True,
                use_xml_retime=True, import_clip_names=False,
                merge_by_source_file=True)

media_dir = tempfile.mkdtemp(prefix="rct_upd_media_")
paths = []
for shot in ("shotA", "shotB", "shotC"):
    path = os.path.join(media_dir, f"{shot}.mov")
    shutil.copyfile(media_src, path)
    paths.append(path)

ran = False
try:
    pm.SaveProject()
    project = pm.CreateProject(scratch_name)
    if project is None:
        print("SKIPPED: could not create the throwaway project.")
        sys.exit(SKIP)
    project.SetSetting("timelineFrameRate", "25")
    pool = project.GetMediaPool()
    root = pool.GetRootFolder()
    clips = {c.GetName(): c for c in (pool.ImportMedia(paths) or [])}
    check("three shots imported", len(clips), 3)

    src_bin = pool.AddSubFolder(root, "SRC")
    pool.SetCurrentFolder(src_bin)
    src_tl = pool.CreateEmptyTimeline("Edit_v1")
    project.SetCurrentTimeline(src_tl)
    t0 = src_tl.GetStartFrame()
    rec = t0
    for name in ("shotA.mov", "shotB.mov", "shotC.mov"):
        pool.AppendToTimeline([{"mediaPoolItem": clips[name], "startFrame": 100,
                                "endFrame": 200, "recordFrame": rec}])
        rec += 100

    # ---- create ---------------------------------------------------------
    log = run_quiet(S["run_workflow"], dst_timeline_name="All_Clips",
                    sorting_method="Source Name", preserve_track_layout=False,
                    **SETTINGS)
    allc = project.GetCurrentTimeline()
    check("create built All_Clips", allc.GetName() if allc else None, "All_Clips")
    placed = by_name(allc)
    check("one clip per shot", sorted(placed), ["shotA.mov", "shotB.mov",
                                                "shotC.mov"])
    item_a = placed["shotA.mov"][0][1]
    item_b = placed["shotB.mov"][0][1]
    item_c = placed["shotC.mov"][0][1]
    print(f"  created: " + ", ".join(
        f"{n} rec {int(i.GetStart())}+{int(i.GetDuration())} "
        f"src {i.GetSourceStartFrame()}"
        for n, i in (("A", item_a), ("B", item_b), ("C", item_c))), flush=True)
    uid_a, uid_b, uid_c = (i.GetUniqueId() for i in (item_a, item_b, item_c))
    check("create placed linked audio" if not VIDEO_ONLY else "create placed no audio",
          len(S["linked_others"](item_b)) >= 1, not VIDEO_ONLY)
    dur_a, dur_b, dur_c = (int(i.GetDuration()) for i in (item_a, item_b, item_c))
    check("create placed each shot exactly (no slip widening)",
          (dur_a, dur_b, dur_c), (100, 100, 100))
    audio_count = sum(len(allc.GetItemListInTrack("audio", t) or [])
                      for t in range(1, (allc.GetTrackCount("audio") or 0) + 1))
    check("create left one audio clip per shot (no orphans)", audio_count,
          0 if VIDEO_ONLY else 3)

    # ---- grade the All Clips timeline -------------------------------------
    resolve.OpenPage("color")
    for item in (item_b, item_c):
        item.SetCDL(GRADE)
    item_c.AddFusionComp()
    hash_b, hash_c = lut_hash(item_b, "b0"), lut_hash(item_c, "c0")
    check("grades exported", hash_b is not None and hash_c is not None)
    resolve.OpenPage("edit")

    # ---- change the source edit -------------------------------------------
    project.SetCurrentTimeline(src_tl)
    s_items = {i.GetMediaPoolItem().GetName(): i
               for i in src_tl.GetItemListInTrack("video", 1)}
    # Gaps first so the source trims themselves have room.
    src_tl.MoveItems([s_items["shotC.mov"]], {"startFrame": t0 + 400})
    src_tl.MoveItems([s_items["shotB.mov"]], {"startFrame": t0 + 200})
    src_tl.MoveItems([s_items["shotA.mov"]], {"startFrame": t0 + 50})
    a_ok = s_items["shotA.mov"].SetLeftOffset(s_items["shotA.mov"].GetLeftOffset() - 20)
    b_ok = s_items["shotB.mov"].SetRightOffset(s_items["shotB.mov"].GetRightOffset() + 30)
    c_ok = s_items["shotC.mov"].SetRightOffset(s_items["shotC.mov"].GetRightOffset() - 40)
    check("source edit changed (A head +20, B tail -30, C tail +40)",
          (a_ok, b_ok, c_ok), (True, True, True))

    # ---- update -------------------------------------------------------------
    project.SetCurrentTimeline(allc)
    log = run_quiet(S["run_update_workflow"], source_selection_mode="Recorded",
                    protect_graded_clips=True, dry_run=False,
                    **{k: v for k, v in SETTINGS.items()})
    print("  --- update log (trim lines) ---")
    for line in log.splitlines():
        if any(k in line for k in ("TRIMMED", "SKIP", "ERROR", "WARNING",
                                   "could not", "trimmed in", "placing")):
            print("   " + line.strip())

    resolve.OpenPage("color")   # ExportLUT answers nothing from the Edit page
    after = by_name(allc)
    b_now = [i for _t, i in after["shotB.mov"] if i.GetUniqueId() == uid_b]
    c_now = [i for _t, i in after["shotC.mov"] if i.GetUniqueId() == uid_c]
    check("B is the same item (trimmed, not rebuilt)", len(b_now), 1)
    check("C is the same item (trimmed, not rebuilt)", len(c_now), 1)
    if b_now:
        b = b_now[0]
        check("B lost 30 frames", int(b.GetDuration()), dur_b - 30)
        check("B's grade is unchanged", lut_hash(b, "b1"), hash_b)
        b_audio = S["linked_others"](b)
        check("B's linked audio was trimmed with it",
              [int(a.GetDuration()) for a in b_audio], [dur_b - 30] * len(b_audio))
    if c_now:
        c = c_now[0]
        check("C gained 40 frames", int(c.GetDuration()), dur_c + 40)
        check("C's grade is unchanged", lut_hash(c, "c1"), hash_c)
        check("C's Fusion comp is still there", c.GetFusionCompCount(), 1)
        c_audio = S["linked_others"](c)
        check("C's linked audio was trimmed with it",
              [int(a.GetDuration()) for a in c_audio], [dur_c + 40] * len(c_audio))
    # A starts the timeline, so its head has nowhere to grow. Video only: the
    # updated copy goes to the update track and the original stays. With linked
    # audio that copy is not an option either (an append gives no control over
    # which audio track it lands on), so it is left for a manual update.
    a_items = after["shotA.mov"]
    if VIDEO_ONLY:
        check("A (boxed in) got a copy on the update track, original untouched",
              sorted((t, int(i.GetDuration())) for t, i in a_items),
              [(1, dur_a), (2, dur_a + 20)])
    else:
        check("A (boxed in, linked audio) is left in place for a manual update",
              [(t, int(i.GetDuration())) for t, i in a_items], [(1, dur_a)])
        check("the log marks A for manual attention",
              "SKIP 'shotA.mov'" in log)
    check("nothing overlaps on any track", no_overlaps(allc))

    log2 = run_quiet(S["run_update_workflow"], source_selection_mode="Recorded",
                     protect_graded_clips=True, dry_run=False,
                     **{k: v for k, v in SETTINGS.items()})
    if VIDEO_ONLY:
        check("an immediate second update has nothing to do",
              "Nothing to do" in log2)
    else:
        # A stays on the to-do list until someone updates it by hand; B and C
        # must not be touched again.
        check("a second update leaves the trimmed shots alone",
              ("TRIMMED" in log2, "1 range change(s)" in log2), (False, True))
    if "TRIMMED" in log2 or ("Nothing to do" not in log2
                             and "1 range change(s)" not in log2):
        print(log2[-1500:])
    ran = True
except Exception:  # noqa: BLE001
    traceback.print_exc()
finally:
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
        print(f"  *** did not get back to {home_name!r}; {scratch_name!r} left ***")
    shutil.rmtree(media_dir, ignore_errors=True)

if not ran:
    sys.exit(1)
print("\n  RESULT: " + ("PASS" if not failures else f"FAIL ({len(failures)})"))
sys.exit(0 if not failures else 1)
