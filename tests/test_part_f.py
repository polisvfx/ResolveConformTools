"""
Part F test: Generate All Clips Timeline PRO update mode, end to end.

Part E covers the pure decision functions. This drives run_update_workflow()
itself against a stubbed Resolve, which is where the wiring lives: reading the
manifest, resolving sources, deleting and re-appending, restoring item state,
writing markers, creating the run's track and recording the run.

The script calls main() at import, so the trailing call is stripped and the rest
is exec'd into a module registered in sys.modules (@dataclass needs that) with
`resolve` injected as a global — the same shape Resolve's own environment has.

The AppendToTimeline stub refuses to place a clip over an existing one rather
than silently overwriting, which is the conservative reading of undocumented
behaviour. If a real Resolve turns out to overwrite instead, this suite still
holds: the code never plans an overlapping placement.

No Resolve needed.
"""

import io
import os
import sys
import types
import contextlib


def _repo_root():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        cwd = os.path.abspath(os.getcwd())
        here = cwd if os.path.basename(cwd) == "tests" else os.path.join(cwd, "tests")
    return os.path.dirname(here)


REPO = _repo_root()
SCRIPT = os.path.join(REPO, "Generate All Clips Timeline PRO.py")

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       got  {got!r}")
        print(f"       want {want!r}")
        fails.append(label)


def check_true(label, got):
    check(label, bool(got), True)


# ---------------------------------------------------------------------------
# Stubbed Resolve
# ---------------------------------------------------------------------------


class MPI:
    """Stub MediaPoolItem. Media runs 0..end inclusive."""

    def __init__(self, name, media_id, path, end=5000, clip_type="Video"):
        self._name, self._id, self._path = name, media_id, path
        self._end, self._type = end, clip_type
        self.timeline = None
        self.third_party = {}

    def GetName(self):
        return self._name

    def GetMediaId(self):
        return self._id

    def GetClipProperty(self, key):
        return {
            "File Path": self._path, "Type": self._type,
            "Frames": str(self._end + 1), "End": str(self._end),
            "FPS": "25.0", "Reel Name": "",
        }.get(key)

    def GetTimeline(self):
        return self.timeline

    def GetThirdPartyMetadata(self, key=None):
        if key is None:
            return dict(self.third_party)
        return self.third_party.get(key)

    def SetThirdPartyMetadata(self, key, value):
        self.third_party[key] = value
        return True


class Item:
    """Stub TimelineItem."""

    _seq = 0

    def __init__(self, mpi, source_start, source_end, record_start, track=1,
                 name=None, fusion_comps=0, versions=None, linked=0,
                 color=None, flags=None, enabled=True):
        Item._seq += 1
        self.uid = f"item{Item._seq}"
        self.mpi = mpi
        self.source_start, self.source_end = source_start, source_end
        self.record_start = record_start
        self.track = track
        self.name = name or mpi.GetName()
        self.markers = {}
        self.fusion_comps = fusion_comps
        self.versions = list(versions or [])
        self.linked = linked
        self.color = color
        self.flags = list(flags or [])
        self.enabled = enabled

    # --- geometry ---
    def GetSourceStartFrame(self):
        return self.source_start

    def GetSourceEndFrame(self):
        return self.source_end

    def GetStart(self):
        return self.record_start

    def GetDuration(self):
        return self.source_end - self.source_start + 1

    def GetEnd(self):
        return self.record_start + self.GetDuration()

    # --- identity / state ---
    def GetMediaPoolItem(self):
        return self.mpi

    def GetName(self):
        return self.name

    def SetName(self, value):
        self.name = value
        return True

    def GetClipColor(self):
        return self.color

    def SetClipColor(self, value):
        self.color = value
        return True

    def GetFlagList(self):
        return list(self.flags)

    def AddFlag(self, color):
        self.flags.append(color)
        return True

    def GetClipEnabled(self):
        return self.enabled

    def SetClipEnabled(self, value):
        self.enabled = value
        return True

    def GetFusionCompCount(self):
        return self.fusion_comps

    def GetVersionNameList(self, version_type):
        return list(self.versions)

    def AddVersion(self, name, version_type):
        self.versions.append(name)
        return True

    def SetCurrentVersion(self, name, version_type):
        return True

    def GetLinkedItems(self):
        return [self] * (self.linked + 1) if self.linked else [self]

    # --- markers ---
    def GetMarkers(self):
        return {float(f): dict(v) for f, v in self.markers.items()}

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        frame = int(frame)
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note,
                               "duration": duration, "customData": custom}
        return True

    def __repr__(self):
        return (f"Item({self.name} src {self.source_start}-{self.source_end} "
                f"@V{self.track}:{self.record_start})")


