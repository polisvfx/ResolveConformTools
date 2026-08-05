--[[
Part C test: Shot Numbering.lua scope + numbering semantics.

Loads the real script with Main() stripped and a stubbed environment, then calls
the real Main() with a stubbed ShowConfigDialog to drive each scenario. Stub
MediaPoolItems and TimelineItems record every SetMetadata / AddMarker call so the
resulting numbering can be asserted exactly.

Run:  fuscript.exe -l lua test_shot_numbering.lua
--]]

-- Repo root is the parent of this tests/ directory, derived from this file's own
-- path so the suite works from any checkout. Pass a root as arg[1] to override.
local function repoRoot()
    if arg and arg[1] and arg[1] ~= "" then
        return (arg[1]:gsub("[/\\]$", "")) .. "/"
    end
    local src = debug.getinfo(1, "S").source:gsub("^@", "")
    local dir = src:match("^(.*)[/\\][^/\\]+$")
    if not dir or dir == "" then dir = "." end
    local parent = dir:match("^(.*)[/\\]tests$")
    if parent and parent ~= "" then return parent .. "/" end
    if dir == "tests" then return "./" end
    return dir .. "/"
end

local SCRIPT = repoRoot() .. "Shot Numbering.lua"

local failures = 0
local function check(label, got, want)
    if got == want then
        print(string.format("  [ok  ] %s", label))
    else
        failures = failures + 1
        print(string.format("  [FAIL] %s", label))
        print(string.format("           got  %s", tostring(got)))
        print(string.format("           want %s", tostring(want)))
    end
end

-- ---------------------------------------------------------------------------
-- Stubs
-- ---------------------------------------------------------------------------

local uidCounter = 0
local function nextUid()
    uidCounter = uidCounter + 1
    return "uid-" .. tostring(uidCounter)
end

local function NewMediaPoolItem(name)
    local self = { _name = name, _meta = {}, _uid = nextUid(), setCalls = {} }
    function self:GetName() return self._name end
    function self:GetUniqueId() return self._uid end
    function self:GetMetadata(k) return self._meta[k] end
    function self:SetMetadata(k, v)
        self._meta[k] = v
        table.insert(self.setCalls, v)
        return true
    end
    return self
end

-- track/start/mpi -> a stub TimelineItem
local function NewTimelineItem(mpi, trackIndex, startFrame, dur, trackType)
    local self = {
        _mpi = mpi, _track = trackIndex, _start = startFrame,
        _dur = dur or 10, _uid = nextUid(),
        _trackType = trackType or "video",
        markers = {},
    }
    function self:GetMediaPoolItem() return self._mpi end
    function self:GetUniqueId() return self._uid end
    function self:GetStart() return self._start end
    function self:GetEnd() return self._start + self._dur end
    function self:GetDuration() return self._dur end
    function self:GetLeftOffset() return 0 end
    function self:GetName()
        return (self._mpi and self._mpi:GetName()) or "generator"
    end
    function self:AddMarker(frame, color, name, note, duration)
        table.insert(self.markers, { color = color, name = name, note = note })
        return true
    end
    function self:GetMarkers() return {} end
    function self:DeleteMarkerAtFrame(f) return true end
    return self
end

-- Builds a stub Timeline out of a list of {mpi, track, start} descriptors.
local function NewTimeline(name, items, opts)
    opts = opts or {}
    local self = { _name = name, _items = items, _sel = opts.selection }
    function self:GetName() return self._name end
    function self:GetTrackCount(tt)
        if tt ~= "video" then return 0 end
        local maxTrack = 0
        for _, it in ipairs(self._items) do
            if it._trackType == "video" and it._track > maxTrack then
                maxTrack = it._track
            end
        end
        return maxTrack
    end
    function self:GetItemListInTrack(tt, idx)
        local out = {}
        for _, it in ipairs(self._items) do
            if it._trackType == tt and it._track == idx then
                table.insert(out, it)
            end
        end
        return out
    end
    if not opts.noSelectionAPI then
        function self:GetSelectedClips() return self._sel or {} end
    end
    return self
