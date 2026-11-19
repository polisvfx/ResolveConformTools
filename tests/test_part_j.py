"""
Part J test: Render Scaled Plates.

The script calls main() at import (and would open a Fusion UIManager dialog), so
the real functions are extracted from source via ast and exec'd into one
namespace, the same way as Part E. The pure rules (target resolution, grouping,
merging, naming, manifest) are tested directly; plate collection, the manifest
stores and the slip-corrected append are driven against stub Resolve objects.

No Resolve needed.
"""

import __future__
import ast
import json
import os
import re
import sys
from datetime import datetime, timezone


def _repo_root():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        cwd = os.path.abspath(os.getcwd())
        here = cwd if os.path.basename(cwd) == "tests" else os.path.join(cwd, "tests")
    return os.path.dirname(here)


REPO = _repo_root()
SCRIPT = os.path.join(REPO, "Render Scaled Plates.py")
WANTED = [
    "summary_headline", "custom_name_for",
    "parse_resolution", "round_even", "compute_target", "merge_plate_ranges",
    "group_by_resolution", "resolution_label", "render_timeline_name",
    "unique_name", "target_dir_for", "utc_now_iso", "build_manifest",
    "encode_manifest", "decode_manifest", "normalise_source_range",
    "parse_scale_value", "plan_lines", "_call", "item_uid", "collect_plates",
    "timeline_markers", "read_manifest", "write_manifest", "placed_source_range",
    "append_plate", "tl_name", "mark_range", "overlaps_range", "range_label",
    "initial_values", "factor_to_slider", "slider_to_factor_text",
]

_SELF_CONTAINED = (
    ast.Constant, ast.BinOp, ast.UnaryOp, ast.Tuple, ast.List, ast.Set, ast.Dict,
    ast.operator, ast.unaryop, ast.expr_context,
)


def extract(path, names):
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    consts = [n for n in tree.body
              if isinstance(n, ast.Assign) and len(n.targets) == 1
              and isinstance(n.targets[0], ast.Name) and n.targets[0].id.isupper()
              and all(isinstance(s, _SELF_CONTAINED) for s in ast.walk(n.value))]
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = set(names) - {n.name for n in nodes}
    if missing:
        raise SystemExit(f"not found in source: {sorted(missing)}")
    ns = {"print": print, "json": json, "os": os, "re": re,
          "datetime": datetime, "timezone": timezone}
    exec(compile(ast.Module(body=consts + nodes, type_ignores=[]), path, "exec",
                 flags=__future__.annotations.compiler_flag), ns)
    return ns


ns = extract(SCRIPT, WANTED)
fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       got  {got!r}")
        print(f"       want {want!r}")
        fails.append(label)


def raises(label, fn, *args):
    try:
        fn(*args)
    except ValueError:
        print(f"ok   {label}")
        return
    print(f"FAIL {label} (no ValueError)")
    fails.append(label)


# --- the script header ------------------------------------------------------

with open(SCRIPT, encoding="utf-8") as fh:
    header = fh.read(3000)
m = re.search(r"Version:\s*(\d+\.\d+)", header)
check("the script carries a Version header", bool(m), True)
check("TOOL_VERSION matches the Version header",
      ns["TOOL_VERSION"], m.group(1) if m else None)

# --- parse_resolution -------------------------------------------------------

pr = ns["parse_resolution"]
check("parses 3840x2160", pr("3840x2160"), (3840, 2160))
check("tolerates spaces and capital X", pr(" 4096 X 2160 "), (4096, 2160))
check("None for empty", pr(""), None)
check("None for None", pr(None), None)
check("None for junk", pr("Unknown"), None)
check("None for zero", pr("0x1080"), None)

# --- compute_target ---------------------------------------------------------

ct = ns["compute_target"]
check("height 1080 on UHD", ct(3840, 2160, "height", 1080), (1920, 1080, ""))
check("height 1080 on DCI 4K", ct(4096, 2160, "height", 1080), (2048, 1080, ""))
check("height 1080 on a scope 2048x858 is native (smaller)",
      ct(2048, 858, "height", 1080), (2048, 858, "native"))
check("height 1080 on 1280x720 is native without upscale",
      ct(1280, 720, "height", 1080), (1280, 720, "native"))
check("height 1080 on 1280x720 scales up when allowed",
      ct(1280, 720, "height", 1080, True), (1920, 1080, ""))
check("width 1920 on DCI 4K: odd height rounds to even",
      ct(4096, 2160, "width", 1920), (1920, 1012, ""))
