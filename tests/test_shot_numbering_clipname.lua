--[[
Part C test: Shot Numbering (Clip Name).lua scope + numbering semantics.

Same approach as test_shot_numbering.lua: load the real script with Main()
stripped into a stub environment, stub ShowConfigDialog, assert on the SetName
calls each stub TimelineItem records.

Run:  fuscript.exe -l lua test_shot_numbering_clipname.lua
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

local SCRIPT = repoRoot() .. "Shot Numbering (Clip Name).lua"

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

local uidCounter = 0
local function nextUid()
    uidCounter = uidCounter + 1
    return "uid-" .. tostring(uidCounter)
end

local function NewMediaPoolItem(name)
    local self = { _name = name, _uid = nextUid() }
    function self:GetName() return self._name end
    function self:GetUniqueId() return self._uid end
    return self
end

local function NewTimelineItem(mpi, trackIndex, startFrame, trackType)
    local self = {
        _mpi = mpi, _track = trackIndex, _start = startFrame,
        _uid = nextUid(), _trackType = trackType or "video",
        _name = (mpi and mpi:GetName()) or "generator",
        setNames = {},
    }
    function self:GetMediaPoolItem() return self._mpi end
    function self:GetUniqueId() return self._uid end
    function self:GetStart() return self._start end
    function self:GetEnd() return self._start + 10 end
    function self:GetDuration() return 10 end
    function self:GetLeftOffset() return 0 end
    function self:GetName() return self._name end
    function self:SetName(n)
        self._name = n
        table.insert(self.setNames, n)
        return true
    end
    return self
end

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

local fh = io.open(SCRIPT, "r")
if not fh then print("FATAL: cannot open " .. SCRIPT); os.exit(1) end
local src = fh:read("*a")
fh:close()

local stripped, n = src:gsub("\nMain%(%)\n", "\n-- stripped\n")
if n ~= 1 then
    print(string.format("FATAL: expected 1 Main() call, found %d", n))
    os.exit(1)
end

local function loadWith(timelineObj)
    local env = {}
    env.print = print; env.string = string; env.table = table; env.math = math
    env.ipairs = ipairs; env.pairs = pairs; env.type = type; env.pcall = pcall
    env.tostring = tostring; env.tonumber = tonumber; env.os = os; env.io = io
    env.setmetatable = setmetatable; env.select = select; env.error = error
    env.unpack = unpack

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

    local chunk = assert(loadstring(stripped, "ShotNumberingClipName"))
    setfenv(chunk, env)
    chunk()
    return env
end

local function run(label, timelineObj, config)
    print("\n" .. string.rep("-", 66))
    print("SCENARIO: " .. label)
    print(string.rep("-", 66))
    local env = loadWith(timelineObj)
    env.ShowConfigDialog = function() return config end
    env.Main()
    return env
end

local base = { prefix = "SH_", padding = 4, increment = 10,
               includeOriginalName = false, separator = "_",
               selectedOnly = false, numbering = "keep" }
local function cfg(over)
    local c = {}
    for k, v in pairs(base) do c[k] = v end
    for k, v in pairs(over or {}) do c[k] = v end
    return c
end

local function lastName(item)
    if #item.setNames == 0 then return nil end
    return item.setNames[#item.setNames]
end

-- Layout: V1 A@0, B@10, C@20  -> numbers 10, 20, 30
local function build(opts)
    opts = opts or {}
    local A, B, C = NewMediaPoolItem("clipA.mov"), NewMediaPoolItem("clipB.mov"),
                    NewMediaPoolItem("clipC.mov")
    local a = NewTimelineItem(A, 1, 0)
    local b = NewTimelineItem(B, 1, 10)
    local c = NewTimelineItem(C, 1, 20)
    local items = { a, b, c }
    local sel = nil
    if opts.select then
        sel = {}
        local map = { a = a, b = b, c = c }
        for _, w in ipairs(opts.select) do table.insert(sel, map[w]) end
    end
    local tl = NewTimeline("TL", items,
        { selection = sel, noSelectionAPI = opts.noAPI })
    return { a = a, b = b, c = c, tl = tl }
end

print("\n=== 1. whole timeline (baseline, must match pre-1.1) ===")
local s = build()
run("whole timeline", s.tl, cfg())
check("a -> SH_0010", lastName(s.a), "SH_0010")
check("b -> SH_0020", lastName(s.b), "SH_0020")
check("c -> SH_0030", lastName(s.c), "SH_0030")

print("\n=== 2. scoped to b, keep full-timeline numbers ===")
s = build{ select = { "b" } }
run("selected={b}, keep", s.tl, cfg{ selectedOnly = true })
check("b -> SH_0020, same as an unscoped run", lastName(s.b), "SH_0020")
check("a untouched", lastName(s.a), nil)
check("c untouched", lastName(s.c), nil)

print("\n=== 3. scoped to c, renumber from scratch ===")
s = build{ select = { "c" } }
run("selected={c}, renumber", s.tl, cfg{ selectedOnly = true, numbering = "renumber" })
check("c -> SH_0010 (numbered as if alone)", lastName(s.c), "SH_0010")
check("a untouched", lastName(s.a), nil)
check("b untouched", lastName(s.b), nil)

print("\n=== 4. scoped to {a,c}, keep vs renumber ===")
s = build{ select = { "a", "c" } }
run("selected={a,c}, keep", s.tl, cfg{ selectedOnly = true })
check("a -> SH_0010", lastName(s.a), "SH_0010")
check("c -> SH_0030 (keeps its full-timeline number)", lastName(s.c), "SH_0030")
check("b untouched", lastName(s.b), nil)

s = build{ select = { "a", "c" } }
run("selected={a,c}, renumber", s.tl, cfg{ selectedOnly = true, numbering = "renumber" })
check("a -> SH_0010", lastName(s.a), "SH_0010")
check("c -> SH_0020 (renumbered)", lastName(s.c), "SH_0020")
check("b untouched", lastName(s.b), nil)

print("\n=== 5. API absent / nothing selected -> whole timeline ===")
s = build{ noAPI = true }
run("no API", s.tl, cfg{ selectedOnly = true })
check("a -> SH_0010", lastName(s.a), "SH_0010")
check("c -> SH_0030", lastName(s.c), "SH_0030")

s = build{ select = {} }
run("empty selection", s.tl, cfg{ selectedOnly = true })
check("a -> SH_0010", lastName(s.a), "SH_0010")
check("c -> SH_0030", lastName(s.c), "SH_0030")

print("\n=== 6. suffix mode still strips the extension ===")
s = build{ select = { "b" } }
run("selected={b}, keep, includeOriginalName", s.tl,
    cfg{ selectedOnly = true, includeOriginalName = true })
check("b -> SH_0020_clipB", lastName(s.b), "SH_0020_clipB")

print("\n=== 7. scoped restore only touches the selection ===")
s = build{ select = { "b" } }
local env = loadWith(s.tl)
-- Rename everything first, unscoped.
env.ShowConfigDialog = function() return cfg() end
env.Main()
check("pre-restore: a renamed", lastName(s.a), "SH_0010")
-- Now restore, scoped to the selection only.
local ordered = env.GetOrderedTimelineItems(s.tl)
local _, selectedOnly = env.ApplySelectionScope(ordered, true, s.tl)
env.RestoreOriginalClipNames(selectedOnly)
check("b restored to its source name", lastName(s.b), "clipB.mov")
check("a still renamed (outside selection)", lastName(s.a), "SH_0010")
check("c still renamed (outside selection)", lastName(s.c), "SH_0030")

print("\n" .. string.rep("=", 66))
if failures == 0 then
    print("RESULT: PASS")
else
    print(string.format("RESULT: FAIL (%d assertion(s))", failures))
end
print(string.rep("=", 66))
os.exit(failures == 0 and 0 or 1)
