"""
Part K test: Batch Rename.

The script imports PySide6 and calls main() at import, so the engine is pulled
from source with ast: every module-level constant, every function and the four
data classes (OpType, RenameOp, RenamePreset, UndoRecord), exec'd into a real
module object (dataclasses look their module up in sys.modules). The Qt classes
are left out, so this runs on a plain Python with no PySide6 and no Resolve.

Covers numbering (placement, padding, increment, start, continue-after-highest,
full-timeline positions), Clean Name, Change Case, tokens, the colour / flag /
marker filters, timeline collection order and the track filter, selection
matching by uid with audio dropped, rename + undo by uid, Restore, and loading a
v1 preset.
"""

import ast
import os
import sys
import types
from datetime import datetime


def _repo_root():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        cwd = os.path.abspath(os.getcwd())
        here = cwd if os.path.basename(cwd) == "tests" else os.path.join(cwd, "tests")
    return os.path.dirname(here)


REPO = _repo_root()
SCRIPT = os.path.join(REPO, "Batch Rename.py")
DATA_CLASSES = {"OpType", "RenameOp", "RenamePreset", "UndoRecord"}


def load_engine():
    with open(SCRIPT, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    body = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            body.append(node)
        elif isinstance(node, ast.Import) or (
                isinstance(node, ast.ImportFrom) and not (node.module or "").startswith("PySide6")):
            body.append(node)
        elif isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in DATA_CLASSES:
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name != "main":
            body.append(node)
    missing = DATA_CLASSES - {n.name for n in body if isinstance(n, ast.ClassDef)}
    if missing:
        raise SystemExit(f"not found in source: {sorted(missing)}")
    mod = types.ModuleType("batch_rename_under_test")
    sys.modules[mod.__name__] = mod
    exec(compile(ast.Module(body=body, type_ignores=[]), SCRIPT, "exec"), mod.__dict__)
    return mod


br = load_engine()
OpType, RenameOp = br.OpType, br.RenameOp
NOW = datetime(2026, 11, 24, 9, 5, 7)
fails = []
passes = 0


def check(label, got, want):
    global passes
    if got == want:
        passes += 1
    else:
        fails.append(label)
        print(f"FAIL {label}\n     got:  {got!r}\n     want: {want!r}")


def num(**kw):
    return RenameOp(OpType.NUMBERING, **kw)


def names(items, ops, all_names=None, full_index_of=None):
    return br.compute_new_names(items, ops, NOW, all_names, full_index_of)


def plain(*ns):
    return [{"name": n, "uid": f"id{i}"} for i, n in enumerate(ns)]


# ---------------------------------------------------------------------------
# Stub Resolve objects
# ---------------------------------------------------------------------------

class MPI:
    def __init__(self, name, uid=None, color="", flags=(), markers=None):
        self.name, self.uid, self.color = name, uid or "mpi-" + name, color
        self.flags, self.markers = list(flags), markers or {}
        self.set_calls = []

    def GetName(self): return self.name
    def GetMediaId(self): return self.uid
    def GetUniqueId(self): return self.uid
    def SetName(self, n):
        self.set_calls.append(n)
        self.name = n
        return True
    def GetClipProperty(self, key): return {"Type": "Video"}.get(key)
    def GetClipColor(self): return self.color
    def GetFlagList(self): return self.flags
    def GetMarkers(self): return self.markers


class TItem:
    def __init__(self, uid, mpi, start, track_type="video", track=1, name=None,
                 color="", flags=(), markers=None, has_setname=True):
        self.uid, self.mpi, self.start = uid, mpi, start
        self.name = name if name is not None else (mpi.GetName() if mpi else "Title")
        self.track_type, self.track = track_type, track
        self.color, self.flags, self.markers = color, list(flags), markers or {}
        self.set_calls = []
        if not has_setname:
            self.SetName = None  # the bridge returns None for unknown methods

    def GetUniqueId(self): return self.uid
    def GetMediaPoolItem(self): return self.mpi
    def GetName(self): return self.name
    def SetName(self, n):
        self.set_calls.append(n)
        self.name = n
        return True
    def GetStart(self): return self.start
    def GetClipColor(self): return self.color
    def GetFlagList(self): return self.flags
    def GetMarkers(self): return self.markers
    def GetTrackTypeAndIndex(self): return [self.track_type, self.track]


class Timeline:
    def __init__(self, tracks, name="Edit_v3", uid="tl-1", selection=None):
        self.tracks, self.name, self.uid, self.selection = tracks, name, uid, selection

    def GetName(self): return self.name
    def GetUniqueId(self): return self.uid
    def GetTrackCount(self, kind): return len(self.tracks) if kind == "video" else 0
    def GetItemListInTrack(self, kind, idx): return self.tracks[idx - 1]
    def GetSelectedClips(self): return self.selection


class Project:
    def __init__(self, timelines, current=None):
        self.timelines, self.current = timelines, current

    def GetCurrentTimeline(self): return self.current
    def GetTimelineCount(self): return len(self.timelines)
    def GetTimelineByIndex(self, i): return self.timelines[i - 1]


class ResolveStub:
    def __init__(self, version): self.version = version
    def GetVersion(self): return self.version


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------

items = plain("a.mov", "b.mov", "c.mov")
check("numbering default: replace, pad 4, from 10 step 10",
      names(items, [num()]), ["0010", "0020", "0030"])
check("numbering template + start + increment + padding",
      names(items, [num(num_template="SH_{n}", num_start=100, num_increment=5, num_padding=3)]),
      ["SH_100", "SH_105", "SH_110"])
check("numbering before name with separator",
      names(items, [num(num_placement="before", num_separator="-", num_padding=2,
                        num_start=1, num_increment=1)]),
      ["01-a.mov", "02-b.mov", "03-c.mov"])
check("numbering after name",
      names(items, [num(num_placement="after", num_template="v{n}", num_padding=3,
                        num_start=1, num_increment=1)]),
      ["a.mov_v001", "b.mov_v002", "c.mov_v003"])
check("padding narrower than the number does not truncate",
      names(plain("x"), [num(num_start=12345, num_padding=2)]), ["12345"])
check("format_number keeps a sign in front of the padding",
      br.format_number(-5, 3), "-005")

existing = ["SH_0040", "SH_0120_old", "XSH_0500", "SH_0030", "A001C003"]
check("continue (replace) matches whole names only",
      br.numbering_base(num(num_template="SH_{n}", num_start_mode="continue"), existing), 50)
check("continue (before) matches at the start, up to the separator",
      br.numbering_base(num(num_template="SH_{n}", num_placement="before",
                            num_start_mode="continue"), existing), 130)
check("continue (after) matches at the end, after the separator",
      br.numbering_base(num(num_template="{n}", num_placement="after", num_separator="_",
                            num_start_mode="continue"), ["clip_0070", "clip_x", "clip0900"]), 80)
check("continue with no match falls back to the start number",
      br.numbering_base(num(num_template="EP_{n}", num_start=7, num_start_mode="continue"),
                        existing), 7)
check("continue treats other tokens in the template as wildcards",
      br.numbering_base(num(num_template="{track}_{n}", num_start_mode="continue"),
                        ["V1_0010", "V2_0090", "V3x"]), 100)
check("continue drives compute_new_names",
      names(plain("new1", "new2"), [num(num_template="SH_{n}", num_start_mode="continue")],
            all_names=existing),
      ["SH_0050", "SH_0060"])
check("'start at' ignores existing names",
      br.numbering_base(num(num_template="SH_{n}", num_start=10), existing), 10)

# Full-timeline positions: a subset keeps the numbers a full run gives it.
full = plain("a", "b", "c", "d")
subset = [full[1], full[3]]
full_index_of = {i["uid"]: n for n, i in enumerate(full)}
check("count over shown clips renumbers the subset",
      names(subset, [num()], full_index_of=full_index_of), ["0010", "0020"])
check("count over the whole timeline keeps full-timeline numbers",
      names(subset, [num(num_count_over="timeline")], full_index_of=full_index_of),
      ["0020", "0040"])
check("full-timeline counting without an index (Media Pool tab) counts the shown clips",
      names(subset, [num(num_count_over="timeline")]), ["0010", "0020"])

two = [num(num_template="A{n}", num_start=1, num_increment=1, num_padding=1),
       num(num_template="B{n}", num_placement="after", num_separator="", num_start=10,
           num_increment=10, num_padding=1)]
check("two numbering ops keep their own sequences",
      names(plain("x", "y"), two), ["A1B10", "A2B20"])
check("a disabled numbering op is skipped",
      names(plain("x"), [num(enabled=False)]), ["x"])
check("numbering_regex refuses a template without {n}",
      br.numbering_regex(num(num_template="SH_")), None)
check("pipeline_needs_all_names only for an enabled continue op",
      (br.pipeline_needs_all_names([num()]),
       br.pipeline_needs_all_names([num(num_start_mode="continue")]),
       br.pipeline_needs_all_names([num(num_start_mode="continue", enabled=False)])),
      (False, True, False))

# ---------------------------------------------------------------------------
# Clean Name / Change Case
# ---------------------------------------------------------------------------

for raw, want in [
    ("A001C003_220101_R1AB.mov", "A001C003_220101_R1AB"),
    ("plate_[0001-0100].exr", "plate"),
    ("plate [1001-1100]", "plate"),
    ("clip.R3D", "clip"),
    ("clip.braw", "clip"),
    ("SH_010.v002", "SH_010.v002"),
    ("Shot 1.5 final", "Shot 1.5 final"),
    (".hidden", ".hidden"),
    ("[0001-0100]", "[0001-0100]"),
]:
    check(f"clean_name {raw!r}", br.clean_name(raw), want)
check("clean_name extension only", br.clean_name("p_[01-10].exr", True, False), "p_[01-10]")
check("clean_name range only", br.clean_name("p_[01-10].exr", False, True), "p_[01-10].exr")
check("Clean Name op in a pipeline",
      names(plain("plate_[0001-0100].exr"), [RenameOp(OpType.CLEAN_NAME)]), ["plate"])

check("upper keeps extension", br.change_case("my_clip-name.mov", "upper", True), "MY_CLIP-NAME.mov")
check("upper including extension", br.change_case("my_clip.mov", "upper", False), "MY_CLIP.MOV")
check("lower", br.change_case("SH_010.MOV", "lower", True), "sh_010.MOV")
check("title keeps separators", br.change_case("my_clip-name final.mov", "title", True),
      "My_Clip-Name Final.mov")
check("title lowercases the rest of a word", br.change_case("ABC_DEF", "title", True), "Abc_Def")
check("capitalize touches only the first letter",
      br.change_case("sH_010", "capitalize", True), "SH_010")
check("change case on an empty name", br.change_case("", "title", True), "")
check("Change Case op in a pipeline",
      names(plain("a_b.mov"), [RenameOp(OpType.CHANGE_CASE, case_mode="upper")]), ["A_B.mov"])

# ---------------------------------------------------------------------------
# Tokens and the existing operations
# ---------------------------------------------------------------------------

tok_items = [{"name": "A001.mov", "uid": "u",
              "tokens": {"{source}": "A001", "{track}": "V2", "{timeline}": "Edit_v3"}}]
check("timeline tokens in prefix",
      names(tok_items, [RenameOp(OpType.PREFIX, affix_text="{track}_")]), ["V2_A001.mov"])
check("timeline tokens in a numbering template",
      names(tok_items, [num(num_template="{timeline}_{n}_{source}", num_padding=3,
                            num_start=1, num_increment=1)]),
      ["Edit_v3_001_A001"])
check("date tokens still expand",
      names(plain("x"), [RenameOp(OpType.SUFFIX, affix_text="_{date}")]), ["x_2026-11-24"])
check("timeline tokens are left literal without values (Media Pool tab)",
      names(plain("x"), [RenameOp(OpType.SUFFIX, affix_text="_{track}")]), ["x_{track}"])
check("case-insensitive replace treats backslashes in the replacement literally",
      names(plain("Shot"), [RenameOp(OpType.SEARCH_REPLACE, search_text="shot",
                                     replace_text="a\\1", case_sensitive=False)]),
      ["a\\1"])
check("remove ops unchanged",
      [br.apply_pipeline("ABCDEF", [RenameOp(OpType.REMOVE_START, remove_count=2)], NOW),
       br.apply_pipeline("ABCDEF", [RenameOp(OpType.REMOVE_END, remove_count=2)], NOW),
       br.apply_pipeline("ABCDEF", [RenameOp(OpType.REMOVE_POSITION, remove_count=2,
                                             remove_position=1)], NOW)],
      ["CDEF", "ABCD", "ADEF"])
check("apply_pipeline still works without a ctx (v1 call shape)",
      br.apply_pipeline("x", [num(num_start=3, num_padding=1)], NOW), "3")

# ---------------------------------------------------------------------------
# Attribute filters
# ---------------------------------------------------------------------------

red = br.read_attrs(TItem("r", MPI("r"), 0, color="Orange", flags=["Red"],
                          markers={10: {"color": "Blue"}, 20: {"color": "Green"}}))
bare = br.read_attrs(TItem("b", MPI("b"), 0))
check("read_attrs", red, {"clip_color": "Orange", "flags": ["Red"],
                          "marker_colors": ["Blue", "Green"], "marker_count": 2})
check("read_attrs with missing methods", br.read_attrs(object()),
      {"clip_color": "", "flags": [], "marker_colors": [], "marker_count": 0})


def m(info, **f):
    return br.item_matches_attr_filters(info, dict(br.DEFAULT_ATTR_FILTERS, **f))


check("filters: Any passes everything", (m(red), m(bare)), (True, True))
check("filters: clip color", (m(red, clip_color="Orange"), m(bare, clip_color="Orange"),
                              m(red, clip_color="None"), m(bare, clip_color="None")),
      (True, False, False, True))
check("filters: flag", (m(red, flag="Red"), m(red, flag="Blue"), m(bare, flag="None"),
                        m(red, flag="None")), (True, False, True, False))
check("filters: markers", (m(red, marker="Has markers"), m(bare, marker="Has markers"),
                           m(bare, marker="No markers"), m(red, marker="Green"),
                           m(red, marker="Red")), (True, False, True, True, False))

# ---------------------------------------------------------------------------
# Timeline collection, scope, filters
# ---------------------------------------------------------------------------

src_a = MPI("A001C003.mov")
src_b = MPI("B002_[0001-0100].exr")
v1 = [TItem("u1", src_a, 90000, track=1), TItem("u2", src_b, 90100, track=1)]
v2 = [TItem("u3", src_a, 90000, track=2, color="Blue"), TItem("u4", None, 90050, track=2)]
timeline = Timeline([v1, v2])
tl_items = br.get_timeline_items(timeline)
check("timeline order: start, then track",
      [i["uid"] for i in tl_items], ["uid:u1", "uid:u3", "uid:u4", "uid:u2"])
check("timeline item tokens",
      tl_items[3]["tokens"], {"{source}": "B002", "{track}": "V1", "{timeline}": "Edit_v3"})
check("item without a media pool clip has no source", tl_items[2]["source_name"], "")
check("track filter",
      [i["uid"] for i in br.filter_timeline_items(tl_items, {2}, br.DEFAULT_ATTR_FILTERS)],
      ["uid:u3", "uid:u4"])
check("track filter None means all tracks",
      len(br.filter_timeline_items(tl_items, None, br.DEFAULT_ATTR_FILTERS)), 4)
check("attr filter on timeline items",
      [i["uid"] for i in br.filter_timeline_items(tl_items, None,
                                                  dict(br.DEFAULT_ATTR_FILTERS, clip_color="Blue"))],
      ["uid:u3"])
check("track_range_label", (br.track_range_label(tl_items),
                            br.track_range_label(tl_items[:1]),
                            br.track_range_label([{"track": 1}, {"track": 3}])),
      ("V1–V2", "V1", "V1, V3"))
check("get_timeline_items(None)", br.get_timeline_items(None), [])

# Selection: fresh wrappers per call, so matching must be by uid; audio dropped.
sel = [TItem("u2", src_b, 90100), TItem("u3", src_a, 90000, track=2),
       TItem("a1", src_a, 90000, track_type="audio")]
picked, ignored = br.filter_to_selection(tl_items, sel)
check("selection matches by uid, in timeline order",
      [i["uid"] for i in picked], ["uid:u3", "uid:u2"])
check("audio items in a selection are ignored and counted", ignored, 1)
check("selection API present vs missing",
      (br.get_timeline_selection(Timeline([], selection=[])),
       br.get_timeline_selection(type("Old", (), {"GetSelectedClips": None})())),
      ([], None))

# Numbering a selection with full-timeline positions.
fio = {i["uid"]: n for n, i in enumerate(tl_items)}
check("selected clips keep full-timeline numbers",
      names(picked, [num(num_template="SH_{n}", num_count_over="timeline")],
            full_index_of=fio),
      ["SH_0020", "SH_0040"])

check("rename support detected from a timeline item",
      (br.timeline_rename_supported(ResolveStub([19, 0]), timeline),
       br.timeline_rename_supported(ResolveStub([21, 0]),
                                    Timeline([[TItem("x", src_a, 0, has_setname=False)]]))),
      (True, False))
check("rename support from the version on an empty timeline",
      (br.timeline_rename_supported(ResolveStub([20, 2, 0]), Timeline([])),
       br.timeline_rename_supported(ResolveStub([20, 1, 4]), Timeline([])),
       br.timeline_rename_supported(ResolveStub(None), None)),
      (True, False, False))

# ---------------------------------------------------------------------------
# Rename, undo, restore (timeline)
# ---------------------------------------------------------------------------

ops = [num(num_template="SH_{n}")]
shown = br.filter_timeline_items(tl_items, None, br.DEFAULT_ATTR_FILTERS)
pairs = list(zip(shown, names(shown, ops)))
record, failed = br.execute_rename(pairs, kind="timeline", timeline_uid="tl-1",
                                   timeline_name="Edit_v3")
check("timeline rename writes the instances",
      [t.name for t in v1 + v2], ["SH_0010", "SH_0040", "SH_0020", "SH_0030"])
check("timeline rename leaves the media pool alone",
      (src_a.name, src_a.set_calls, src_b.set_calls), ("A001C003.mov", [], []))
check("undo record", (record.kind, record.timeline_uid, len(record.mappings), failed),
      ("timeline", "tl-1", 4, 0))

other_tl = Timeline([[TItem("zz", src_a, 0)]], name="Other", uid="tl-2")
project = Project([other_tl, timeline], current=other_tl)
check("find_timeline_by_uid looks past the current timeline",
      br.find_timeline_by_uid(project, "tl-1") is timeline, True)
check("undo reverts by uid", br.execute_undo(record, project), 4)
check("names after undo", [t.name for t in v1 + v2],
      ["A001C003.mov", "B002_[0001-0100].exr", "A001C003.mov", "Title"])
check("undo never touched the other timeline", other_tl.tracks[0][0].set_calls, [])
check("undo of a deleted timeline reverts nothing",
      br.execute_undo(br.UndoRecord("x", [("uid:u1", "a", "b")], "timeline", "gone"), project), 0)

# undo_lookup: duplicates by name are told apart by uid
dup = [{"uid": "uid:1", "name": "SH"}, {"uid": "uid:2", "name": "SH"}]
check("undo_lookup by uid",
      [(i["uid"], old) for i, old in br.undo_lookup(dup, [("uid:2", "b", "SH"), ("uid:1", "a", "SH")])],
      [("uid:2", "b"), ("uid:1", "a")])
check("undo_lookup falls back to the new name once per item",
      [(i["uid"], old) for i, old in br.undo_lookup(dup, [("x", "a", "SH"), ("y", "b", "SH")])],
      [("uid:1", "a"), ("uid:2", "b")])

v2[0].name = "SH_0099"
plan = br.restore_plan(br.get_timeline_items(timeline))
check("restore plan: only items whose name differs, with a source",
      [(i["uid"], n) for i, n in plan], [("uid:u3", "A001C003.mov")])
rec, _ = br.execute_rename(plan, kind="timeline", timeline_uid="tl-1", verb="Restored")
check("restore renames and is undoable",
      (v2[0].name, rec.description, rec.mappings),
      ("A001C003.mov", "Restored 1 timeline clip", [("uid:u3", "SH_0099", "A001C003.mov")]))

failing = TItem("f", src_a, 0)
failing.SetName = lambda n: False
rec, failed = br.execute_rename([({"item": failing, "name": "a", "uid": "f"}, "b")])
check("a failed SetName is counted and not recorded", (rec, failed), (None, 1))

blank = TItem("e", src_a, 0, name="abc")
rec, failed = br.execute_rename([({"item": blank, "name": "abc", "uid": "e"}, "  ")])
check("a rename to a blank name is skipped, not written",
      (rec, failed, blank.set_calls), (None, 1, []))

# ---------------------------------------------------------------------------
# Media pool ordering
# ---------------------------------------------------------------------------

mp_items = [{"name": "b", "bin_index": 0, "start_tc": "02:00:00:00"},
            {"name": "A", "bin_index": 1, "start_tc": "01:00:00:00"},
            {"name": "c", "bin_index": 2, "start_tc": ""}]
check("sort: bin order", [i["name"] for i in br.sort_media_pool_items(mp_items, "Bin order")],
      ["b", "A", "c"])
check("sort: name, case-insensitive",
      [i["name"] for i in br.sort_media_pool_items(mp_items, "Name")], ["A", "b", "c"])
check("sort: start TC, blanks last",
      [i["name"] for i in br.sort_media_pool_items(mp_items, "Start TC")], ["A", "b", "c"])
mp_clip = br._clip_to_item(MPI("x.mov", color="Teal", flags=["Red"]), 3)
check("media pool item dict", (mp_clip["uid"], mp_clip["type_label"], mp_clip["clip_color"],
                               mp_clip["flags"], mp_clip["bin_index"]),
      ("mpi-x.mov", "Video", "Teal", ["Red"], 3))
check("media pool filter: type + attrs",
      len(br.filter_media_pool_items([mp_clip], {"Video"}, dict(br.DEFAULT_ATTR_FILTERS, flag="Red"))
          + br.filter_media_pool_items([mp_clip], {"Audio"}, br.DEFAULT_ATTR_FILTERS)), 1)

# ---------------------------------------------------------------------------
# Presets and timecode
# ---------------------------------------------------------------------------

v1_op = {"op_type": "Add Prefix", "search_text": "", "replace_text": "", "case_sensitive": True,
         "use_regex": False, "affix_text": "X_", "remove_count": 0, "remove_position": 0,
         "enabled": True}
op = br.deserialize_op(v1_op)
check("v1 op loads with new-field defaults",
      (op.op_type, op.affix_text, op.num_template, op.case_mode), (OpType.PREFIX, "X_", "{n}", "upper"))
round_trip = br.deserialize_op(br.serialize_op(num(num_template="S{n}", num_start_mode="continue")))
check("numbering op round-trips", (round_trip.num_template, round_trip.num_start_mode),
      ("S{n}", "continue"))
p = br.preset_from_dict({"name": "old", "operations": [v1_op], "filters": {"FilterVideo": False}})
check("v1 preset loads on the media pool tab", (p.tab, p.extra, p.include_subfolders),
      ("media_pool", {}, False))
check("preset round-trips", br.preset_from_dict(br.preset_to_dict(
    br.RenamePreset("n", [], {}, True, "timeline", {"tl_scope": "selected"}))).extra,
      {"tl_scope": "selected"})

check("timecode 25 fps", br.frames_to_tc(90000, 25), "01:00:00:00")
check("timecode 23.976 counts at 24", br.frames_to_tc(86400 + 23, 23.976), "01:00:00:23")
check("drop-frame minute boundary", br.frames_to_tc(1800, 29.97, True), "00:01:00;02")
check("drop-frame ten-minute boundary", br.frames_to_tc(17982, 29.97, True), "00:10:00;00")
check("drop-frame one hour", br.frames_to_tc(107892, 29.97, True), "01:00:00;00")

print(f"\n{passes} passed, {len(fails)} failed")
sys.exit(1 if fails else 0)