end

-- ---------------------------------------------------------------------------
-- Load the real script with Main() stripped, into a stub environment
-- ---------------------------------------------------------------------------

local fh = io.open(SCRIPT, "r")
if not fh then
    print("FATAL: cannot open " .. SCRIPT)
    os.exit(1)
end
local src = fh:read("*a")
fh:close()

-- Remove only the bottom-of-file invocation, not the definition.
local stripped, n = src:gsub("\nMain%(%)\n", "\n-- Main() call stripped for test\n")
if n ~= 1 then
    print(string.format("FATAL: expected exactly 1 Main() call, found %d", n))
    os.exit(1)
end

local function loadWith(timelineObj)
    local env = {}
    -- Standard library passthrough
    env.print = print; env.string = string; env.table = table; env.math = math
    env.ipairs = ipairs; env.pairs = pairs; env.type = type; env.pcall = pcall
    env.tostring = tostring; env.tonumber = tonumber; env.os = os; env.io = io
    env.setmetatable = setmetatable; env.rawget = rawget; env.select = select
    env.error = error; env.unpack = unpack

    -- Resolve API stubs. The script does this at load:
    --   resolve = bmd.scriptapp("Resolve"); fusion = resolve:Fusion()
    --   project = resolve:GetProjectManager():GetCurrentProject()
    --   timeline = project:GetCurrentTimeline()
    local project = {}
    function project:GetCurrentTimeline() return timelineObj end
    function project:GetMediaPool() return {} end

    local pm = {}
    function pm:GetCurrentProject() return project end

    local resolve = {}
    function resolve:GetProjectManager() return pm end
    function resolve:Fusion() return { UIManager = {} } end

    env.bmd = {
        scriptapp = function() return resolve end,
        UIDispatcher = function() return {} end,
    }

    local chunk = assert(loadstring(stripped, "ShotNumbering"))
    setfenv(chunk, env)
    chunk()
    return env
end

-- Drives one scenario: returns the env after a Main() run.
local function run(label, timelineObj, config)
    print("\n" .. string.rep("-", 66))
    print("SCENARIO: " .. label)
    print(string.rep("-", 66))
    local env = loadWith(timelineObj)
    env.ShowConfigDialog = function() return config end
    env.Main()
    return env
end

local function metaOf(mpi) return mpi._meta["Shot"] end
local function markerNames(item)
    local out = {}
    for _, m in ipairs(item.markers) do table.insert(out, m.color .. ":" .. m.name) end
    return table.concat(out, ",")
end

local baseConfig = { padding = 4, increment = 10, clearExisting = false,
                     selectedOnly = false, numbering = "keep" }
local function cfg(over)
    local c = {}
    for k, v in pairs(baseConfig) do c[k] = v end
    for k, v in pairs(over or {}) do c[k] = v end
    return c
end

-- ---------------------------------------------------------------------------
-- Scenarios
-- ---------------------------------------------------------------------------

-- Layout used throughout: source A twice, source B once.
--   V1: A@0, B@10, A@20      ordered -> A@0, B@10, A@20  -> numbers 10, 20, 30
local function build(opts)
    local A = NewMediaPoolItem("clipA")
    local B = NewMediaPoolItem("clipB")
    local a1 = NewTimelineItem(A, 1, 0)
    local b1 = NewTimelineItem(B, 1, 10)
    local a2 = NewTimelineItem(A, 1, 20)
    local items = { a1, b1, a2 }
    local sel = nil
    if opts and opts.select then
        sel = {}
        for _, which in ipairs(opts.select) do
            local map = { a1 = a1, b1 = b1, a2 = a2 }
            table.insert(sel, map[which])
        end
        if opts.extraAudio then
            local C = NewMediaPoolItem("audioC")
            table.insert(sel, NewTimelineItem(C, 1, 5, 10, "audio"))
        end
    end
    local tl = NewTimeline("TL", items,
        { selection = sel, noSelectionAPI = opts and opts.noAPI })
    return { A = A, B = B, a1 = a1, b1 = b1, a2 = a2, tl = tl }
