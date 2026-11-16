"""
Live capability probes for the Resolve 21.1+ editing APIs that could let
Generate All Clips Timeline PRO's update mode trim and move clips in place
instead of deleting and re-appending them (which loses grades).

    RCT_LIVE_WRITE=1 RCT_PROBE_MEDIA=<video file with audio> py -3.12 -u tests/live_trim_probe.py

Works in a throwaway project: saves the project you have open, creates
RCT_TRIM_<pid>, probes there, reloads your project and deletes the throwaway
one. Nothing is asserted - every line is a measured answer for the design.

Exit codes: 0 ran, 1 harness error, 2 skipped.
"""

import hashlib
import os
import sys
import tempfile
import traceback

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

media_path = os.environ.get("RCT_PROBE_MEDIA", "")
if not os.path.isfile(media_path):
    print("SKIPPED: set RCT_PROBE_MEDIA to a video file with audio (300+ frames).")
    sys.exit(SKIP)

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
print(f"  Resolve {resolve.GetVersionString()}  home project {home_name!r}",
      flush=True)

pid = os.getpid()
scratch_name = f"RCT_TRIM_{pid}"


def note(key, answer, detail=""):
    print(f"  {key:<34} {answer}" + (f"   {detail}" if detail else ""),
          flush=True)


def call(obj, name, *args, **kw):
    fn = getattr(obj, name, None)
    if not callable(fn):
        return "<absent>"
    try:
        return fn(*args, **kw)
    except Exception as exc:  # noqa: BLE001
        return f"<raised {exc}>"


def snap(item):
    if item is None:
        return None
    return {
        "start": call(item, "GetStart"), "end": call(item, "GetEnd"),
        "dur": call(item, "GetDuration"),
        "src": (call(item, "GetSourceStartFrame"),
                call(item, "GetSourceEndFrame")),
        "L": call(item, "GetLeftOffset"), "R": call(item, "GetRightOffset"),
        "track": call(item, "GetTrackTypeAndIndex"),
    }


def fmt(s):
    if s is None:
        return "None"
    return (f"rec {s['start']}-{s['end']} dur {s['dur']} src {s['src'][0]}-"
            f"{s['src'][1]} L{s['L']} R{s['R']}")


def lut_hash(item, tag):
    path = os.path.join(tempfile.gettempdir(), f"rct_trim_{pid}_{tag}.cube")
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


GRADE = {"NodeIndex": "1", "Slope": "1.4 0.8 0.9", "Offset": "0.02 0 -0.02",
         "Power": "1.1 1 0.9", "Saturation": "0.6"}

