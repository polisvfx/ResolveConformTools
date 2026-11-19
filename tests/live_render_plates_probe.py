"""
Live check for Render Scaled Plates, end to end, in a throwaway project.

Writes, so it only runs when opted in:

    RCT_LIVE_WRITE=1 py -3.12 -u tests/live_render_plates_probe.py

It never touches the project you have open. It creates a project called
RCT_RenderPlates_PROBE, generates four test plates with ffmpeg (3840x2160,
4096x2160, 2048x858, 1280x720), cuts a source timeline from them, and runs the
script's real create, queue, render and re-submit paths. It then checks the
render timelines, the queued jobs and the rendered files (with ffprobe), loads
your project again, and deletes the probe project. Set RCT_KEEP_PROBE=1 to keep
the project for a look.

Exit 0 pass, 1 fail, 2 skipped (no Resolve, no ffmpeg, or not opted in).
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time

SKIP = 2
if os.environ.get("RCT_LIVE_WRITE") != "1":
    print("SKIPPED: set RCT_LIVE_WRITE=1 to run (creates and deletes a scratch project).")
    sys.exit(SKIP)
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    print("SKIPPED: ffmpeg and ffprobe are needed to make and check the test plates.")
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
SCRIPT = os.path.join(REPO, "Render Scaled Plates.py")
PROBE_PROJECT = "RCT_RenderPlates_PROBE"


def load_script():
    """Every definition in the script, without the trailing main() call."""
    with open(SCRIPT, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    body = [n for n in tree.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                    and getattr(n.value.func, "id", None) == "main")]
    ns = {"__name__": "rsp_probe"}
    exec(compile(ast.Module(body=body, type_ignores=[]), SCRIPT, "exec"), ns)
    return ns


import DaVinciResolveScript as dvr  # noqa: E402

resolve = dvr.scriptapp("Resolve")
if resolve is None:
    print("SKIPPED: Resolve is not running, or external scripting is disabled.")
    sys.exit(SKIP)
pm = resolve.GetProjectManager()
home = pm.GetCurrentProject()
if home is None:
    print("SKIPPED: no project is open (needed to return to afterwards).")
    sys.exit(SKIP)
home_name = home.GetName()
print(f"Resolve {resolve.GetVersionString()}, home project '{home_name}'")

rsp = load_script()
fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       got  {got!r}")
        print(f"       want {want!r}")
        fails.append(label)


def probe_dims(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def has_audio(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip()
    return bool(out)


work = tempfile.mkdtemp(prefix="rct_rsp_")
media_dir = os.path.join(work, "media")
out_dir = os.path.join(work, "out")
os.makedirs(media_dir)
SIZES = ["3840x2160", "4096x2160", "2048x858", "1280x720"]
for size in SIZES:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={size}:rate=25:duration=4", "-c:v", "prores_ks",
                    "-profile:v", "0", os.path.join(media_dir, f"plate_{size}.mov")],
                   check=True)

if PROBE_PROJECT in (pm.GetProjectListInCurrentFolder() or []):
    pm.DeleteProject(PROBE_PROJECT)
project = pm.CreateProject(PROBE_PROJECT)
if project is None:
    print(f"FAIL: could not create '{PROBE_PROJECT}'")
    sys.exit(1)

try:
    project.SetSetting("timelineFrameRate", "25")
    mp = project.GetMediaPool()
    clips = {c.GetName(): c for c in mp.ImportMedia(
        [os.path.join(media_dir, f"plate_{s}.mov") for s in SIZES])}
    src = mp.CreateEmptyTimeline("SRC_Edit")
    project.SetCurrentTimeline(src)
    # Two overlapping uses of the UHD plate (merge into one), one of each other.
    mp.AppendToTimeline([
        {"mediaPoolItem": clips["plate_3840x2160.mov"], "startFrame": 10, "endFrame": 40},
        {"mediaPoolItem": clips["plate_4096x2160.mov"], "startFrame": 0, "endFrame": 20},
        {"mediaPoolItem": clips["plate_3840x2160.mov"], "startFrame": 30, "endFrame": 60},
        {"mediaPoolItem": clips["plate_2048x858.mov"], "startFrame": 5, "endFrame": 25},
        {"mediaPoolItem": clips["plate_1280x720.mov"], "startFrame": 0, "endFrame": 10},
    ])
    items = src.GetItemListInTrack("video", 1)
    items[-1].SetClipEnabled(False)  # the 720p use is disabled: left out

    base = "Probe"
    opts = {"mode": "height", "value": 1080, "allow_upscale": False,
            "copy_grades": True, "merge": True, "existing": "reuse",
            # Resolve leaves the last appended clip selected; take them all.
            "scope": "all",
            "base_name": base, "dry_run": False, "preset": "ProRes 422 HQ",
            "target_dir": out_dir, "subfolders": True, "export_audio": False,
            "start_render": False}

    print("\n--- create ---")
    entries = rsp["run_create"](project, opts)
    names = sorted(rsp["tl_name"](tl) for tl, _ in entries)
    check("one render timeline per output resolution",
          names, ["Probe_1920x1080", "Probe_2048x1080", "Probe_2048x858"])
    for tl, manifest in entries:
        w, h = manifest["resolution"]
        check(f"{rsp['tl_name'](tl)} timeline resolution",
              (tl.GetSetting("timelineResolutionWidth"),
               tl.GetSetting("timelineResolutionHeight")), (str(w), str(h)))
    by_name = {rsp["tl_name"](tl): tl for tl, _ in entries}
    uhd_items = by_name["Probe_1920x1080"].GetItemListInTrack("video", 1)
    check("the two overlapping UHD uses became one plate", len(uhd_items), 1)
    if uhd_items:
        check("covering source 10-59",
              (uhd_items[0].GetSourceStartFrame(), uhd_items[0].GetDuration()), (10, 50))
    tl_1080 = by_name["Probe_1920x1080"]
    check("plates are video only",
          sum(len(tl_1080.GetItemListInTrack("audio", i) or [])
              for i in range(1, (tl_1080.GetTrackCount("audio") or 0) + 1)), 0)
    check("the source timeline is current again",
          rsp["tl_name"](project.GetCurrentTimeline()), "SRC_Edit")
    check("found again by manifest", len(rsp["find_render_timelines"](project)), 3)

    print("\n--- queue + render ---")
    jobs = rsp["run_queue"](project, entries, opts)
    check("one job per render timeline", len(jobs), 3)
    joblist = {j["JobId"]: j for j in project.GetRenderJobList()}
    for jid in jobs:
        job = joblist.get(jid, {})
        print(f"     job {job.get('TimelineName')}: {job.get('FormatWidth')}x"
              f"{job.get('FormatHeight')} {job.get('RenderMode')} {job.get('PresetName')}")
        check(f"{job.get('TimelineName')} is Individual clips",
              job.get("RenderMode"), "Individual clips")
    project.StartRendering(jobs, False)
    t0 = time.time()
    while project.IsRenderingInProgress() and time.time() - t0 < 300:
        time.sleep(1)
    for res in ("1920x1080", "2048x1080", "2048x858"):
        folder = os.path.join(out_dir, res)
        files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        check(f"{res}: files rendered into their own subfolder", len(files) >= 1, True)
        for f in files:
            want = tuple(int(v) for v in res.split("x"))
            check(f"  {f} is {res}", probe_dims(os.path.join(folder, f)), want)
            check(f"  {f} has no audio", has_audio(os.path.join(folder, f)), False)

    print("\n--- re-submit ---")
    count_before = project.GetTimelineCount()
    found = rsp["find_render_timelines"](project)
    check("every render timeline records when it was queued",
          all(m.get("last_queued_utc") for _, m in found), True)
    jobs2 = rsp["run_queue"](project, found, dict(opts, preset="H.264 Master"))
    check("re-submit queues again", len(jobs2), 3)
    check("re-submit builds nothing", project.GetTimelineCount(), count_before)
    check("and remembers the new preset",
          sorted({m.get("last_preset") for _, m in rsp["find_render_timelines"](project)}),
          ["H.264 Master"])

    print("\n--- create again, policies ---")
    project.SetCurrentTimeline(src)
    again = rsp["run_create"](project, opts)
    check("reuse: nothing new built", project.GetTimelineCount(), count_before)
    check("reuse: all three handed on to the queue", len(again), 3)
    rsp["run_create"](project, dict(opts, existing="new"))
    check("new: three more alongside", project.GetTimelineCount(), count_before + 3)
    check("new: _v2 names",
          sorted(n for n in (rsp["tl_name"](t) for t in rsp["list_timelines"](project))
                 if n.endswith("_v2")),
          ["Probe_1920x1080_v2", "Probe_2048x1080_v2", "Probe_2048x858_v2"])
    rsp["run_create"](project, dict(opts, existing="replace"))
    check("replace: count unchanged", project.GetTimelineCount(), count_before + 3)

    print("\n--- In and Out ---")
    project.SetCurrentTimeline(src)
    starts = sorted(i.GetStart() for i in src.GetItemListInTrack("video", 1))
    t0 = src.GetStartFrame()
    # From the last frame of the DCI clip (the 2nd) to the first of the 2048x858
    # one (the 4th): DCI, the second UHD use and the scope plate. Marks are
    # offsets from the timeline start, Out inclusive.
    src.SetMarkInOut(starts[2] - 1 - t0, starts[3] - t0)
    plates, label, _ = rsp["collect_plates"](src, mode="height", value=1080,
                                             allow_upscale=False, scope="inout")
    print(f"     {label}")
    check("In-Out picks the clips touching the marked range",
          sorted(p["name"] for p in plates),
          ["plate_2048x858.mov", "plate_3840x2160.mov", "plate_4096x2160.mov"])
    uhd = [p for p in plates if p["name"] == "plate_3840x2160.mov"]
    check("whole clips, not trimmed to the marks",
          [(p["start"], p["end"]) for p in uhd], [(30, 59)])
    src.SetMarkInOut(starts[4] - t0, starts[4] + 2 - t0)  # only the disabled 720p use
    plates, _, _ = rsp["collect_plates"](src, mode="height", value=1080,
                                         allow_upscale=False, scope="inout")
    check("a disabled clip inside In-Out is left out", plates, [])
finally:
    project.DeleteAllRenderJobs()
    pm.SaveProject()
    pm.LoadProject(home_name)
    if os.environ.get("RCT_KEEP_PROBE") == "1":
        print(f"\nKept project '{PROBE_PROJECT}' and files in {work}")
    else:
        print(f"\nDeleted probe project: {pm.DeleteProject(PROBE_PROJECT)}")
        shutil.rmtree(work, ignore_errors=True)

print("")
if fails:
    print(f"RESULT: FAIL ({len(fails)} failed)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
