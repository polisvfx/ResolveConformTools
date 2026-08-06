"""
Part E test: Generate All Clips Timeline PRO update-mode logic.

The script calls main() at import (and would open a Fusion UIManager dialog), so
the real functions are extracted from source via ast and exec'd into one
namespace — the test exercises the shipped source rather than a copy.

Every function covered here is deliberately dataclass-free: it takes and returns
plain tuples, dicts, lists and scalars. The ast harness pulls bare FunctionDef
nodes, so anything referencing a module-level dataclass could not be extracted
without keeping a second copy of that dataclass in sync here.

No Resolve needed.
"""

import __future__
import ast
import json
import os
import sys


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
WANTED = [
    "clip_identity_key",
    "build_manifest", "manifest_add_run", "encode_manifest", "decode_manifest",
    "trim_manifest_for_marker", "first_free_frame", "find_manifest_marker_frame",
    "read_accepted_range", "diff_ranges", "classify_change",
    "plan_rebuild_placement", "compute_free_space", "fits_in_place",
]


_SELF_CONTAINED_NODES = (
    ast.Constant, ast.BinOp, ast.UnaryOp, ast.Tuple, ast.List, ast.Set, ast.Dict,
    ast.operator, ast.unaryop, ast.expr_context,
)


def _is_self_contained(node):
    """True when an expression can be evaluated with no names in scope."""
    return all(isinstance(sub, _SELF_CONTAINED_NODES) for sub in ast.walk(node))


def extract(path, names):
    """Pull the named top-level functions, plus every self-contained constant.

    Two wrinkles beyond the Part A/B harness:

    - Constants come along because functions like merge_changelog() default an
      argument to a module-level constant, and defaults are evaluated at def
      time. Only assignments whose value is self-contained (a literal, or
      arithmetic over literals such as `1 << 30`) are taken, which skips
      SORT_METHODS: its values are function references that are not extracted,
      so evaluating it would raise NameError.
    - The module is compiled with the `annotations` future flag, matching the
      real file's `from __future__ import annotations`. Without it, an
      annotation like `-> Optional[str]` is evaluated at def time and raises
      NameError because typing was never imported into this namespace.
    """
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)

    consts = []
    for n in tree.body:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        target = n.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        if not _is_self_contained(n.value):
            continue  # e.g. SORT_METHODS — references names we do not extract
        consts.append(n)

    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    found = {n.name for n in nodes}
    missing = set(names) - found
    if missing:
        raise SystemExit(f"not found in source: {sorted(missing)}")

    ns = {"print": print, "json": json}
    module = ast.Module(body=consts + nodes, type_ignores=[])
    exec(compile(module, path, "exec",
                 flags=__future__.annotations.compiler_flag), ns)
    print(f"  extracted {len(consts)} module constant(s)")
    for n in nodes:
        print(f"  extracted {n.name}()  lines {n.lineno}-{n.end_lineno}")
    return ns


ns = extract(SCRIPT, WANTED)
clip_identity_key = ns["clip_identity_key"]
build_manifest = ns["build_manifest"]
manifest_add_run = ns["manifest_add_run"]
encode_manifest = ns["encode_manifest"]
decode_manifest = ns["decode_manifest"]
trim_manifest_for_marker = ns["trim_manifest_for_marker"]
first_free_frame = ns["first_free_frame"]
find_manifest_marker_frame = ns["find_manifest_marker_frame"]

read_accepted_range = ns["read_accepted_range"]
diff_ranges = ns["diff_ranges"]
classify_change = ns["classify_change"]
plan_rebuild_placement = ns["plan_rebuild_placement"]
compute_free_space = ns["compute_free_space"]
fits_in_place = ns["fits_in_place"]

UPDATE_MARKER_PREFIX = ns["UPDATE_MARKER_PREFIX"]
UPDATE_CHANGE_TOLERANCE = ns["UPDATE_CHANGE_TOLERANCE"]
UPDATE_SHRINK_TOLERANCE = ns["UPDATE_SHRINK_TOLERANCE"]
FREE_SPACE_UNBOUNDED = ns["FREE_SPACE_UNBOUNDED"]
REBUILD_FIT_SLACK = ns["REBUILD_FIT_SLACK"]
SLIP_TOLERANCE = ns["SLIP_TOLERANCE"]

