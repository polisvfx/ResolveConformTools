#!/usr/bin/env lua
--
-- Shot Numbering (Clip Name) - part of ResolveConformTools
-- Copyright (C) 2026 Maris Polis - marispolis.com
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU General Public License as published by
-- the Free Software Foundation, either version 3 of the License, or
-- (at your option) any later version.
--
-- This program is distributed in the hope that it will be useful,
-- but WITHOUT ANY WARRANTY; without even the implied warranty of
-- MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
-- GNU General Public License for more details.
--
-- You should have received a copy of the GNU General Public License
-- along with this program.  If not, see <https://www.gnu.org/licenses/>.
--
-- SPDX-License-Identifier: GPL-3.0-or-later
--

-- Script to set sequential shot numbers as Clip Names on timeline items
-- Version: 1.1
-- Requires: DaVinci Resolve 20.2+ (uses TimelineItem:SetName API)
--
-- Uses TimelineItem:SetName() to rename each clip instance on the timeline.
-- Each timeline item gets its own unique sequential name, regardless of
-- whether the same source clip appears multiple times.
-- Original clip names can be preserved as a suffix.
--
-- Scope can be limited to the clips selected on the timeline, which needs
-- Timeline:GetSelectedClips() (DaVinci Resolve 21.0.4+). Two numbering modes are
-- offered when scoped:
--   Keep full-timeline numbers - number the whole timeline as usual but only
--     rename the selected clips, so they match an unscoped run.
--   Renumber from the first number - treat the selection as if it were the
--     entire timeline.
-- On builds without the API the scope control is disabled and behaviour is
-- unchanged.

local SCRIPT_VERSION = "1.1"

-- Get Resolve API and Fusion object
local resolve = bmd.scriptapp("Resolve")
local fusion = resolve:Fusion()

-- Get current project and timeline
local projectManager = resolve:GetProjectManager()
local project = projectManager:GetCurrentProject()
local timeline = project:GetCurrentTimeline()

if timeline == nil then
    print("No timeline is open. Please open a timeline and try again.")
    return
end

-- Re-fetch the timeline the user is actually looking at.
-- The module-level `timeline` handle is captured at load time, i.e. before the
-- config dialog runs. The dialog is not application-modal - the user can (and for
-- the scope option must be able to) click clips and even switch timelines while
-- it is open - so anything that runs after the dialog closes should ask again.
function GetActiveTimeline()
    local tl = nil
    pcall(function() tl = project:GetCurrentTimeline() end)
    return tl or timeline
end


-- Does this build expose Timeline:GetSelectedClips()? (DaVinci Resolve 21.0.4+)
function HasTimelineSelectionAPI(tl)
    local present = false
    pcall(function() present = (tl.GetSelectedClips ~= nil) end)
    return present
end


-- Returns an array of selected TimelineItems, or nil when the API is missing.
-- nil (unsupported) and {} (supported, nothing selected) are deliberately
-- distinct: they produce different messages and different fallbacks.
function GetTimelineSelection(tl)
    if not HasTimelineSelectionAPI(tl) then return nil end
    local items = {}
    local ok = pcall(function()
        local sel = tl:GetSelectedClips()
        if type(sel) == "table" then
            for _, item in pairs(sel) do
                if item then table.insert(items, item) end
            end
        end
    end)
    if not ok then return nil end
    return items
end


-- Stable key for a TimelineItem across separate API calls.
-- Resolve hands back a fresh wrapper object per call, so neither `==` nor identity
-- works between the selection list and the per-track walk (verified on 21.0.4.5:
-- both are false for the same item fetched twice). GetUniqueId() is stable across
-- calls and distinct per instance of a source clip, so it is the real key; the
-- geometric fallback only matters on a build that lacks it.
function TimelineItemKey(item)
    if item == nil then return nil end

    local uid = nil
    pcall(function() uid = item:GetUniqueId() end)
    if uid ~= nil and uid ~= "" then
        return "uid:" .. tostring(uid)
    end

    local mpiId, startFrame, endFrame, leftOffset = "", -1, -1, -1
    pcall(function()
        local mpi = item:GetMediaPoolItem()
        if mpi then mpiId = mpi:GetUniqueId() or "" end
        startFrame = item:GetStart()
        endFrame = item:GetEnd()
        leftOffset = item:GetLeftOffset() or -1
    end)
    return string.format("geo:%s:%d:%d:%d",
        tostring(mpiId), startFrame, endFrame, leftOffset)