class Timeline:
    def __init__(self, name, uid, tracks=None, start_frame=86400, mpi=None):
        self.name, self.uid = name, uid
        self.tracks = tracks or {1: []}
        self.start_frame = start_frame
        self.markers = {}
        self.track_names = {}
        self.mpi = mpi

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetMediaPoolItem(self):
        return self.mpi

    def GetStartFrame(self):
        return self.start_frame

    def GetEndFrame(self):
        ends = [i.GetEnd() for items in self.tracks.values() for i in items]
        return max(ends) if ends else self.start_frame

    def GetTrackCount(self, track_type):
        return max(self.tracks) if track_type == "video" and self.tracks else 0

    def GetItemListInTrack(self, track_type, index):
        if track_type != "video":
            return []
        return sorted(self.tracks.get(index, []), key=lambda i: i.record_start)

    def AddTrack(self, track_type, *args):
        if track_type != "video":
            return False
        self.tracks[self.GetTrackCount("video") + 1] = []
        return True

    def SetTrackName(self, track_type, index, name):
        self.track_names[index] = name
        return True

    def GetTrackName(self, track_type, index):
        return self.track_names.get(index, f"Video {index}")

    def DeleteClips(self, items, ripple=False):
        for item in items:
            for track in self.tracks.values():
                if item in track:
                    track.remove(item)
        return True

    def GetSetting(self, name):
        return "25.0" if name == "timelineFrameRate" else ""

    def GetMarkers(self):
        return {float(f): dict(v) for f, v in self.markers.items()}

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        frame = int(frame)
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note,
                               "duration": duration, "customData": custom}
        return True

    def DeleteMarkerAtFrame(self, frame):
        return self.markers.pop(int(frame), None) is not None

    def Export(self, path, fmt):
        return False

    def all_items(self):
        return [i for items in self.tracks.values() for i in items]


class MediaPool:
    def __init__(self, project, selection=None):
        self.project = project
        self.selection = selection or []
        self.append_calls = []
        self.refused = 0

    def GetCurrentFolder(self):
        return None

    def GetSelectedClips(self):
        return self.selection

    def CreateEmptyTimeline(self, name):
        # Real timelines start at 01:00:00:00, which is the whole point of the
        # preserve-layout regression below.
        created = Timeline(name, f"tl:{name}", {1: []}, start_frame=86400,
                           mpi=MPI(name, f"mid_{name}", "", clip_type="Timeline"))
        created.mpi.timeline = created
        self.project.timelines.append(created)
        self.project.current = created
        return created

    def AppendToTimeline(self, clip_infos):
        timeline = self.project.current
        placed = []
        for info in clip_infos:
            self.append_calls.append(dict(info))
            track = info.get("trackIndex", 1)
            items = timeline.tracks.setdefault(track, [])
            start = info["startFrame"]
            end = info["endFrame"]
            duration = end - start + 1
            record = info.get("recordFrame")
            if record is None:
                record = (max((i.GetEnd() for i in items), default=timeline.start_frame))
            # Refuse to place over an existing clip rather than overwriting it.
            for existing in items:
                if record < existing.GetEnd() and existing.record_start < record + duration:
                    self.refused += 1
                    return []
            item = Item(info["mediaPoolItem"], start, end, record, track)
            items.append(item)
            placed.append(item)
        return placed


class Project:
    def __init__(self, timelines, current):
        self.timelines = timelines
        self.current = current
        self.media_pool = None

    def GetMediaPool(self):
        return self.media_pool

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, index):
        return self.timelines[index - 1]

    def GetSetting(self, name):
        return "25.0" if name == "timelineFrameRate" else ""


class Resolve:
    EXPORT_FCP_7_XML = 1

    def __init__(self, project):
        self.project = project

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

