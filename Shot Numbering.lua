#!/usr/bin/env lua
--
-- Shot Numbering - part of ResolveConformTools
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

-- Script to set Shot metadata field for clips in the timeline
-- Version: 1.5
-- Assigns sequential shot numbers based strictly on timeline order.
-- 'Shot' metadata is set ONLY on the first instance of each source clip encountered.
-- Duplicate instances have their 'Shot' metadata UNCHANGED, but receive a marker
-- showing the sequential number they consumed in the timeline order.
-- Every clip consumes a number in the sequence.
-- Allows configuration of padding, increments, and overwriting existing values.
--
-- Scope can be limited to the clips selected on the timeline, which needs
-- Timeline:GetSelectedClips() (DaVinci Resolve 21.0.4+). Two numbering modes are
-- offered when scoped:
--   Keep full-timeline numbers - number the whole timeline as usual but only
--     write to the selected clips, so they match an unscoped run.
--   Renumber from the first number - treat the selection as if it were the
--     entire timeline.
-- On builds without the API the scope control is disabled and behaviour is
-- unchanged.

local SCRIPT_VERSION = "1.5"

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

local mediaPool = project:GetMediaPool()

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

-- CORRECTED AddMarker FUNCTION (No changes needed from v1.2)
function AddMarker(item, markerName, markerColor, markerNote)
    markerNote = markerNote or "Shotnumber Status" -- Default note if not provided
    local sourceSegmentStartFrame = item:GetLeftOffset()
    local timelineDuration = item:GetDuration()
    if sourceSegmentStartFrame ~= nil and timelineDuration ~= nil and timelineDuration > 0 then
        local offsetInTimeline = math.floor(timelineDuration * 0.75) -- 75% placement
        local markerFrameId = sourceSegmentStartFrame + offsetInTimeline
        -- AddMarker(frameId, colorName, name, note, duration)
        local success = item:AddMarker(markerFrameId, markerColor, markerName, markerNote, 1)
        if success then
            -- Verbose logging can be enabled if needed
            -- print(string.format("  Added %s marker '%s' at Source Frame %d", markerColor, markerName, markerFrameId))
        else
            print(string.format("  FAILED to add %s marker '%s' to clip '%s' at target Source Frame %d", markerColor, markerName, item:GetName(), markerFrameId))
        end
        return success
    else
         -- Verbose logging can be enabled if needed
         -- print(string.format("  Skipped adding marker '%s' to clip '%s' - Invalid duration/offset", markerName, item:GetName()))
        return false
    end
end


-- Function to clear all markers with a specific note.
-- `itemList` restricts the sweep; defaults to the whole timeline. Callers pass the
-- scoped list so a "Selected clips only" run never reaches outside the selection.
function ClearMarkersByNote(noteToClear, itemList)
    local items = itemList or GetOrderedTimelineItems()
    local clearedCount = 0
    print(string.format("Clearing all markers with note '%s'...", noteToClear))
    for _, itemData in ipairs(items) do
        local timelineItem = itemData.item
        local markers = timelineItem:GetMarkers()
        if type(markers) == "table" then
            local markersDeletedOnThisClip = false
            for frameId, markerInfo in pairs(markers) do
                if markerInfo and markerInfo.note == noteToClear then
                    if timelineItem:DeleteMarkerAtFrame(frameId) then
                        markersDeletedOnThisClip = true
                    else
                         print(string.format("  WARNING: Failed to delete marker at frame %d from clip '%s'", frameId, timelineItem:GetName()))
                    end
                end
            end
            if markersDeletedOnThisClip then clearedCount = clearedCount + 1 end
        end
    end
    print(string.format("Completed: Cleared '%s' markers from %d clips", noteToClear, clearedCount))
    return clearedCount
end


-- Function to clear 'Shot' metadata.
-- `itemList` restricts the sweep; defaults to the whole timeline. Under a scoped
-- run this must receive only the clips that will actually be written, otherwise
-- an overwrite run would blank metadata outside the user's selection.
function ClearShotMetadata(itemList)
    local items = itemList or GetOrderedTimelineItems()
    local clearedCount = 0
    print("Clearing 'Shot' metadata values...")
    for _, itemData in ipairs(items) do
        local timelineItem = itemData.item
        local mediaPoolItem = timelineItem:GetMediaPoolItem()
        if mediaPoolItem then
            local currentShot = mediaPoolItem:GetMetadata("Shot")
            if currentShot and currentShot ~= "" then
                if mediaPoolItem:SetMetadata("Shot", "") then
                    clearedCount = clearedCount + 1
                else
                     print(string.format("  WARNING: Failed to clear Shot value '%s' from clip '%s'", currentShot, mediaPoolItem:GetName()))
                end
            end
        end
    end
    print(string.format("Completed: Cleared Shot metadata from %d clips", clearedCount))
    return clearedCount