MANIFEST_KEY = ns["MANIFEST_KEY"]
MANIFEST_MARKER_PREFIX = ns["MANIFEST_MARKER_PREFIX"]
MANIFEST_SCHEMA = ns["MANIFEST_SCHEMA"]
MANIFEST_MARKER_MAX_CHARS = ns["MANIFEST_MARKER_MAX_CHARS"]

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       got  {got!r}")
        print(f"       want {want!r}")
        fails.append(label)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class MPI:
    """Stub MediaPoolItem.

    raises_prop mimics a bridge call that blows up rather than returning None,
    which is the failure mode the real getters are wrapped against.
    """

    def __init__(self, media_id=None, file_path=None, raises_prop=False,
                 raises_id=False):
        self._id = media_id
        self._fp = file_path
        self._raises_prop = raises_prop
        self._raises_id = raises_id

    def GetMediaId(self):
        if self._raises_id:
            raise RuntimeError("boom")
        return self._id

    def GetClipProperty(self, key):
        if self._raises_prop:
            raise RuntimeError("boom")
        if key == "File Path":
            return self._fp
        return None


# ---------------------------------------------------------------------------
# clip_identity_key
# ---------------------------------------------------------------------------

print("\n== clip_identity_key ==")

check("file path wins when merging by source file",
      clip_identity_key(MPI("mid1", "/vol/a.mov"), True), "FP:/vol/a.mov")

check("media id used when merging by source file is off",
      clip_identity_key(MPI("mid1", "/vol/a.mov"), False), "mid1")

check("empty file path falls back to media id",
      clip_identity_key(MPI("mid1", ""), True), "mid1")

check("missing file path falls back to media id",
      clip_identity_key(MPI("mid1", None), True), "mid1")

check("no media id and no path is None",
      clip_identity_key(MPI(None, None), True), None)

check("no media id and no path is None (merge off)",
      clip_identity_key(MPI(None, "/vol/a.mov"), False), None)

check("path-only item still keys by path",
      clip_identity_key(MPI(None, "/vol/a.mov"), True), "FP:/vol/a.mov")

check("GetClipProperty raising falls back to media id",
      clip_identity_key(MPI("mid1", "/vol/a.mov", raises_prop=True), True), "mid1")

check("GetMediaId raising still yields the path key",
      clip_identity_key(MPI("mid1", "/vol/a.mov", raises_id=True), True),
      "FP:/vol/a.mov")

check("GetMediaId raising with no path is None",
      clip_identity_key(MPI("mid1", None, raises_id=True), True), None)

check("empty media id normalises to None",
      clip_identity_key(MPI("", None), True), None)

# Two Media Pool entries for one file share an identity only when merging is on.
# This is the whole point of the helper: the update scan must recognise a clip
# already on the timeline as the same shot the collector just produced.
dual_a = MPI("mid_a", "/vol/shot_010.exr")
dual_b = MPI("mid_b", "/vol/shot_010.exr")
check("two media pool entries, one file, merging on -> same identity",
      clip_identity_key(dual_a, True) == clip_identity_key(dual_b, True), True)
check("two media pool entries, one file, merging off -> different identity",
      clip_identity_key(dual_a, False) == clip_identity_key(dual_b, False), False)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

print("\n== manifest ==")

SOURCES = [{"uid": "tl:aaa", "name": "REEL_01"}, {"uid": "tl:bbb", "name": "REEL_02"}]
SETTINGS = {"connection_threshold": 25, "merge_by_source_file": True,
            "video_only": True, "allow_disabled_clips": False,
            "use_xml_retime": True, "import_clip_names": False}

base = build_manifest("tl:dest", "All_Sources", SOURCES, SETTINGS,
                      "2026-08-06T10:00:00Z", "2.0")

check("new manifest carries the current schema", base["schema"], MANIFEST_SCHEMA)
check("new manifest starts at run 0", base["run_counter"], 0)
check("new manifest has no runs", base["runs"], [])
check("new manifest is not adopted", base["adopted"], False)
check("sources are kept", base["sources"], SOURCES)
check("settings are kept", base["settings"], SETTINGS)

check("round trip through encode/decode is identity",
      decode_manifest(encode_manifest(base)), base)
check("round trip survives the marker prefix",
      decode_manifest(MANIFEST_MARKER_PREFIX + encode_manifest(base)), base)
check("encoding is stable across calls",
      encode_manifest(base) == encode_manifest(dict(base)), True)

# GetThirdPartyMetadata is documented as string|dict, so a dict has to work too.
check("decode accepts the {key: value} dict form",
      decode_manifest({MANIFEST_KEY: encode_manifest(base)}), base)

