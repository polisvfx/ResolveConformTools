-- Parse-check the repo's Lua scripts without executing them.
-- loadfile() compiles and returns a function (or nil + error), so syntax errors
-- surface without any of the scripts touching Resolve.

-- Repo root is the parent of this tests/ directory, derived from this file's own
-- path so the checker works from any checkout instead of one hardcoded install.
-- Pass an explicit root as the first argument to override.
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

local base = repoRoot()
print("Repo root: " .. base)

local files = {
    "Generate All Clips Timeline.lua",
    "Shot Numbering.lua",
    "Shot Numbering (Clip Name).lua",
    "Mark Duplicate Clips.lua",
    "Copy Timeline Settings.lua",
    "Remove Audio.lua",
}

local failures = 0
for _, f in ipairs(files) do
    local fn, err = loadfile(base .. f)
    if fn then
        print("OK    " .. f)
    else
        failures = failures + 1
        print("FAIL  " .. f)
        print("        " .. tostring(err))
    end
end

print("")
if failures == 0 then
    print("All " .. tostring(#files) .. " Lua files parse cleanly.")
else
    print(tostring(failures) .. " file(s) FAILED to parse.")
end