check("factor 0.5 on UHD", ct(3840, 2160, "factor", 0.5), (1920, 1080, ""))
check("factor 0.5 on 2048x858 -> 1024x430 (429 rounds to even)",
      ct(2048, 858, "factor", 0.5), (1024, 430, ""))
check("factor 1 is the same size", ct(1920, 1080, "factor", 1), (1920, 1080, "same"))
check("factor 2 without upscale is native",
      ct(1920, 1080, "factor", 2), (1920, 1080, "native"))
check("height equal to the source is 'same', not 'native'",
      ct(1920, 1080, "height", 1080), (1920, 1080, "same"))
check("every output dimension is even",
      all(v % 2 == 0 for w in (1997, 3423, 4608, 6144) for h in (857, 2161, 3888)
          for mode, val in (("height", 1001), ("width", 1001), ("factor", 0.37))
          for v in ct(w, h, mode, val)[:2] if ct(w, h, mode, val)[2] != "native"),
      True)
raises("an unknown mode is refused", ct, 1920, 1080, "diagonal", 1)
raises("a zero value is refused", ct, 1920, 1080, "factor", 0)

# --- parse_scale_value ------------------------------------------------------

psv = ns["parse_scale_value"]
check("height text to int", psv("height", "1080"), 1080)
check("factor accepts 0.5", psv("factor", "0.5"), 0.5)
check("factor accepts a comma", psv("factor", "0,25"), 0.25)
check("factor accepts 50%", psv("factor", "50%"), 0.5)
raises("height refuses a fraction", psv, "height", "1080.5")
raises("height refuses empty", psv, "height", "")
raises("factor refuses negative", psv, "factor", "-1")
raises("width refuses text", psv, "width", "big")

# --- file naming override -------------------------------------------------

cn = ns["custom_name_for"]
check("preset naming sends no CustomName", cn("preset"), None)
check("source name token", cn("source"), "%{Source Name}")
check("clip name token", cn("clip"), "%{Clip Name}")
check("an unknown choice keeps the preset's", cn("bogus"), None)
check("every choice has a label", sorted(ns["FILE_NAMING_LABELS"]), sorted(ns["FILE_NAMINGS"]))

# --- summary headline --------------------------------------------------------

sh = ns["summary_headline"]
check("create, queued, rendering", sh("create", 6, 6, 6, True, False),
      "Built or re-used 6 render timeline(s), queued 6 of 6 job(s), rendering started.")
check("a short queue is visible", sh("create", 3, 2, 3, None, False),
      "Built or re-used 3 render timeline(s), queued 2 of 3 job(s).")
check("re-submit, render refused", sh("resubmit", 2, 2, 2, False, False),
      "Queued 2 of 2 job(s), rendering did NOT start.")
check("dry run", sh("create", 3, 0, 3, None, True),
      "Dry run: nothing was changed. The plan is below.")

# --- GUI value defaults and the factor slider -------------------------------

iv = ns["initial_values"]
check("defaults: height 2160, width 4096", iv(None),
      {"height": "2160", "width": "4096", "factor": "0.5"})
check("each rule remembers its own last value",
      iv({"height": "1080", "factor": "0.25"}),
      {"height": "1080", "width": "4096", "factor": "0.25"})
check("blank or junk saved values fall back", iv({"width": "  ", "height": 5}),
      {"height": "2160", "width": "4096", "factor": "0.5"})
check("every default parses", [psv(m, iv(None)[m]) for m in ("height", "width", "factor")],
      [2160, 4096, 0.5])

f2s, s2f = ns["factor_to_slider"], ns["slider_to_factor_text"]
check("slider: 0.5 is the middle", f2s(0.5), 50)
check("slider: 1.0 is the top", f2s(1.0), 100)
check("slider: a factor above 1 pins to the top", f2s(1.5), 100)
check("slider: 0 pins to the lowest step, never 0", f2s(0), 1)
check("slider text", (s2f(50), s2f(100), s2f(1), s2f(33)), ("0.5", "1", "0.01", "0.33"))
check("slider round trip", all(f2s(float(s2f(v))) == v for v in range(1, 101)), True)

# --- merge / group / name ---------------------------------------------------


def plate(key, start, end, order, target=(1920, 1080)):
    return {"key": key, "name": key, "start": start, "end": end, "order": order,
            "target": target, "src_res": (3840, 2160), "note": "", "uses": 1}


mp = ns["merge_plate_ranges"]
merged = mp([plate("A", 100, 200, 3), plate("B", 0, 50, 1), plate("A", 150, 300, 2),
             plate("A", 301, 320, 5), plate("A", 400, 450, 4)])
