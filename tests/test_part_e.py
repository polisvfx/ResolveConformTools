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
]


def extract(path, names):
    """Pull the named top-level functions, plus every literal module constant.

    Two wrinkles beyond the Part A/B harness:

    - Constants come along because functions like merge_changelog() default an
      argument to a module-level constant, and defaults are evaluated at def
      time. Only assignments whose value is a literal are taken, which skips
      SORT_METHODS (its values are function references that are not extracted).
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
        try:
            ast.literal_eval(n.value)
        except (ValueError, TypeError, SyntaxError):
            continue  # not a literal (e.g. SORT_METHODS) — leave it behind
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

print("")
if fails:
    print(f"RESULT: FAIL ({len(fails)} failed)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