check("decode of None is None", decode_manifest(None), None)
check("decode of empty string is None", decode_manifest(""), None)
check("decode of whitespace is None", decode_manifest("   "), None)
check("decode of junk is None", decode_manifest("not json at all"), None)
check("decode of a bare object with no schema is None", decode_manifest("{}"), None)
check("decode of a JSON list is None", decode_manifest("[1,2,3]"), None)
check("decode of a JSON string is None", decode_manifest('"hello"'), None)
check("decode of a bare prefix is None", decode_manifest(MANIFEST_MARKER_PREFIX), None)
check("decode of an unsupported schema is None",
      decode_manifest('{"schema":99,"sources":[]}'), None)
check("decode of a non-string non-dict is None", decode_manifest(12345), None)

run1 = {"n": 1, "utc": "2026-08-06T11:00:00Z", "added": 3, "extended": 1}
after1 = manifest_add_run(base, run1)
check("adding a run bumps the counter", after1["run_counter"], 1)
check("adding a run stamps last_run_utc", after1["last_run_utc"], run1["utc"])
check("adding a run appends it", after1["runs"], [run1])
check("adding a run does not mutate the original", base["runs"], [])

many = base
for n in range(1, 41):
    many = manifest_add_run(many, {"n": n, "utc": f"2026-08-06T{n % 24:02d}:00:00Z"})
check("run history caps at MANIFEST_MAX_RUNS", len(many["runs"]),
      ns["MANIFEST_MAX_RUNS"])
check("the cap drops the oldest runs first", many["runs"][0]["n"],
      41 - ns["MANIFEST_MAX_RUNS"])
check("the cap keeps the newest run", many["runs"][-1]["n"], 40)
check("run_counter tracks the newest run", many["run_counter"], 40)

fits = trim_manifest_for_marker(many, MANIFEST_MARKER_MAX_CHARS)
check("a full history already fits the marker budget",
      fits["runs"] == many["runs"], True)
check("trimming preserves sources", fits["sources"], SOURCES)
check("trimming preserves settings", fits["settings"], SETTINGS)

tight = trim_manifest_for_marker(many, 400)
check("a tight budget drops runs", len(tight["runs"]) < len(many["runs"]), True)
check("a tight budget still preserves sources", tight["sources"], SOURCES)
check("a tight budget still preserves settings", tight["settings"], SETTINGS)

# Sources and settings alone can exceed a very small budget. Returning the
# manifest anyway (rather than dropping them) is the deliberate choice: a
# too-long marker is recoverable, a manifest missing its sources is not.
impossible = trim_manifest_for_marker(many, 10)
check("an impossible budget empties runs but keeps the rest",
      (impossible["runs"], impossible["sources"]), ([], SOURCES))


# ---------------------------------------------------------------------------
# first_free_frame
# ---------------------------------------------------------------------------

print("\n== first_free_frame ==")

check("free preferred frame is used", first_free_frame([], 0, lower=0), 0)
check("taken preferred frame steps forward", first_free_frame([0], 0, lower=0), 1)
check("steps forward past a run of taken frames",
      first_free_frame([0, 1, 2], 0, lower=0), 3)
check("unrelated taken frames are ignored",
      first_free_frame([0, 1, 2], 5, lower=0), 5)
check("forward exhausted falls back to searching backward",
      first_free_frame([10, 11], 10, lower=0, upper=11), 9)
check("every frame taken returns -1",
      first_free_frame([0, 1, 2], 1, lower=0, upper=2), -1)
check("float frames are tolerated", first_free_frame([0.0, 1.0], 0, lower=0), 2)
check("no lower bound means no backward search",
      first_free_frame([5, 6], 5, upper=6), -1)


# ---------------------------------------------------------------------------
# find_manifest_marker_frame
# ---------------------------------------------------------------------------

print("\n== find_manifest_marker_frame ==")

check("no markers means no manifest", find_manifest_marker_frame({}), None)
check("markers without our prefix are ignored",
      find_manifest_marker_frame({0: {"customData": "something else"},
                                  9: {"customData": ""}}), None)
check("the manifest marker is found by prefix",
      find_manifest_marker_frame({0: {"customData": "other"},
                                  5: {"customData": MANIFEST_MARKER_PREFIX + "{}"}}), 5)