with open(SCRIPT, "r", encoding="utf-8") as fh:
    SRC = fh.read().rstrip()
if not SRC.endswith("main()"):
    raise SystemExit("expected the script to end with a main() call")
SRC = SRC[: -len("main()")]

_LOADS = [0]


def load(resolve_stub):
    _LOADS[0] += 1
    name = f"genallpro_{_LOADS[0]}"
    module = types.ModuleType(name)
    module.resolve = resolve_stub
    sys.modules[name] = module
    try:
        exec(compile(SRC, SCRIPT, "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


# ---------------------------------------------------------------------------
# World builder
# ---------------------------------------------------------------------------

DEFAULTS = dict(
    source_selection_mode="Union",
    selection_method="Current Selection",
    connection_threshold=25,
    allow_disabled_clips=False,
    video_only=True,
    mark_duplicates=False,
    mark_retimed_clips=False,
    use_xml_retime=False,
    import_clip_names=False,
    merge_by_source_file=True,
    protect_graded_clips=True,
    dry_run=False,
)


def build_world(source_ranges, dest_items, dest_start=86400):
    """source_ranges: {clip_name: [(src_start, src_end), ...]} on one source
    timeline. dest_items: list of Item built by make_dest_item().
    """
    Item._seq = 0
    media = {}
    for name in set(list(source_ranges) + [i["name"] for i in dest_items]):
        media[name] = MPI(name, f"mid_{name}", f"/vol/{name}.mov")

    src_items = {1: []}
    cursor = 0
    for name, ranges in source_ranges.items():
        for start, end in ranges:
            src_items[1].append(Item(media[name], start, end, cursor))
            cursor += end - start + 1

    source_tl = Timeline("SRC_01", "tl:src01", src_items, start_frame=0)
    source_mpi = MPI("SRC_01", "mid_srctl", "", clip_type="Timeline")
    source_mpi.timeline = source_tl

    dest_tracks = {}
    for spec in dest_items:
        item = Item(media[spec["name"]], spec["src"][0], spec["src"][1],
                    spec["record"], spec.get("track", 1),
                    name=spec.get("label"),
                    fusion_comps=spec.get("fusion_comps", 0),
                    versions=spec.get("versions"),
                    linked=spec.get("linked", 0),
                    color=spec.get("color"), flags=spec.get("flags"))
        dest_tracks.setdefault(spec.get("track", 1), []).append(item)
    dest_tracks.setdefault(1, [])

    dest_mpi = MPI("All_Sources", "mid_desttl", "", clip_type="Timeline")
    dest_tl = Timeline("All_Sources", "tl:dest", dest_tracks,
                       start_frame=dest_start, mpi=dest_mpi)
    dest_mpi.timeline = dest_tl

    project = Project([source_tl, dest_tl], dest_tl)
    project.media_pool = MediaPool(project, [source_mpi])
    return Resolve(project), project, dest_tl, source_tl


def make_dest(name, src, record, **kw):
    spec = {"name": name, "src": src, "record": record}
    spec.update(kw)
    return spec


def run_update(module, **overrides):
    params = dict(DEFAULTS)
    params.update(overrides)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module.run_update_workflow(**params)
    return buf.getvalue()


def items_on(timeline, track):
    return timeline.GetItemListInTrack("video", track)


def update_markers(item):
    return [m for m in item.markers.values()
            if str(m.get("customData", "")).startswith("RCT_UPD:")]


# ---------------------------------------------------------------------------
# 1. Refusing to guess
# ---------------------------------------------------------------------------

print("\n== no manifest ==")

res, project, dest, src = build_world(
    {"shot_a": [(100, 200)]},
    [make_dest("shot_a", (100, 200), 86400)])
mod = load(res)
log = run_update(mod, source_selection_mode="Recorded")
check_true("Recorded sources with no manifest stops", "no All Clips manifest" in log)
check("nothing was appended", len(project.media_pool.append_calls), 0)
check("the timeline is untouched", len(dest.all_items()), 1)

# ---------------------------------------------------------------------------
# 2. Adoption, and a timeline that already matches
# ---------------------------------------------------------------------------

print("\n== adoption and the no-op run ==")

res, project, dest, src = build_world(
    {"shot_a": [(100, 200)], "shot_b": [(500, 600)]},
    [make_dest("shot_a", (100, 200), 86400),
     make_dest("shot_b", (500, 600), 86501)])
mod = load(res)
log = run_update(mod)
check_true("an unmanaged timeline is adopted", "ADOPTING" in log)
check_true("a matching timeline reports nothing to do",
           "Nothing to do" in log)
check("no clips were touched", len(project.media_pool.append_calls), 0)
check("no track was added", dest.GetTrackCount("video"), 1)

# Adoption still records a manifest so the next run can use Recorded sources.
manifest, store = mod.read_manifest(dest)
check_true("adoption writes a manifest", manifest is not None)
check("the manifest is marked adopted", manifest["adopted"], True)
check("the manifest records the source timeline",
      [s["name"] for s in manifest["sources"]], ["SRC_01"])
check("the manifest records the connection threshold",
      manifest["settings"]["connection_threshold"], 25)
check_true("the manifest is readable back from metadata", store == "metadata")

# The badge marker carries the same payload for when metadata does not persist.
badge = mod.find_manifest_marker_frame(mod.normalised_markers(dest))
check_true("a badge marker is written too", badge is not None)
dest.mpi.third_party.clear()
manifest_from_marker, store2 = mod.read_manifest(dest)
check("the marker alone can rebuild the manifest",
      manifest_from_marker["sources"], manifest["sources"])
check("and reports where it came from", store2, "marker")

# ---------------------------------------------------------------------------
# 3. Extending a shot that has room
# ---------------------------------------------------------------------------

print("\n== extend, in place ==")

# Room to grow: the clip sits 100 frames into the timeline and is last on its
# track, so there is space at the head and unbounded space at the tail.
res, project, dest, src = build_world(
    {"shot_a": [(80, 240)]},
    [make_dest("shot_a", (100, 200), 86500, color="Orange", flags=["Blue"],
               label="A010_plate")])
dest.tracks[1][0].markers[150] = {"color": "Green", "name": "check this",
                                  "note": "user note", "duration": 1,
                                  "customData": ""}
mod = load(res)
log = run_update(mod)

placed = items_on(dest, 1)
check("the clip is still on its own track", len(placed), 1)
check("the source range now covers the new extents",
      (placed[0].source_start, placed[0].source_end), (80, 240))
check("the clip is anchored so kept frames hold position",
      placed[0].record_start, 86500 - 20)
check("no update track was created", dest.GetTrackCount("video"), 1)
check("the user's clip name is reapplied", placed[0].name, "A010_plate")
check("the clip colour is reapplied", placed[0].color, "Orange")
check("the flags are reapplied", placed[0].flags, ["Blue"])
check_true("the user's own marker is restored", 150 in placed[0].markers)
check("the restored marker keeps its note",
      placed[0].markers[150]["note"], "user note")
marks = update_markers(placed[0])
check("exactly one update marker is written", len(marks), 1)
check("it is coloured for an extension", marks[0]["color"], "Green")
check_true("its name says what happened", "extended" in marks[0]["name"])
check_true("its note carries the deltas",
           "head +20f" in marks[0]["note"] and "tail +40f" in marks[0]["note"])
check_true("its note carries the ranges",
           "100-200 -> 80-240" in marks[0]["note"])

# ---------------------------------------------------------------------------
# 4. Extending a shot boxed in by its neighbour
# ---------------------------------------------------------------------------

print("\n== extend, no room ==")

# A gapless spine is the normal shape of a freshly generated All Clips timeline,
# and it is exactly the case where a clip cannot grow where it stands. The
# updated copy goes to the run's track and the original is left intact.

res, project, dest, src = build_world(
    {"shot_a": [(80, 240)], "shot_b": [(500, 600)]},
    [make_dest("shot_a", (100, 200), 86400),
     make_dest("shot_b", (500, 600), 86501)])
mod = load(res)
log = run_update(mod)

track1 = items_on(dest, 1)
check("the boxed-in clip stays where it was", len(track1), 2)
check("its range is unchanged",
      (track1[0].source_start, track1[0].source_end), (100, 200))
check("an update track was created", dest.GetTrackCount("video"), 2)
check_true("the track is named for the run",
           dest.GetTrackName("video", 2).startswith("Update 1 - "))
moved = items_on(dest, 2)
check("the updated copy is on the update track", len(moved), 1)
check("the copy carries the full new range",
      (moved[0].source_start, moved[0].source_end), (80, 240))
check("the copy starts after the existing content",
      moved[0].record_start >= dest.start_frame, True)
old_marks = update_markers(track1[0])
check("the original is marked superseded", old_marks[0]["color"], "Purple")
check_true("the superseded note names the update track",
           "Update 1 - " in old_marks[0]["note"])

# ---------------------------------------------------------------------------
# 5. Shortening leaves a gap and does not move its neighbours
# ---------------------------------------------------------------------------

print("\n== shorten ==")

res, project, dest, src = build_world(
    {"shot_a": [(100, 150)], "shot_b": [(500, 600)]},
    [make_dest("shot_a", (100, 200), 86400),
     make_dest("shot_b", (500, 600), 86501)])
mod = load(res)
log = run_update(mod)

track1 = items_on(dest, 1)
check("both clips are still there", len(track1), 2)
check("the shortened clip has the new range",
      (track1[0].source_start, track1[0].source_end), (100, 150))
check("it keeps its record frame", track1[0].record_start, 86400)
check("the untouched neighbour has not moved", track1[1].record_start, 86501)
check("the neighbour was never re-appended",
      [c for c in project.media_pool.append_calls
       if c["mediaPoolItem"].GetName() == "shot_b"], [])
marks = update_markers(track1[0])
check("the shortened clip is marked yellow", marks[0]["color"], "Yellow")

# ---------------------------------------------------------------------------
# 6. New and dropped shots
# ---------------------------------------------------------------------------

print("\n== new and dropped ==")

res, project, dest, src = build_world(
    {"shot_a": [(100, 200)], "shot_new": [(10, 90)]},
    [make_dest("shot_a", (100, 200), 86400),
     make_dest("shot_gone", (700, 800), 86501)])
mod = load(res)
log = run_update(mod)

check("the new shot went to a new track", dest.GetTrackCount("video"), 2)
added = items_on(dest, 2)
check("exactly one clip was added", len(added), 1)
check("it is the new shot", added[0].mpi.GetName(), "shot_new")
check("with the collected range",
      (added[0].source_start, added[0].source_end), (10, 90))
check("its marker is the new colour",
      update_markers(added[0])[0]["color"], "Mint")

track1 = items_on(dest, 1)
check("the dropped shot is still on the timeline", len(track1), 2)
gone = [i for i in track1 if i.mpi.GetName() == "shot_gone"][0]
check("nothing was deleted", gone.source_start, 700)
drop_marks = update_markers(gone)
check("the dropped shot is marked", len(drop_marks), 1)
check("with the dropped colour", drop_marks[0]["color"], "Rose")
check_true("and says so", "No longer used" in drop_marks[0]["name"])

# ---------------------------------------------------------------------------
# 7. The churn test: run again with nothing changed
# ---------------------------------------------------------------------------

print("\n== churn: a second run with unchanged sources ==")

before_appends = len(project.media_pool.append_calls)
before_tracks = dest.GetTrackCount("video")
log2 = run_update(mod, source_selection_mode="Recorded")
check_true("the second run finds nothing to do", "Nothing to do" in log2)
check("no clip was re-appended",
      len(project.media_pool.append_calls), before_appends)
check("no track was added", dest.GetTrackCount("video"), before_tracks)

# A dropped shot stays dropped without collecting a marker on every run.
gone_marks = update_markers(gone)
check("the dropped shot is not re-marked", len(gone_marks), 1)

# ---------------------------------------------------------------------------
# 8. Dry run
# ---------------------------------------------------------------------------

print("\n== dry run ==")

res, project, dest, src = build_world(
    {"shot_a": [(80, 240)]},
    [make_dest("shot_a", (100, 200), 86400)])
mod = load(res)
log = run_update(mod, dry_run=True)
check_true("the dry run says so", "DRY RUN" in log)
check_true("it still reports the plan", "extended" in log)
check("nothing was appended", len(project.media_pool.append_calls), 0)
check("the clip is untouched",
      (items_on(dest, 1)[0].source_start, items_on(dest, 1)[0].source_end),
      (100, 200))
check("no marker was written", len(update_markers(items_on(dest, 1)[0])), 0)
check("no manifest was written", mod.read_manifest(dest)[0], None)

# ---------------------------------------------------------------------------
# 9. Clips that must not be rebuilt
# ---------------------------------------------------------------------------

print("\n== protected clips ==")

res, project, dest, src = build_world(
    {"shot_a": [(80, 240)]},
    [make_dest("shot_a", (100, 200), 86400, fusion_comps=2)])
mod = load(res)
log = run_update(mod)
kept = items_on(dest, 1)[0]
check("a clip with a Fusion comp is not rebuilt",
      (kept.source_start, kept.source_end), (100, 200))
check("its Fusion comps are intact", kept.fusion_comps, 2)
check_true("the log explains the skip", "Fusion comp" in log)
marks = update_markers(kept)
check("it is marked for manual attention", marks[0]["color"], "Fuchsia")

res, project, dest, src = build_world(
    {"shot_a": [(80, 240)]},
    [make_dest("shot_a", (100, 200), 86500, fusion_comps=2)])
mod = load(res)
log = run_update(mod, protect_graded_clips=False)
rebuilt = items_on(dest, 1)[0]
check("unprotecting rebuilds it anyway",
      (rebuilt.source_start, rebuilt.source_end), (80, 240))
check_true("but the loss is reported", "cannot survive" in log)

res, project, dest, src = build_world(
    {"shot_a": [(80, 240)]},
    [make_dest("shot_a", (100, 200), 86400, linked=1)])
mod = load(res)
log = run_update(mod)
linked_item = items_on(dest, 1)[0]
check("a clip with linked audio is not rebuilt",
      (linked_item.source_start, linked_item.source_end), (100, 200))
check_true("the log explains why", "linked audio" in log)

# ---------------------------------------------------------------------------
# 10. Manifest bookkeeping across runs
# ---------------------------------------------------------------------------

print("\n== run history ==")

res, project, dest, src = build_world(
    {"shot_a": [(80, 240)]},
    [make_dest("shot_a", (100, 200), 86500)])
mod = load(res)
run_update(mod)
manifest, _store = mod.read_manifest(dest)
check("the run counter starts at 1", manifest["run_counter"], 1)
check("one run is recorded", len(manifest["runs"]), 1)
check("the run counts the extension", manifest["runs"][0]["extended"], 1)
check("a run that adds nothing creates no track",
      manifest["runs"][0]["track"], None)
run_marker = [m for m in dest.markers.values()
              if str(m.get("customData", "")).startswith("RCT_RUN:")]
check("a run marker is written to the timeline ruler", len(run_marker), 1)
check_true("it names the run", run_marker[0]["name"].startswith("Update run 1"))

# ---------------------------------------------------------------------------
# 11. main() dispatch
# ---------------------------------------------------------------------------
#
# The dialog cannot be driven headlessly, but the parameter hand-off from it to
# the two workflows can — and a mismatched keyword there would only surface as a
# TypeError inside Resolve.

print("\n== main() dispatch ==")

res, project, dest, src = build_world(
    {"shot_a": [(100, 200)]}, [make_dest("shot_a", (100, 200), 86400)])
mod = load(res)

UI_RESULT = {
    "mode": mod.MODE_CREATE,
    "source_selection_mode": "Union",
    "dst_timeline_name": "All_Sources",
    "selection_method": "Current Selection",
    "sorting_method": "Source Name",
    "connection_threshold": 25,
    "allow_disabled_clips": False,
    "video_only": True,
    "mark_duplicates": True,
    "mark_retimed_clips": True,
    "use_xml_retime": False,
    "import_clip_names": False,
    "preserve_track_layout": False,
    "merge_by_source_file": True,
    "protect_graded_clips": True,
    "dry_run": True,
}

calls = []
mod.run_workflow = lambda **kw: calls.append(("create", kw))
mod.run_update_workflow = lambda **kw: calls.append(("update", kw))
mod.ProgressUI = lambda: types.SimpleNamespace(close=lambda: None, set=lambda *a: None)

mod.build_and_show_ui = lambda: dict(UI_RESULT)
mod.main()
check("create mode calls run_workflow", calls[-1][0], "create")
check("create mode passes the destination name",
      calls[-1][1]["dst_timeline_name"], "All_Sources")
check("create mode does not pass update-only options",
      [k for k in ("dry_run", "protect_graded_clips", "source_selection_mode")
       if k in calls[-1][1]], [])

mod.build_and_show_ui = lambda: dict(UI_RESULT, mode=mod.MODE_UPDATE)
mod.main()
check("update mode calls run_update_workflow", calls[-1][0], "update")
check("update mode passes the source mode",
      calls[-1][1]["source_selection_mode"], "Union")
check("update mode passes dry run", calls[-1][1]["dry_run"], True)
check("update mode does not pass create-only options",
      [k for k in ("dst_timeline_name", "sorting_method", "preserve_track_layout")
       if k in calls[-1][1]], [])

mod.build_and_show_ui = lambda: None
mod.main()
check("cancelling the dialog runs nothing", len(calls), 2)

# The kwargs the dialog produces must actually satisfy the two signatures.
import inspect  # noqa: E402

fresh = load(res)
for mode, func in ((mod.MODE_CREATE, fresh.run_workflow),
                   (mod.MODE_UPDATE, fresh.run_update_workflow)):
    captured = []
    fresh.run_workflow = lambda **kw: captured.append(kw)
    fresh.run_update_workflow = lambda **kw: captured.append(kw)
    fresh.ProgressUI = lambda: types.SimpleNamespace(close=lambda: None,
                                                     set=lambda *a: None)
    fresh.build_and_show_ui = lambda m=mode: dict(UI_RESULT, mode=m)
    fresh.main()
    try:
        # captured[0] already carries the progress kwarg main() supplies.
        inspect.signature(func).bind(**captured[0])
        bound = True
        problem = ""
    except TypeError as exc:
        bound = False
        problem = str(exc)
    check(f"{mode} kwargs satisfy the workflow signature", bound, True)
    if not bound:
        print(f"       {problem}")


# ---------------------------------------------------------------------------
# 12. Preserve Source Track Layout record frames (create mode)
# ---------------------------------------------------------------------------
#
# Regression for a bug that predates update mode. AppendToTimeline's recordFrame
# is absolute - the same space as GetStart() - while the preserve-layout cursor
# and its block markers are zero-based offsets from the timeline start. Passing
# the raw cursor put every clip an hour before the timeline's own start, with the
# block marker meant to label it 90000 frames away. Verified on 21.0.4.5 before
# fixing: recordFrame=0 on a timeline starting at 90000 really does place the
# clip at absolute frame 0.

print("\n== preserve track layout record frames ==")

res, project, dest, src = build_world(
    {"shot_a": [(100, 200)], "shot_b": [(500, 600)]},
    [make_dest("shot_a", (100, 200), 86400)])
mod = load(res)

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    mod.run_workflow(
        dst_timeline_name="Layout_Test",
        selection_method="Current Selection",
        sorting_method="None",
        connection_threshold=25,
        allow_disabled_clips=False,
        video_only=True,
        mark_duplicates=False,
        mark_retimed_clips=False,
        use_xml_retime=False,
        import_clip_names=False,
        preserve_track_layout=True,
        merge_by_source_file=True,
    )

created = project.timelines[-1]
check("a timeline was created", created.GetName(), "Layout_Test")
record_frames = [c.get("recordFrame") for c in project.media_pool.append_calls
                 if c.get("recordFrame") is not None]
check_true("preserve layout passes explicit record frames", record_frames)
check("no clip is placed before the timeline start",
      [f for f in record_frames if f < created.start_frame], [])

placed_starts = sorted(i.GetStart() for i in created.all_items())
check("the first clip sits at the timeline start",
      placed_starts[0], created.start_frame)

# The block marker and the block it labels must land on the same picture.
marker_offsets = sorted(created.markers)
check_true("a source-timeline block marker was written", marker_offsets)
check("the block marker is aligned with the block it labels",
      created.start_frame + marker_offsets[0], placed_starts[0])

# Relative layout within the block is still preserved.
check("relative spacing between clips is unchanged",
      placed_starts[1] - placed_starts[0], 101)


# ---------------------------------------------------------------------------

print("")
if fails:
    print(f"RESULT: FAIL ({len(fails)} failed)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