end


-- Flags the entries of `orderedItems` that are in the timeline selection.
-- Returns orderedItems, selectedOnly, scopeLabel where selectedOnly == nil means
-- the scope collapsed back to the whole timeline (for any reason, each of which
-- prints its own explanation).
--
-- Audio and subtitle items need no explicit filter here: orderedItems only ever
-- contains video-track items, so a selected audio item simply never matches.
function ApplySelectionScope(orderedItems, wantSelectionOnly, tl)
    for _, itemData in ipairs(orderedItems) do
        itemData.selected = false
    end

    if not wantSelectionOnly then
        return orderedItems, nil, "Whole timeline"
    end

    local selection = GetTimelineSelection(tl)
    if selection == nil then
        print("NOTE: This DaVinci Resolve build has no Timeline:GetSelectedClips()")
        print("      (added in 21.0.4). Falling back to the whole timeline.")
        return orderedItems, nil, "Whole timeline (selection API unavailable)"
    end
    if #selection == 0 then
        print("NOTE: 'Selected clips only' was chosen, but nothing is selected on")
        print("      the timeline. Falling back to the whole timeline.")
        return orderedItems, nil, "Whole timeline (nothing was selected)"
    end

    local selectedKeys = {}
    for _, item in ipairs(selection) do
        local key = TimelineItemKey(item)
        if key then selectedKeys[key] = true end
    end

    local selectedOnly = {}
    local seenKeys = {}
    for _, itemData in ipairs(orderedItems) do
        local key = TimelineItemKey(itemData.item)
        if key and selectedKeys[key] then
            if seenKeys[key] then
                print(string.format("  WARNING: two items share identity key %s", key))
                print("           - both are treated as selected.")
            end
            seenKeys[key] = true
            itemData.selected = true
            table.insert(selectedOnly, itemData)
        end
    end

    if #selectedOnly == 0 then
        print(string.format("NOTE: %d item(s) selected, but none are video clips on", #selection))
        print("      this timeline. Falling back to the whole timeline.")
        return orderedItems, nil, "Whole timeline (no video clips selected)"
    end

    local ignored = #selection - #selectedOnly
    if ignored > 0 then
        print(string.format("  Ignoring %d selected item(s) that are not video clips on this timeline.", ignored))
    end
    return orderedItems, selectedOnly,
        string.format("Selected clips only (%d of %d timeline items)",
            #selectedOnly, #orderedItems)
end


-- Function to get all timeline items ordered by their position
function GetOrderedTimelineItems(tl)
    tl = tl or GetActiveTimeline()
    local trackTypes = {"video"}
    local allItems = {}
    for _, trackType in ipairs(trackTypes) do
        local trackCount = tl:GetTrackCount(trackType)
        for trackIndex = trackCount, 1, -1 do
            local trackItems = tl:GetItemListInTrack(trackType, trackIndex)
            if trackItems then
                for _, item in ipairs(trackItems) do
                    table.insert(allItems, {
                        item = item,
                        start = item:GetStart(),
                        trackType = trackType,
                        trackIndex = trackIndex
                    })
                end
            end
        end
    end
    table.sort(allItems, function(a, b)
        if a.start == b.start then
            return a.trackIndex < b.trackIndex
        end
        return a.start < b.start
    end)
    return allItems
end


-- Function to restore clip names from their Media Pool source.
-- `itemList` restricts the sweep; defaults to the whole timeline. Callers pass the
-- scoped list: an unscoped restore is destructive and asymmetric, since a user who
-- had just renamed five selected clips would otherwise rename the whole timeline.
function RestoreOriginalClipNames(itemList)
    local items = itemList or GetOrderedTimelineItems()
    local restoredCount = 0
    print("Restoring original clip names from Media Pool items...")
    for _, itemData in ipairs(items) do
        local timelineItem = itemData.item
        local mediaPoolItem = timelineItem:GetMediaPoolItem()
        if mediaPoolItem then
            local originalName = mediaPoolItem:GetName()
            if originalName and originalName ~= "" then
                if timelineItem:SetName(originalName) then
                    restoredCount = restoredCount + 1
                else
                    print(string.format("  WARNING: Failed to restore name for clip at T%d @ %d", itemData.trackIndex, itemData.start))
                end
            end
        end
    end
    print(string.format("Completed: Restored original names on %d clips", restoredCount))
    return restoredCount
end


-- Function to show configuration dialog
function ShowConfigDialog()
    local ui = fusion.UIManager
    local disp = bmd.UIDispatcher(ui)
    -- Defaults reproduce the pre-1.1 behaviour exactly.
    local config = {
        prefix = "SH_",
        padding = 4,
        increment = 10,
        includeOriginalName = false,
        separator = "_",
        selectedOnly = false,
        numbering = "keep"
    }
    local result = nil

    local win = disp:AddWindow({
        WindowTitle = "Shot Numbering - Clip Name (v" .. SCRIPT_VERSION .. ")",
        ID = "ConfigWin",
        Geometry = { 100, 100, 470, 440 },
        Spacing = 10,
        ui:VGroup{ ID = "root", Weight = 1.0,
            ui:HGroup{
                ui:Label{ Text = "Prefix:", Weight = 0.3 },
                ui:LineEdit{ ID = "PrefixInput", Text = config.prefix, PlaceholderText = "e.g. SH_", Weight = 0.7 }
            },
            ui:HGroup{
                ui:Label{ Text = "Number Padding:", Weight = 0.3 },
                ui:SpinBox{ ID = "PaddingInput", Value = config.padding, Minimum = 1, Maximum = 10, Weight = 0.7 }
            },
            ui:HGroup{
                ui:Label{ Text = "Increment By:", Weight = 0.3 },
                ui:SpinBox{ ID = "IncrementInput", Value = config.increment, Minimum = 1, Maximum = 1000, Weight = 0.7 }
            },
            ui:HGroup{
                ui:Label{ Text = "Separator:", Weight = 0.3 },
                ui:LineEdit{ ID = "SeparatorInput", Text = config.separator, PlaceholderText = "e.g. _", Weight = 0.7 }
            },
            ui:HGroup{
                ui:Label{ Text = "Apply To:", Weight = 0.3 },
                ui:ComboBox{ ID = "ScopeCombo", Weight = 0.7 }
            },
            ui:HGroup{
                ui:Label{ Text = "Numbering:", Weight = 0.3 },
                ui:ComboBox{ ID = "NumberingCombo", Weight = 0.7 }
            },
            ui:Label{ ID = "ScopeNote", Text = "", WordWrap = true },
            ui:CheckBox{
                ID = "IncludeOriginalCheckBox",
                Text = "Append original clip name as suffix",
                Checked = config.includeOriginalName
            },
            ui:VGap(5),
            ui:Label{
                Text = "Requires DaVinci Resolve 20.2+. Uses TimelineItem:SetName() to rename each clip instance independently. The Media Pool source clip is not affected.",
                WordWrap = true
            },
            ui:VGap(10),
            ui:Button{ ID = "RestoreButton", Text = "Restore Original Clip Names" },
            ui:VGap(10),
            ui:HGroup{ Weight = 0,
                ui:Button{ ID = "CancelButton", Text = "Cancel" },
                ui:Button{ ID = "OKButton", Text = "Run Numbering" }
            }
        }
    })

    local itm = win:GetItems()

    itm.ScopeCombo:AddItem("Whole timeline")
    itm.ScopeCombo:AddItem("Selected clips only")
    itm.NumberingCombo:AddItem("Keep full-timeline numbers")
    itm.NumberingCombo:AddItem("Renumber selection from the first number")
    itm.NumberingCombo.Enabled = false

    if not HasTimelineSelectionAPI(GetActiveTimeline()) then
        itm.ScopeCombo.Enabled = false
        itm.ScopeNote.Text = "'Selected clips only' needs DaVinci Resolve 21.0.4 or newer."
    else
        itm.ScopeNote.Text = "Selection is read when this dialog closes, so you can select clips now."
    end

    function win.On.ScopeCombo.CurrentIndexChanged(ev)
        itm.NumberingCombo.Enabled = (itm.ScopeCombo.CurrentIndex == 1)
    end

    function win.On.ConfigWin.Close(ev)
        disp:ExitLoop()
    end

    function win.On.CancelButton.Clicked(ev)
        result = nil
        disp:ExitLoop()
    end

    function win.On.OKButton.Clicked(ev)
        config.prefix = itm.PrefixInput.Text
        config.padding = itm.PaddingInput.Value
        config.increment = itm.IncrementInput.Value
        config.separator = itm.SeparatorInput.Text
        config.includeOriginalName = itm.IncludeOriginalCheckBox.Checked
        config.selectedOnly = (itm.ScopeCombo.CurrentIndex == 1)
        config.numbering = (itm.NumberingCombo.CurrentIndex == 1) and "renumber" or "keep"
        result = config
        disp:ExitLoop()
    end

    function win.On.RestoreButton.Clicked(ev)
        print("\n--- Restoring Original Clip Names ---")
        -- Honour whatever the scope combo says right now, so Restore can never undo
        -- more than the run it is undoing.
        local tl = GetActiveTimeline()
        local ordered = GetOrderedTimelineItems(tl)
        local _, selectedOnly, scopeLabel =
            ApplySelectionScope(ordered, itm.ScopeCombo.CurrentIndex == 1, tl)
        print("Scope: " .. scopeLabel)
        local restored = RestoreOriginalClipNames(selectedOnly or ordered)
        itm.RestoreButton.Text = string.format("Restored (%d clips, %s)", restored, scopeLabel)
        print("--- Restore Complete ---")
    end

    win:Show()
    disp:RunLoop()
    win:Hide()
    return result
end


-- Main script execution
function Main()
    local config = ShowConfigDialog()
    if config == nil then
        print("\nOperation canceled by user.")
        return false
    end

    local prefix = config.prefix
    local padding = config.padding
    local shotStep = config.increment
    local separator = config.separator
    local includeOriginalName = config.includeOriginalName
    local formatString = "%0" .. tostring(padding) .. "d"

    print("\n--- Starting Shot Numbering - Clip Name (v" .. SCRIPT_VERSION .. ") ---")
    print("Configuration:")
    print(string.format("  Prefix: '%s', Padding: %d, Increment: %d", prefix, padding, shotStep))
    print(string.format("  Include original name: %s, Separator: '%s'", includeOriginalName and "Yes" or "No", separator))

    -- Ask for the timeline again: the dialog is not application-modal, so the user
    -- may have switched timelines (and must have been able to click clips) while it
    -- was open.
    local tl = GetActiveTimeline()
    print(string.format("  Timeline: %s", tl:GetName()))

    local items = GetOrderedTimelineItems(tl)
    if #items == 0 then
        print("\nNo video clips found on the timeline.")
        return false
    end

    local orderedItems, selectedOnly, scopeLabel =
        ApplySelectionScope(items, config.selectedOnly, tl)
    local renumberFromScratch = (selectedOnly ~= nil) and (config.numbering == "renumber")

    print(string.format("  Scope: %s", scopeLabel))
    print(string.format("  Numbering: %s", renumberFromScratch
        and "renumber selection from the first number"
        or "keep full-timeline numbers"))

    -- Build the work list. Each entry is { data = itemData, apply = bool }.
    --
    -- "keep full-timeline numbers" walks the ENTIRE ordered list so the counter
    -- advances exactly as an unscoped run would, and only renames selected clips -
    -- so a selected clip ends up with the same name a full run would have given it.
    --
    -- "renumber from scratch" walks only the selection, numbering it from the first
    -- number as though it were the whole timeline.
    local workList = {}
    if renumberFromScratch then
        for _, itemData in ipairs(selectedOnly) do
            table.insert(workList, { data = itemData, apply = true })
        end
    else
        for _, itemData in ipairs(orderedItems) do
            table.insert(workList, {
                data = itemData,
                apply = (selectedOnly == nil) or itemData.selected,
            })
        end
    end

    local appliedCount = 0
    for _, entry in ipairs(workList) do
        if entry.apply then appliedCount = appliedCount + 1 end
    end

    local currentShotNumberValue = shotStep
    local renamedCount = 0
    local failedCount = 0
    local skippedOutOfScope = 0

    -- Build preview of first clip name
    local previewNumber = string.format(formatString, currentShotNumberValue)
    local previewName = prefix .. previewNumber
    if includeOriginalName then
        previewName = previewName .. separator .. "(clipname)"
    end
    print(string.format("\nProcessing %d timeline items...", #workList))
    if appliedCount ~= #workList then
        print(string.format("  (renaming %d of them; the rest are walked only to keep the numbering aligned)", appliedCount))
    end
    print(string.format("Name pattern: '%s'", previewName))

    for i, entry in ipairs(workList) do
        local itemData = entry.data
        local timelineItem = itemData.item
        local shotNumberStr = string.format(formatString, currentShotNumberValue)

        -- Build the new clip name
        local newName = prefix .. shotNumberStr

        if includeOriginalName then
            -- Get original name from Media Pool item for the suffix
            local originalName = ""
            local mediaPoolItem = timelineItem:GetMediaPoolItem()
            if mediaPoolItem then
                originalName = mediaPoolItem:GetName() or ""
            end
            if originalName ~= "" then
                -- Strip file extension from the suffix (e.g. ".mov", ".mxf", ".R3D")
                local stem = originalName:match("^(.+)%.[^.]+$")
                if stem and stem ~= "" then
                    originalName = stem
                end
                -- Strip Resolve's file-sequence range suffix (e.g. " [0001-0100]", "_[0001-0100]")
                local trimmed = originalName:match("^(.-)[%s%._%-]*%[%d+%-%d+%]$")
                if trimmed and trimmed ~= "" then
                    originalName = trimmed
                end
                newName = newName .. separator .. originalName
            end
        end

        local currentName = timelineItem:GetName() or "Unnamed"
        local clipInfoStr = string.format("'%s' (T%d @ %d)", currentName, itemData.trackIndex, itemData.start)

        if not entry.apply then
            -- Walked only to keep the sequence aligned with a full-timeline run.
            skippedOutOfScope = skippedOutOfScope + 1
        elseif timelineItem:SetName(newName) then
            print(string.format("  Renamed %s -> '%s'", clipInfoStr, newName))
            renamedCount = renamedCount + 1
        else
            print(string.format("  FAILED to rename %s -> '%s'", clipInfoStr, newName))
            failedCount = failedCount + 1
        end

        currentShotNumberValue = currentShotNumberValue + shotStep
    end

    print("\n--- Shot Numbering Summary ---")
    print(string.format("Scope: %s", scopeLabel))
    print(string.format("Items walked for numbering: %d", #workList))
    print(string.format("  Successfully renamed: %d", renamedCount))
    if skippedOutOfScope > 0 then
        print(string.format("  Walked but left untouched (outside selection): %d", skippedOutOfScope))
    end
    if failedCount > 0 then
        print(string.format("  Failed: %d", failedCount))
    end

    return true
end

-- Run the script
Main()
print("\n--- Script Finished ---")