check("the lowest matching frame wins",
      find_manifest_marker_frame({7: {"customData": MANIFEST_MARKER_PREFIX + "{}"},
                                  3: {"customData": MANIFEST_MARKER_PREFIX + "{}"}}), 3)
check("a missing customData key is tolerated",
      find_manifest_marker_frame({0: {"name": "plain marker"}}), None)
check("a None marker info is tolerated",
      find_manifest_marker_frame({0: None}), None)
check("a non-string customData is tolerated",
      find_manifest_marker_frame({0: {"customData": 42}}), None)


# ---------------------------------------------------------------------------
# classify_change
# ---------------------------------------------------------------------------

print("\n== classify_change ==")

check("grow tolerance is at least the observed placement slip",
      UPDATE_CHANGE_TOLERANCE >= SLIP_TOLERANCE, True)
# Missing frames break a pull; surplus frames are just handle. The thresholds are
# deliberately asymmetric, and shrink must be the forgiving one.
check("shrink tolerance is the more forgiving of the two",
      UPDATE_SHRINK_TOLERANCE > UPDATE_CHANGE_TOLERANCE, True)

for delta in (0, 1, 2, 3):
    check(f"head grown by {delta} is within tolerance",
          classify_change((100 - delta, 200), (100, 200)), "unchanged")
    check(f"tail grown by {delta} is within tolerance",
          classify_change((100, 200 + delta), (100, 200)), "unchanged")

for delta in (0, 4, 8, 12):
    check(f"head pulled in by {delta} is within shrink tolerance",
          classify_change((100 + delta, 200), (100, 200)), "unchanged")
    check(f"tail pulled in by {delta} is within shrink tolerance",
          classify_change((100, 200 - delta), (100, 200)), "unchanged")

check("head reaching further back is extended",
      classify_change((96, 200), (100, 200)), "extended")
check("tail reaching further on is extended",
      classify_change((100, 204), (100, 200)), "extended")
check("both ends reaching out is extended",
      classify_change((96, 204), (100, 200)), "extended")
check("head pulled well in is shortened",
      classify_change((113, 200), (100, 200)), "shortened")
check("tail pulled well in is shortened",
      classify_change((100, 187), (100, 200)), "shortened")
check("both ends pulled well in is shortened",
      classify_change((113, 187), (100, 200)), "shortened")
check("head out and tail well in is both",
      classify_change((96, 187), (100, 200)), "both")
check("head well in and tail out is both",
      classify_change((113, 204), (100, 200)), "both")

# The asymmetry in action: a 4-frame surplus is left alone, a 4-frame shortfall
# is not. This is the real case from a live dry run — a reversed clip carrying
# five frames of extra handle, which is not worth a rebuild.
check("a 4-frame surplus at the tail is left alone",
      classify_change((100, 196), (100, 200)), "unchanged")
check("a 4-frame shortfall at the tail is acted on",
      classify_change((100, 204), (100, 200)), "extended")
check("the live case: head -1, tail -4 reads unchanged",
      classify_change((1505588, 1505637), (1505587, 1505641)), "unchanged")

# The regression that would otherwise churn the whole timeline on every run: the
# append helper's widening fallback places a clip wider than asked, which is a
# shrink on both ends and now measured against the forgiving threshold.
check("a clip widened by 1 on both ends reads unchanged",
      classify_change((100, 200), (99, 201)), "unchanged")
check("a clip widened by 2 on both ends reads unchanged",
      classify_change((100, 200), (98, 202)), "unchanged")
check("a clip widened by 10 on both ends still reads unchanged",
      classify_change((100, 200), (90, 210)), "unchanged")

# The escape hatch for a target that cannot physically be reached.
check("accepted range matching desired forces unchanged",
      classify_change((100, 200), (100, 150), (100, 200)), "unchanged")
check("accepted range within tolerance of desired forces unchanged",
      classify_change((100, 200), (100, 150), (102, 199)), "unchanged")
check("a stale accepted range does not suppress a real change",
      classify_change((100, 300), (100, 200), (100, 200)), "extended")
check("a half-written accepted range is ignored",
      classify_change((100, 300), (100, 200), (100, None)), "extended")
check("no accepted range is ignored",
      classify_change((100, 300), (100, 200), None), "extended")

check("an explicit grow tolerance is honoured",
      classify_change((80, 200), (100, 200), None, 20), "unchanged")
check("an explicit shrink tolerance is honoured",
      classify_change((100, 150), (100, 200), None, 3, 60), "unchanged")
check("a tight explicit shrink tolerance bites",
      classify_change((100, 196), (100, 200), None, 3, 2), "shortened")