check("overlapping and touching uses merge; a gap stays separate",
      [(p["key"], p["start"], p["end"], p["uses"]) for p in merged],
      [("B", 0, 50, 1), ("A", 100, 320, 3), ("A", 400, 450, 1)])
check("a merged plate takes the earliest order",
      [p["order"] for p in merged], [1, 2, 4])
src = [plate("A", 1, 2, 1)]
mp(src + [plate("A", 2, 5, 2)])
check("merging does not modify its input", src[0]["end"], 2)

groups = ns["group_by_resolution"]([plate("A", 0, 1, 2, (2048, 1080)),
                                    plate("B", 0, 1, 1, (1920, 1080)),
                                    plate("C", 0, 1, 0, (2048, 1080))])
check("groups by target resolution", sorted(groups), [(1920, 1080), (2048, 1080)])
check("a group keeps edit order", [p["key"] for p in groups[(2048, 1080)]], ["C", "A"])

check("timeline name", ns["render_timeline_name"]("Spot_plates", 1920, 1080),
      "Spot_plates_1920x1080")
check("empty base falls back", ns["render_timeline_name"]("  ", 2, 2), "Plates_2x2")
check("unique_name keeps a free name", ns["unique_name"]("A", {"B"}), "A")
check("unique_name counts up", ns["unique_name"]("A", {"A", "A_v2"}), "A_v3")

tdf = ns["target_dir_for"]
check("subfolder per resolution", tdf(os.path.join("x", "out"), 1920, 1080, True),
      os.path.join("x", "out", "1920x1080"))
check("no subfolder", tdf("out", 1920, 1080, False), "out")
check("empty dir means the preset's own", tdf("", 1920, 1080, True), "")

lines = ns["plan_lines"]({(1920, 1080): [dict(plate("A", 0, 9, 0), uses=2)],
                          (1280, 720): [dict(plate("B", 0, 9, 1, (1280, 720)),
                                             note="native", src_res=(1280, 720))]},
                         "S")
check("plan lists the biggest resolution first", lines[0], "S_1920x1080  (1 plate)")
check("plan mentions merged uses", "[2 uses merged]" in lines[1], True)
check("plan flags native plates", any("kept native" in ln for ln in lines), True)

# --- normalise_source_range -------------------------------------------------

nsr = ns["normalise_source_range"]
check("a human cut: inclusive end kept", nsr(10, 39, 30), (10, 39))
check("an appended clip echoing the exclusive end", nsr(10, 40, 30), (10, 39))
check("a reversed clip is normalised", nsr(40, 10, 31), (10, 40))
check("a 50% retime keeps its whole span", nsr(10, 40, 62), (10, 40))
check("a frame hold is left alone", nsr(10, 11, 50), (10, 11))

# --- manifest ---------------------------------------------------------------

man = ns["build_manifest"](base_name="S", width=1920, height=1080, mode="height",
                           value=1080, allow_upscale=False, source_name="Edit",
                           source_uid="u1", clip_count=4, now_iso="2026-11-19T00:00:00Z")
enc = ns["encode_manifest"](man)
check("manifest round-trips", ns["decode_manifest"](enc), man)
check("marker form (with prefix) decodes",
      ns["decode_manifest"](ns["MANIFEST_MARKER_PREFIX"] + enc), man)
check("someone else's JSON is not ours",
      ns["decode_manifest"](json.dumps({"tool": "Other", "resolution": [1, 1]})), None)
check("a manifest without a resolution is refused",
      ns["decode_manifest"](json.dumps({"tool": ns["TOOL_NAME"]})), None)
check("junk is refused", ns["decode_manifest"]("{nope"), None)

# --- stubs ------------------------------------------------------------------


class MPI:
    def __init__(self, name, res="3840x2160", end=999):
        self.name, self.res, self.end = name, res, end
        self.meta = {}

    def GetName(self):
        return self.name

    def GetMediaId(self):
        return "id-" + self.name

    def GetClipProperty(self, key):
        return {"Resolution": self.res, "PAR": "Square", "End": str(self.end)}.get(key)

    def GetThirdPartyMetadata(self, key):
        return self.meta.get(key)

    def SetThirdPartyMetadata(self, key, value):
        self.meta[key] = value
        return True