ran = False
project = None
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
    frames = int(mpi.GetClipProperty("Frames") or 0)
    print(f"  media {os.path.basename(media_path)}: {frames} frames, "
          f"type {mpi.GetClipProperty('Type')!r}", flush=True)

    tl = pool.CreateEmptyTimeline("probe")
    project.SetCurrentTimeline(tl)
    T0 = tl.GetStartFrame()
    print(f"  timeline starts at {T0}", flush=True)

    def place(s, e_excl, rec, track=1, video_only=True):
        info = {"mediaPoolItem": mpi, "startFrame": s, "endFrame": e_excl,
                "trackIndex": track, "recordFrame": T0 + rec}
        if video_only:
            info["mediaType"] = 1
        got = pool.AppendToTimeline([info]) or []
        vids = [i for i in got if (call(i, "GetTrackTypeAndIndex") or [""])[0]
                == "video"]
        return vids[0] if vids else (got[0] if got else None)

    def track_items(kind="video", idx=1):
        return tl.GetItemListInTrack(kind, idx) or []

    # ---- T1: offset semantics ------------------------------------------
    print("\n  -- T1 offset semantics (src 100..199 at rec +100) --")
    a = place(100, 200, 100)
    s = snap(a)
    note("T1 snap", fmt(s))
    note("T1 L == source start?", s["L"] == 100, f"L={s['L']}")
    note("T1 R == frames - end_excl?", s["R"] == frames - 200,
         f"R={s['R']} frames={frames}")
    note("T1 GetType", call(a, "GetType"))
    note("T1 GetSpeed", call(a, "GetSpeed"))
    note("T1 GetPosition", call(a, "GetPosition"))

    # ---- T2: extend / shorten head into a free gap ---------------------
    print("\n  -- T2 SetLeftOffset (gap of 100f before) --")
    r = call(a, "SetLeftOffset", s["L"] - 10)
    s2 = snap(a)
    note("T2 extend head 10 ->", r, fmt(s2))
    note("T2   record start moved?", s2["start"] - s["start"])
    note("T2   record end moved?", s2["end"] - s["end"])
    r = call(a, "SetLeftOffset", s2["L"] + 20)
    s3 = snap(a)
    note("T2 shorten head 20 ->", r, fmt(s3))
    note("T2   record start moved?", s3["start"] - s2["start"])
    note("T2   record end moved?", s3["end"] - s2["end"])

    # ---- T3: tail, with a downstream neighbour (ripple?) ---------------
    print("\n  -- T3 SetRightOffset (neighbour 50f after) --")
    b = place(250, 300, (s3["end"] - T0) + 50)  # neighbour, gap 50
    sb = snap(b)
    sa = snap(a)
    r = call(a, "SetRightOffset", sa["R"] - 10)
    sa2, sb2 = snap(a), snap(b)
    note("T3 extend tail 10 ->", r, fmt(sa2))
    note("T3   start moved?", sa2["start"] - sa["start"])
    note("T3   neighbour moved?", sb2["start"] - sb["start"])
    r = call(a, "SetRightOffset", sa2["R"] + 15)
    sa3, sb3 = snap(a), snap(b)
    note("T3 shorten tail 15 ->", r, fmt(sa3))
    note("T3   neighbour moved?", sb3["start"] - sb2["start"])

    # ---- T4: extend tail INTO the neighbour ----------------------------
    print("\n  -- T4 extend tail past the gap into the neighbour --")
    gap = sb3["start"] - sa3["end"]
    r = call(a, "SetRightOffset", sa3["R"] - (gap + 10))
    sa4, sb4 = snap(a), snap(b)
    note(f"T4 extend {gap + 10} (gap {gap}) ->", r, fmt(sa4))
    note("T4   neighbour", fmt(sb4))
    note("T4   items on track", len(track_items()))
    # extend exactly to the gap
    sa4 = snap(a)
    gap = sb4["start"] - sa4["end"]
    r = call(a, "SetRightOffset", sa4["R"] - gap)
    note(f"T4 extend exactly gap {gap} ->", r, fmt(snap(a)))

    # ---- T5: past media bounds -----------------------------------------
    print("\n  -- T5 beyond media --")
    c = place(5, 50, 1000)
    sc = snap(c)
    r = call(c, "SetLeftOffset", -5)
    note("T5 SetLeftOffset(-5) ->", r, fmt(snap(c)))
    r = call(c, "SetLeftOffset", 0)
    note("T5 SetLeftOffset(0) ->", r, fmt(snap(c)))
    sc = snap(c)
    r = call(c, "SetRightOffset", -10)
    note("T5 SetRightOffset(-10) ->", r, fmt(snap(c)))
    r = call(c, "SetLeftOffset", sc["L"] + sc["dur"] + 5)
    note("T5 shorten past zero length ->", r, fmt(snap(c)))

    # ---- T6: grade / versions / fusion survive trim + move -------------
    print("\n  -- T6 work survives trim and move --")
    d = place(100, 200, 1200)
    project_page = resolve.GetCurrentPage()
    resolve.OpenPage("color")
    note("T6 SetCDL", call(d, "SetCDL", GRADE))
    call(d, "AddVersion", "v2", 0)
    call(d, "LoadVersionByName", "v2", 0)
    call(d, "SetCDL", {**GRADE, "Saturation": "0.2"})
    comp = call(d, "AddFusionComp")
    h0 = lut_hash(d, "d0")
    v0 = call(d, "GetVersionNameList", 0)
    f0 = call(d, "GetFusionCompCount")
    note("T6 before", f"lut {h0} versions {v0} comps {f0}")
    note("T6 GetCDL(1)", call(d, "GetCDL", 1))
    sd = snap(d)
    call(d, "SetLeftOffset", sd["L"] - 20)
    call(d, "SetRightOffset", snap(d)["R"] - 20)
    h1 = lut_hash(d, "d1")
    note("T6 after trim", f"lut {h1} versions {call(d, 'GetVersionNameList', 0)} "
         f"comps {call(d, 'GetFusionCompCount')}  same grade: {h1 == h0}",
         fmt(snap(d)))
    r = call(d, "SetPosition", {"startFrame": T0 + 1600})
    h2 = lut_hash(d, "d2")
    note("T6 SetPosition +rec ->", r, fmt(snap(d)))
    note("T6 after move", f"lut {h2} same grade: {h2 == h0} "
         f"comps {call(d, 'GetFusionCompCount')}")
    r = call(d, "SetPosition", {"trackIndex": 2})
    note("T6 SetPosition track 2 ->", r,
         f"{fmt(snap(d))} V2 items {len(track_items('video', 2))} "
         f"(track count {tl.GetTrackCount('video')})")
    tl.AddTrack("video")
    r = call(d, "SetPosition", {"trackIndex": 2})
    note("T6 SetPosition track 2 (exists) ->", r, fmt(snap(d)))
    h3 = lut_hash(d, "d3")
    note("T6 after track move", f"same grade: {h3 == h0}")
    if project_page:
        resolve.OpenPage(project_page)

    # ---- T7: moves into occupied space ---------------------------------
    print("\n  -- T7 SetPosition / MoveItems onto another clip --")
    e = place(100, 150, 2000)
    f = place(100, 150, 2100)
    se, sf = snap(e), snap(f)
    r = call(f, "SetPosition", {"startFrame": se["start"] + 10})
    note("T7 SetPosition overlapping ->", r, f"e {fmt(snap(e))} | f {fmt(snap(f))}")
    r = call(tl, "MoveItems", [f], {"startFrame": T0 + 2300})
    note("T7 MoveItems to free ->", r, fmt(snap(f)))
    r = call(tl, "MoveItems", [e, f], {"startFrame": T0 + 2500})
    note("T7 MoveItems pair ->", r, f"e {fmt(snap(e))} | f {fmt(snap(f))}")
    r = call(tl, "MoveItems", [e], {"startFrame": snap(f)["start"]})
    note("T7 MoveItems onto f ->", r, f"e {fmt(snap(e))} | f {fmt(snap(f))}")
    note("T7 items on V1", len(track_items()))

    # ---- T8: linked audio follows a trim --------------------------------
    print("\n  -- T8 linked audio --")
    g = place(100, 200, 3000, video_only=False)
    linked = call(g, "GetLinkedItems") or []
    aud = [i for i in linked if (call(i, "GetTrackTypeAndIndex") or [""])[0]
           == "audio"]
    note("T8 linked items", len(linked), f"audio {len(aud)}")
    if aud:
        sg, sa_ = snap(g), snap(aud[0])
        note("T8 before", f"v {fmt(sg)} | a {fmt(sa_)}")
        r = call(g, "SetLeftOffset", sg["L"] - 10)
        note("T8 extend head (linked=True) ->", r,
             f"v {fmt(snap(g))} | a {fmt(snap(aud[0]))}")
        r = call(g, "SetRightOffset", snap(g)["R"] + 10, False, False)
        note("T8 shorten tail (linked=False) ->", r,
             f"v {fmt(snap(g))} | a {fmt(snap(aud[0]))}")
        r = call(g, "SetPosition", {"startFrame": T0 + 3500})
        note("T8 SetPosition (moveLinked=True) ->", r,
             f"v {fmt(snap(g))} | a {fmt(snap(aud[0]))}")

    # ---- T9: speed ------------------------------------------------------
    print("\n  -- T9 GetSpeed / SetSpeed --")
    h = place(100, 200, 4000)
    note("T9 GetSpeed plain", call(h, "GetSpeed"), fmt(snap(h)))
    r = call(h, "SetSpeed", {"Percentage": 50.0})
    note("T9 SetSpeed 50 ->", r, f"{call(h, 'GetSpeed')}  {fmt(snap(h))}")
    r = call(h, "SetSpeed", {"Percentage": 200.0})
    note("T9 SetSpeed 200 ->", r, f"{call(h, 'GetSpeed')}  {fmt(snap(h))}")
    r = call(h, "SetSpeed", {"Percentage": 0.0})
    note("T9 SetSpeed 0 (freeze) ->", r, f"{call(h, 'GetSpeed')}  {fmt(snap(h))}")
    note("T9 GetProperty('Speed')", call(h, "GetProperty", "Speed"))
    i_ = place(100, 200, 4500)
    call(i_, "SetSpeed", {"Percentage": 50.0})
    si = snap(i_)
    r = call(i_, "SetLeftOffset", si["L"] - 10)
    note("T9 trim a 50% clip head 10 ->", r, f"before {fmt(si)} | after {fmt(snap(i_))}")

    # ---- T10: CopyAttributes ------------------------------------------
    print("\n  -- T10 CopyAttributes --")
    j = place(100, 150, 5000)
    k = place(100, 150, 5100)
    call(j, "SetProperty", "ZoomX", 1.5)
    call(j, "SetProperty", "CropLeft", 40.0)
    call(j, "SetClipColor", "Orange")
    r = call(j, "CopyAttributes", [k], {"allVideoAttributes": True,
                                        "clipColor": True})
    note("T10 CopyAttributes ->", r,
         f"k ZoomX {call(k, 'GetProperty', 'ZoomX')} CropLeft "
         f"{call(k, 'GetProperty', 'CropLeft')} colour {call(k, 'GetClipColor')}")

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
        if not pm.DeleteProject(scratch_name):
            print(f"  *** could not delete throwaway project {scratch_name!r} ***")
        else:
            print(f"  reloaded {home_name!r}, deleted {scratch_name!r}")
    else:
        print(f"  *** did not get back to {home_name!r}; throwaway project "
              f"{scratch_name!r} left in place ***")

sys.exit(0 if ran else 1)