# ---------------------------------------------------------------------------
# diff_ranges
# ---------------------------------------------------------------------------

print("\n== diff_ranges ==")

check("identical single ranges pair up",
      diff_ranges([(0, 100)], [(0, 100)]), ([(0, 0)], [], []))
check("nothing placed means everything is new",
      diff_ranges([(0, 100), (500, 600)], []), ([], [0, 1], []))
check("nothing desired means everything is dropped",
      diff_ranges([], [(0, 100), (500, 600)]), ([], [], [0, 1]))
check("non-overlapping ranges do not pair",
      diff_ranges([(0, 100)], [(500, 600)]), ([], [0], [0]))
check("abutting-but-disjoint ranges do not pair",
      diff_ranges([(0, 100)], [(101, 200)]), ([], [0], [0]))
check("a single frame of overlap is enough to pair",
      diff_ranges([(0, 100)], [(100, 200)]), ([(0, 0)], [], []))

# Index pairing would match desired[1] to placed[1] here and report the wrong
# clip as changed. Overlap matching gets it right.
check("a range deleted from the middle does not shift the rest",
      diff_ranges([(0, 100), (900, 1000)],
                  [(0, 100), (400, 500), (900, 1000)]),
      ([(0, 0), (1, 2)], [], [1]))

check("best overlap wins when two placed clips overlap one desired",
      diff_ranges([(100, 200)], [(100, 120), (150, 210)]),
      ([(0, 1)], [], [0]))
check("best overlap wins when two desired ranges overlap one placed",
      diff_ranges([(100, 120), (150, 210)], [(100, 200)]),
      ([(1, 0)], [0], []))

grew = diff_ranges([(90, 210)], [(100, 200)])
check("a grown range still pairs with its old placement", grew, ([(0, 0)], [], []))

many_desired = [(0, 100), (200, 300), (400, 500)]
many_placed = [(210, 290), (0, 90), (450, 460)]
once = diff_ranges(many_desired, many_placed)
twice = diff_ranges(many_desired, many_placed)
check("matching is deterministic", once, twice)
check("out-of-order placements match by overlap, not position",
      once, ([(0, 1), (1, 0), (2, 2)], [], []))

# Equal overlap on both sides: the tie-break is total endpoint distance.
check("equal overlap is broken by endpoint distance",
      diff_ranges([(100, 200)], [(100, 200), (50, 250)]),
      ([(0, 0)], [], [1]))


# ---------------------------------------------------------------------------
# plan_rebuild_placement
# ---------------------------------------------------------------------------

print("\n== plan_rebuild_placement ==")

check("an unchanged range needs no room and does not move",
      plan_rebuild_placement(1000, (500, 600), (500, 600)), (1000, 101, 0, 0))
check("growing at the head moves the record frame back and needs room before",
      plan_rebuild_placement(1000, (500, 600), (480, 600)), (980, 121, 20, 0))
check("growing at the tail stays put and needs room after",
      plan_rebuild_placement(1000, (500, 600), (500, 650)), (1000, 151, 0, 50))
check("growing both ends needs room on both sides",
      plan_rebuild_placement(1000, (500, 600), (480, 650)), (980, 171, 20, 50))
check("trimming the head moves the record frame forward and needs no room",
      plan_rebuild_placement(1000, (500, 600), (520, 600)), (1020, 81, 0, 0))
check("trimming the tail stays put and needs no room",
      plan_rebuild_placement(1000, (500, 600), (500, 580)), (1000, 81, 0, 0))
check("a head grow with a bigger tail trim needs room only before",
      plan_rebuild_placement(1000, (500, 600), (490, 550)), (990, 61, 10, 0))

# The invariant the source-frame anchor exists for: a source frame kept by both
# the old and the new range stays at the same timeline position.
for kept in (520, 560, 599):
    for new_src in ((480, 600), (500, 650), (510, 590), (490, 700)):
        new_record = plan_rebuild_placement(1000, (500, 600), new_src)[0]
        old_pos = 1000 + (kept - 500)
        new_pos = new_record + (kept - new_src[0])
        if old_pos != new_pos:
            check(f"retained frame {kept} holds position for {new_src}",
                  new_pos, old_pos)
            break
    else:
        continue
    break
else:
    check("retained source frames hold their timeline position", True, True)


# ---------------------------------------------------------------------------
# compute_free_space / fits_in_place
# ---------------------------------------------------------------------------