class Item:
    _n = 0

    def __init__(self, mpi, start, end, rec, enabled=True, track=("video", 1),
                 duration=None):
        Item._n += 1
        self.uid = f"uid{Item._n}"
        self.mpi, self.s, self.e, self.rec = mpi, start, end, rec
        self.enabled, self.track = enabled, track
        self.duration = duration if duration is not None else abs(end - start) + 1

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.mpi.name if self.mpi else "Title"

    def GetMediaPoolItem(self):
        return self.mpi

    def GetSourceStartFrame(self):
        return self.s

    def GetSourceEndFrame(self):
        return self.e

    def GetDuration(self):
        return self.duration

    def GetStart(self):
        return self.rec

    def GetEnd(self):
        return self.rec + self.duration

    def GetClipEnabled(self):
        return self.enabled

    def GetTrackTypeAndIndex(self):
        return list(self.track)


class TL:
    def __init__(self, tracks, selected=(), disabled_tracks=(), marks=None, start=0):
        self.tracks, self.selected, self.disabled = tracks, list(selected), set(disabled_tracks)
        self.marks, self.start = marks or {"video": {}, "audio": {}}, start
        self.markers, self.mpi = {}, MPI("timeline")
        self.deleted = []

    def GetName(self):
        return "Edit"

    def GetMarkInOut(self):
        return self.marks

    def GetStartFrame(self):
        return self.start

    def GetSelectedClips(self):
        return self.selected

    def GetTrackCount(self, kind):
        return len(self.tracks) if kind == "video" else 0

    def GetIsTrackEnabled(self, kind, idx):
        return idx not in self.disabled

    def GetItemListInTrack(self, kind, idx):
        return self.tracks[idx - 1]

    def GetMediaPoolItem(self):
        return self.mpi

    def GetMarkers(self):
        return dict(self.markers)

    def AddMarker(self, frame, color, name, note, dur, custom):
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note,
                               "duration": dur, "customData": custom}
        return True

    def DeleteMarkerAtFrame(self, frame):
        return self.markers.pop(frame, None) is not None

    def DeleteClips(self, items, ripple):
        self.deleted.extend(items)
        return True


uhd, dci, hd = MPI("uhd"), MPI("dci", "4096x2160"), MPI("hd", "1280x720")
i1 = Item(uhd, 100, 199, 1000)
i2 = Item(dci, 0, 49, 900)
i3 = Item(hd, 10, 19, 1100, enabled=False)
i4 = Item(None, 0, 10, 1200)
i5 = Item(uhd, 300, 349, 2000)
tl = TL([[i1, i2, i3, i4], [i5]])

cp = ns["collect_plates"]
plates, scope, skipped = cp(tl, mode="height", value=1080, allow_upscale=False)
check("no selection: every enabled video clip",
      sorted(p["name"] for p in plates), ["dci", "uhd", "uhd"])
check("scope label says all", scope.startswith("all 3"), True)
check("a clip without media is reported as skipped", [s[0] for s in skipped], ["Title"])
check("targets are computed per plate",
      sorted({p["target"] for p in plates}), [(1920, 1080), (2048, 1080)])

tl.selected = [i3, i5, Item(uhd, 0, 1, 0, track=("audio", 1))]
plates, scope, _ = cp(tl, mode="height", value=1080, allow_upscale=False)
check("a selection wins, disabled or not; audio is ignored",
      sorted(p["name"] for p in plates), ["hd", "uhd"])
check("scope label says selected, and of how many",
      scope, "only the 2 SELECTED clip(s), of 4 enabled; deselect, or choose "
      "All enabled clips, to take them all")
check("the selected smaller plate stays native",
      [p["note"] for p in plates if p["name"] == "hd"], ["native"])

tl.selected = []
tl.disabled = {2}
plates, _, _ = cp(tl, mode="factor", value=0.5, allow_upscale=False)
check("a disabled track is skipped", sorted(p["name"] for p in plates), ["dci", "uhd"])

# --- In and Out ------------------------------------------------------------

mr = ns["mark_range"]
check("marks are offsets from the timeline start, out inclusive",
      mr({"video": {"in": 25, "out": 45}, "audio": {}}, 86400), (86425, 86445))
check("no marks is None", mr({"video": {}, "audio": {}}, 86400), None)
check("an In alone leaves the end open", mr({"video": {"in": 30}}, 100), (130, None))
check("an Out alone leaves the start open", mr({"video": {"out": 9}}, 100), (None, 109))
check("audio marks fill in when video has none",
      mr({"video": {}, "audio": {"in": 1, "out": 2}}, 0), (1, 2))
check("junk is None", mr(None, 0), None)