end


-- Function to show configuration dialog (Updated title/note)
function ShowConfigDialog()
    local ui = fusion.UIManager
    local disp = bmd.UIDispatcher(ui)
    -- Defaults reproduce the pre-1.5 behaviour exactly.
    local config = { padding = 4, increment = 10, clearExisting = false,
                     selectedOnly = false, numbering = "keep" }
    local result = nil
    local win = disp:AddWindow({
        WindowTitle = "Shot Number Configuration v" .. SCRIPT_VERSION, ID = "ConfigWin", Geometry = { 100, 100, 460, 400 }, Spacing = 10,
        ui:VGroup{ ID = "root", Weight = 1.0,
            ui:HGroup{ ui:Label{ Text = "Number Padding:", Weight = 0.3 }, ui:SpinBox{ ID = "PaddingInput", Value = config.padding, Minimum = 1, Maximum = 10, Weight = 0.7 } },
            ui:HGroup{ ui:Label{ Text = "Increment By:", Weight = 0.3 }, ui:SpinBox{ ID = "IncrementInput", Value = config.increment, Minimum = 1, Maximum = 1000, Weight = 0.7 } },
            ui:HGroup{ ui:Label{ Text = "Apply To:", Weight = 0.3 }, ui:ComboBox{ ID = "ScopeCombo", Weight = 0.7 } },
            ui:HGroup{ ui:Label{ Text = "Numbering:", Weight = 0.3 }, ui:ComboBox{ ID = "NumberingCombo", Weight = 0.7 } },
            ui:Label{ ID = "ScopeNote", Text = "", WordWrap = true },
            ui:CheckBox{ ID = "ClearCheckBox", Text = "Overwrite existing 'Shot' metadata (on first instances)", Checked = config.clearExisting },
            ui:VGap(10),
            ui:Label{ Text = "Note: Sets 'Shot' metadata using a strict sequential number only on the FIRST instance of a source clip. Duplicates consume a number but get a marker instead; metadata is unchanged.", WordWrap = true},
            ui:VGap(15),
            ui:Button{ ID = "ClearMarkersButton", Text = "Clear 'Shotnumber Status' Markers Now" },
            ui:VGap(15),
            ui:HGroup{ Weight = 0, ui:Button{ ID = "CancelButton", Text = "Cancel" }, ui:Button{ ID = "OKButton", Text = "Run Numbering" } }
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

    function win.On.ConfigWin.Close(ev) disp:ExitLoop() end
    function win.On.CancelButton.Clicked(ev) result = nil; disp:ExitLoop() end
    function win.On.OKButton.Clicked(ev)
        config.padding = itm.PaddingInput.Value; config.increment = itm.IncrementInput.Value; config.clearExisting = itm.ClearCheckBox.Checked
        config.selectedOnly = (itm.ScopeCombo.CurrentIndex == 1)
        config.numbering = (itm.NumberingCombo.CurrentIndex == 1) and "renumber" or "keep"
        result = config; disp:ExitLoop()
    end
    function win.On.ClearMarkersButton.Clicked(ev)
        print("\n--- Clearing Markers via Button ---")
        -- Honour whatever the scope combo says right now, so this button can never
        -- reach outside a selection the user has chosen to work within.
        local tl = GetActiveTimeline()
        local ordered = GetOrderedTimelineItems(tl)
        local _, selectedOnly, scopeLabel =
            ApplySelectionScope(ordered, itm.ScopeCombo.CurrentIndex == 1, tl)
        print("Scope: " .. scopeLabel)
        local cleared = ClearMarkersByNote("Shotnumber Status", selectedOnly or ordered)
        itm.ClearMarkersButton.Text = string.format("Cleared Status Markers (%d clips)", cleared)
        print("--- Marker Clearing Complete ---")
    end
    win:Show(); disp:RunLoop(); win:Hide()
    return result
end


-- *** REVISED Main script execution (Strict Sequential, Duplicates Marked) ***
function Main()
    local config = ShowConfigDialog()
    if config == nil then print("\nOperation canceled by user."); return false end

    local padding = config.padding
    local shotStep = config.increment
    local clearExisting = config.clearExisting
    local formatString = "%0" .. tostring(padding) .. "d"
    local markerNote = "Shotnumber Status"

    print("\n--- Starting Shot Numbering (v" .. SCRIPT_VERSION .. " - Strict Sequential, Duplicates Marked) ---")
    print("Configuration:")
    print(string.format("  Padding: %d, Increment: %d, Overwrite Existing (First Instances): %s", padding, shotStep, clearExisting and "Yes" or "No"))

    -- Ask for the timeline again: the dialog is not application-modal, so the user
    -- may have switched timelines (and must have been able to click clips) while it
    -- was open.
    local tl = GetActiveTimeline()
    print(string.format("  Timeline: %s", tl:GetName()))

    local items = GetOrderedTimelineItems(tl)
    if #items == 0 then print("\nNo video clips found on the timeline."); return false end

    local orderedItems, selectedOnly, scopeLabel =
        ApplySelectionScope(items, config.selectedOnly, tl)
    local renumberFromScratch = (selectedOnly ~= nil) and (config.numbering == "renumber")

    print(string.format("  Scope: %s", scopeLabel))
    print(string.format("  Numbering: %s", renumberFromScratch
        and "renumber selection from the first number"
        or "keep full-timeline numbers"))

    -- Build the work list. Each entry is { data = itemData, apply = bool }.
    --
    -- "keep full-timeline numbers" walks the ENTIRE ordered list so the counter and
    -- the first-instance bookkeeping advance exactly as an unscoped run would, and
    -- only writes to selected clips. A consequence worth knowing: if the first
    -- instance of a source clip lies outside the selection, a selected later
    -- instance is a duplicate and gets the Cyan marker rather than 'Shot' metadata
    -- - which is precisely what a whole-timeline run would have done to that clip.
    --
    -- "renumber from scratch" walks only the selection, so first-instance-ness is
    -- computed within the selection: the selection behaves as if it were the whole
    -- timeline.
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

    -- Everything that will actually be written to, for the overwrite sweep below.
    local appliedItems = {}
    for _, entry in ipairs(workList) do
        if entry.apply then table.insert(appliedItems, entry.data) end
    end

    if clearExisting then
        ClearShotMetadata(appliedItems)
    else
        print("\nPreserving existing 'Shot' metadata where found.")
    end

    -- Tracks MediaPoolItems already processed to identify first instances
    local processedMediaPoolItems = {} -- Key: mediaPoolItem, Value: true

    -- SINGLE counter for the sequential number based on timeline position
    local currentShotNumberValue = shotStep

    local modifiedCount = 0       -- Metadata was set/changed (on first instances)
    local duplicateMarkerCount = 0  -- Marker added for a duplicate instance
    local firstInstanceSkippedCount = 0 -- First instance skipped due to existing value (clearExisting=false)
    local firstInstanceMarkerCount = 0 -- Markers added to first instances (Green/Red for skipped)

    local skippedOutOfScope = 0   -- walked for numbering, but not written to

    print(string.format("\nProcessing %d timeline items...", #workList))
    if #appliedItems ~= #workList then
        print(string.format("  (writing to %d of them; the rest are walked only to keep the numbering aligned)", #appliedItems))
    end
    print(string.format("Using format: '%s', Starting number: %s", formatString, string.format(formatString, currentShotNumberValue)))

    for i, entry in ipairs(workList) do
        local itemData = entry.data
        local timelineItem = itemData.item
        local mediaPoolItem = timelineItem:GetMediaPoolItem()

        -- Calculate the sequential number string for THIS clip position
        local currentSequentialShotString = string.format(formatString, currentShotNumberValue)

        if mediaPoolItem then
            local clipName = mediaPoolItem:GetName() or "Unnamed Clip"
            local clipInfoStr = string.format("'%s' (T%d @ %d)", clipName, itemData.trackIndex, itemData.start)

            -- === Check if this is the first time seeing this source clip ===
            -- The bookkeeping happens whether or not this clip is written to, so a
            -- scoped run keeps the same notion of "first instance" as a full run.
            local isFirstInstance = not processedMediaPoolItems[mediaPoolItem]
            processedMediaPoolItems[mediaPoolItem] = true -- Mark as seen

            if not entry.apply then
                -- Walked only to keep the sequence aligned with a full-timeline run.
                skippedOutOfScope = skippedOutOfScope + 1
            elseif isFirstInstance then
                -- *** FIRST INSTANCE ***
                local currentShotMetadata = mediaPoolItem:GetMetadata("Shot")

                if clearExisting or not currentShotMetadata or currentShotMetadata == "" then
                    -- Assign current sequential number: Overwriting OR field was empty
                    if mediaPoolItem:SetMetadata("Shot", currentSequentialShotString) then
                        print(string.format("Set First Instance %s Shot to '%s'", clipInfoStr, currentSequentialShotString))
                        modifiedCount = modifiedCount + 1
                        -- Optional: Add "New" marker (Blue)
                        -- if AddMarker(timelineItem, currentSequentialShotString, "Blue", markerNote) then firstInstanceMarkerCount = firstInstanceMarkerCount + 1 end
                    else
                        print(string.format("WARNING: Failed to set first instance %s Shot to '%s'", clipInfoStr, currentSequentialShotString))
                        -- Optional: Add "Error" marker (Orange)
                        -- if AddMarker(timelineItem, currentSequentialShotString, "Orange", markerNote) then firstInstanceMarkerCount = firstInstanceMarkerCount + 1 end
                    end
                else
                    -- Preserve existing number on first instance
                    firstInstanceSkippedCount = firstInstanceSkippedCount + 1
                    local markerColor = "Orange" -- Default for skipped/preserved

                    if currentShotMetadata == currentSequentialShotString then
                        markerColor = "Green" -- Matches the sequential number it *would* have received
                        print(string.format("Skipped First Instance %s - Preserved correct Shot: '%s' (Added Green marker)", clipInfoStr, currentShotMetadata))
                    else
                        markerColor = "Red" -- Preserved, but *doesn't* match the sequence this time
                        print(string.format("Skipped First Instance %s - Preserved different Shot: '%s' (sequential #: '%s') (Added Red marker)", clipInfoStr, currentShotMetadata, currentSequentialShotString))
                    end
                    -- Add marker indicating status, showing preserved value
                    if AddMarker(timelineItem, currentShotMetadata .. " (Preserved)", markerColor, markerNote) then
                        firstInstanceMarkerCount = firstInstanceMarkerCount + 1
                    end
                end
            else
                -- *** DUPLICATE INSTANCE ***
                print(string.format("Duplicate Instance %s - Metadata unchanged. Adding marker with sequential number '%s'", clipInfoStr, currentSequentialShotString))
                duplicateMarkerCount = duplicateMarkerCount + 1

                -- ** Do NOT modify 'Shot' metadata **

                -- Add marker showing the sequential number this duplicate consumed
                if not AddMarker(timelineItem, currentSequentialShotString, "Cyan", markerNote .. " - Duplicate") then
                     print(string.format("  WARNING: Failed to add duplicate marker to %s", clipInfoStr))
                end
            end
        else
             print(string.format("Warning: Skipping item at T%d @ %d - Could not get Media Pool Item.", itemData.trackIndex, itemData.start))
             -- Still consumes a number in the sequence
        end

        -- ** Increment the sequential number counter for the NEXT clip **
        currentShotNumberValue = currentShotNumberValue + shotStep

    end -- End of loop through items

    print("\n--- Shot Numbering Summary ---")
    print(string.format("Scope: %s", scopeLabel))
    print(string.format("Items walked for numbering: %d", #workList))
    print(string.format("Clips written to: %d", #appliedItems))
    if skippedOutOfScope > 0 then
        print(string.format("  Walked but left untouched (outside selection): %d", skippedOutOfScope))
    end
    print(string.format("  'Shot' metadata set/overwritten (on first instances): %d", modifiedCount))
    print(string.format("  First instances skipped (metadata preserved): %d", firstInstanceSkippedCount))
    print(string.format("  Markers added for duplicate instances: %d", duplicateMarkerCount))
    print(string.format("  Markers added for skipped first instances: %d", firstInstanceMarkerCount))
    print(string.format("  Total markers added: %d", duplicateMarkerCount + firstInstanceMarkerCount))

    return true
end

-- Run the script
Main()
print("\n--- Script Finished ---")