print("\n== compute_free_space ==")

BOUNDS = [(0, 100), (150, 250), (250, 300)]
check("first clip measures back to the timeline start",
      compute_free_space(BOUNDS, 0, 0), (0, 50))
check("middle clip sees the gap on each side",
      compute_free_space(BOUNDS, 1, 0), (50, 0))
check("last clip has unbounded room after it",
      compute_free_space(BOUNDS, 2, 0), (0, FREE_SPACE_UNBOUNDED))
check("a lone clip has room before it and unbounded room after",
      compute_free_space([(100, 200)], 0, 0), (100, FREE_SPACE_UNBOUNDED))
check("a non-zero timeline start is respected",
      compute_free_space([(86400, 86500)], 0, 86400),
      (0, FREE_SPACE_UNBOUNDED))
check("a clip starting before the timeline start clamps to zero",
      compute_free_space([(86300, 86500)], 0, 86400),
      (0, FREE_SPACE_UNBOUNDED))
check("abutting neighbours leave no room at all",
      compute_free_space([(0, 100), (100, 200), (200, 300)], 1, 0), (0, 0))

print("\n== fits_in_place ==")

check("no growth always fits", fits_in_place(0, 0, 0, 0), True)
check("growth into a big enough gap fits", fits_in_place(50, 50, 20, 30), True)
check("growth into an exact gap fits with no slack",
      fits_in_place(20, 30, 20, 30), True)
check("growth into an exact gap fails once slack is required",
      fits_in_place(20, 30, 20, 30, REBUILD_FIT_SLACK), False)
check("head growth alone is blocked by the gap before",
      fits_in_place(5, 500, 20, 0), False)
check("tail growth alone is blocked by the gap after",
      fits_in_place(500, 5, 0, 20), False)
check("head growth alone ignores the gap after",
      fits_in_place(50, 0, 20, 0), True)
check("tail growth alone ignores the gap before",
      fits_in_place(0, 50, 0, 20), True)
check("unbounded room after accommodates any tail growth",
      fits_in_place(0, FREE_SPACE_UNBOUNDED, 0, 99999), True)
# Widening pushes out both ends at once, so it needs a spare frame on each side
# even when only one end grew. A clip abutting its left neighbour cannot widen.
check("abutting the previous clip rules out widening",
      fits_in_place(0, FREE_SPACE_UNBOUNDED, 0, 99999, REBUILD_FIT_SLACK), False)
check("one spare frame on each side permits widening",
      fits_in_place(1, FREE_SPACE_UNBOUNDED, 0, 99999, REBUILD_FIT_SLACK), True)


# ---------------------------------------------------------------------------
# read_accepted_range
# ---------------------------------------------------------------------------

print("\n== read_accepted_range ==")


def upd(run, ds, de):
    return UPDATE_MARKER_PREFIX + json.dumps(
        {"v": 1, "run": run, "kind": "extended", "ds": ds, "de": de})


check("no markers means no accepted range", read_accepted_range({}), (None, None))
check("an update marker yields its accepted range",
      read_accepted_range({40: {"customData": upd(3, 1188, 1540)}}), (1188, 1540))
check("unrelated markers are ignored",
      read_accepted_range({10: {"customData": "RCT_RUN:{}"},
                           20: {"customData": ""}}), (None, None))
check("the highest run wins when two update markers survive",
      read_accepted_range({10: {"customData": upd(2, 100, 200)},
                           50: {"customData": upd(5, 300, 400)}}), (300, 400))
check("marker order in the dict does not matter",
      read_accepted_range({50: {"customData": upd(5, 300, 400)},
                           10: {"customData": upd(2, 100, 200)}}), (300, 400))
check("malformed json in an update marker is ignored",
      read_accepted_range({10: {"customData": UPDATE_MARKER_PREFIX + "{oops"}}),
      (None, None))
check("a non-integer accepted range is ignored",
      read_accepted_range({10: {"customData": UPDATE_MARKER_PREFIX
                                + '{"ds":"a","de":"b"}'}}), (None, None))
check("a missing accepted range is ignored",
      read_accepted_range({10: {"customData": UPDATE_MARKER_PREFIX
                                + '{"v":1,"run":2}'}}), (None, None))
check("a None marker info is tolerated",
      read_accepted_range({10: None}), (None, None))


# ---------------------------------------------------------------------------

print("")
if fails:
    print(f"RESULT: FAIL ({len(fails)} failed)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