ov = ns["overlaps_range"]
check("a clip inside the range", ov(110, 120, (100, 200)), True)
check("a clip straddling In comes in", ov(90, 101, (100, 200)), True)
check("a clip ending exactly at In does not (end is exclusive)", ov(90, 100, (100, 200)), False)
check("a clip starting on Out comes in (Out is inclusive)", ov(200, 210, (100, 200)), True)
check("a clip after Out does not", ov(201, 210, (100, 200)), False)
check("open sides", (ov(0, 5, (None, 10)), ov(500, 600, (100, None))), (True, True))
check("range label", ns["range_label"]((None, 10)), "start-10")

# i1 uhd rec 1000-1099, i2 dci 900-949, i3 hd (disabled) 1100-1109, i5 uhd 2000-2049
tl.disabled = set()
tl.marks = {"video": {"in": 940, "out": 1100}}
tl.selected = [i5]
plates, scope, _ = cp(tl, mode="height", value=1080, allow_upscale=False, scope="inout")
check("In-Out: enabled clips touching the range, selection ignored, disabled left out",
      sorted(p["name"] for p in plates), ["dci", "uhd"])
check("In-Out: whole clips, not trimmed to the marks",
      sorted((p["start"], p["end"]) for p in plates), [(0, 49), (100, 199)])
check("In-Out scope label", scope, "2 enabled video clip(s) between In and Out (940-1100)")
tl.marks = {"video": {}, "audio": {}}
plates, scope, _ = cp(tl, mode="height", value=1080, allow_upscale=False, scope="inout")
check("In-Out without marks takes nothing and says why",
      (plates, scope), ([], "no In or Out mark is set on the timeline"))
plates, _, _ = cp(tl, mode="height", value=1080, allow_upscale=False, scope="all")
check("scope 'all' ignores a selection", sorted(p["name"] for p in plates),
      ["dci", "uhd", "uhd"])
tl.selected = []
raises("an unknown scope is refused", lambda: cp(tl, mode="height", value=1, allow_upscale=False,
                                                  scope="bogus"))

# --- manifest stores --------------------------------------------------------

store_tl = TL([[]])
ns["write_manifest"](store_tl, man)
check("written to third-party metadata",
      ns["decode_manifest"](store_tl.mpi.meta.get(ns["MANIFEST_KEY"])), man)
check("and to one marker at frame 0", list(store_tl.markers), [0])
check("read back from metadata", ns["read_manifest"](store_tl), man)
store_tl.mpi.meta.clear()
check("read back from the marker when metadata is gone",
      ns["read_manifest"](store_tl), man)
man2 = dict(man, last_preset="ProRes 422 HQ")
ns["write_manifest"](store_tl, man2)
check("a rewrite replaces the marker rather than adding one",
      (list(store_tl.markers), ns["read_manifest"](store_tl)["last_preset"]),
      ([0], "ProRes 422 HQ"))
check("an unmanaged timeline reads None", ns["read_manifest"](TL([[]])), None)

# --- append with slip correction --------------------------------------------


class Placed:
    def __init__(self, start, duration):
        self.start, self.duration = start, duration

    def GetSourceStartFrame(self):
        return self.start

    def GetDuration(self):
        return self.duration


class Pool:
    """Places each request shifted by a programmable slip."""

    def __init__(self, slips):
        self.slips, self.requests = list(slips), []

    def AppendToTimeline(self, reqs):
        req = dict(reqs[0])
        self.requests.append(req)
        ds, de = self.slips.pop(0) if self.slips else (0, 0)
        start = req["startFrame"] + ds
        return [Placed(start, req["endFrame"] + de - start)]


ap = ns["append_plate"]
p = {"name": "uhd", "mpi": uhd, "start": 100, "end": 199}
pool = Pool([(0, 0)])
item, status = ap(pool, TL([[]]), p)
check("exact append: endFrame is exclusive and video only",
      (status, pool.requests[0]["endFrame"], pool.requests[0]["mediaType"]),
      ("exact", 200, 1))
pool = Pool([(-1, 0), (0, 0)])
item, status = ap(pool, TL([[]]), p)
check("a one-frame slip is corrected by re-requesting",
      (status, pool.requests[1]["startFrame"]), ("corrected", 101))
class StuckPool(Pool):
    """Always places the same wrong range, whatever is asked for."""

    def AppendToTimeline(self, reqs):
        self.requests.append(dict(reqs[0]))
        return [Placed(101, 100)]


pool = StuckPool([])
work_tl = TL([[]])
item, status = ap(pool, work_tl, p)
check("a slip that will not go away is reported, not looped on",
      (status, len(pool.requests), len(work_tl.deleted)), ("unfixed", 3, 2))

# ---------------------------------------------------------------------------

print("")
if fails:
    print(f"RESULT: FAIL ({len(fails)} failed)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