end

print("\n=== 1. whole timeline (baseline, must match pre-1.5 behaviour) ===")
local s = build()
run("whole timeline", s.tl, cfg())
check("A gets 0010 (first instance)", metaOf(s.A), "0010")
check("B gets 0020", metaOf(s.B), "0020")
check("A@20 duplicate gets a Cyan marker for 0030", markerNames(s.a2), "Cyan:0030")
check("A@20 wrote no metadata", #s.A.setCalls, 1)

print("\n=== 2. scoped to B, keep full-timeline numbers ===")
s = build{ select = { "b1" } }
run("selected={B}, keep", s.tl, cfg{ selectedOnly = true })
check("B still gets 0020, same as an unscoped run", metaOf(s.B), "0020")
check("A untouched (outside selection)", metaOf(s.A), nil)
check("A@0 got no marker", markerNames(s.a1), "")
check("A@20 got no marker", markerNames(s.a2), "")

print("\n=== 3. scoped to the DUPLICATE A@20, keep full-timeline numbers ===")
-- Documented consequence: A's first instance is outside the selection, so the
-- selected later instance is a duplicate and gets a marker, not metadata.
s = build{ select = { "a2" } }
run("selected={A@20}, keep", s.tl, cfg{ selectedOnly = true })
check("A metadata untouched", metaOf(s.A), nil)
check("A@20 gets the Cyan duplicate marker for 0030", markerNames(s.a2), "Cyan:0030")

print("\n=== 4. scoped to the DUPLICATE A@20, renumber from scratch ===")
s = build{ select = { "a2" } }
run("selected={A@20}, renumber", s.tl, cfg{ selectedOnly = true, numbering = "renumber" })
check("A@20 is now a first instance and gets 0010", metaOf(s.A), "0010")
check("A@20 got no duplicate marker", markerNames(s.a2), "")

print("\n=== 5. scoped to {B, A@20}, renumber from scratch ===")
s = build{ select = { "b1", "a2" } }
run("selected={B,A@20}, renumber", s.tl, cfg{ selectedOnly = true, numbering = "renumber" })
check("B gets 0010", metaOf(s.B), "0010")
check("A gets 0020", metaOf(s.A), "0020")

print("\n=== 6. selection API absent (pre-21.0.4) ===")
s = build{ noAPI = true }
run("no API, selectedOnly requested", s.tl, cfg{ selectedOnly = true })
check("falls back to whole timeline: A=0010", metaOf(s.A), "0010")
check("falls back to whole timeline: B=0020", metaOf(s.B), "0020")

print("\n=== 7. scope requested but nothing selected ===")
s = build{ select = {} }
run("empty selection", s.tl, cfg{ selectedOnly = true })
check("falls back to whole timeline: A=0010", metaOf(s.A), "0010")
check("falls back to whole timeline: B=0020", metaOf(s.B), "0020")

print("\n=== 8. selection includes an audio item ===")
s = build{ select = { "b1" }, extraAudio = true }
run("selected={B,audio}, keep", s.tl, cfg{ selectedOnly = true })
check("audio ignored, B still 0020", metaOf(s.B), "0020")
check("A untouched", metaOf(s.A), nil)

print("\n=== 9. overwrite sweep must not reach outside the selection ===")
s = build{ select = { "b1" } }
s.A._meta["Shot"] = "PRESET_A"
s.B._meta["Shot"] = "PRESET_B"
run("selected={B}, clearExisting", s.tl, cfg{ selectedOnly = true, clearExisting = true })
check("B overwritten with 0020", metaOf(s.B), "0020")
check("A's preexisting value survives", metaOf(s.A), "PRESET_A")

print("\n" .. string.rep("=", 66))
if failures == 0 then
    print("RESULT: PASS")
else
    print(string.format("RESULT: FAIL (%d assertion(s))", failures))
end
print(string.rep("=", 66))
os.exit(failures == 0 and 0 or 1)
