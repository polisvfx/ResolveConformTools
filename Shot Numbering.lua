#!/usr/bin/env lua

-- Script to set Shot metadata field for clips in the timeline
-- Version: 1.4 (Strict Sequential Numbering, Duplicates Marked)
-- Assigns sequential shot numbers based strictly on timeline order.
-- 'Shot' metadata is set ONLY on the first instance of each source clip encountered.
-- Duplicate instances have their 'Shot' metadata UNCHANGED, but receive a marker
-- showing the sequential number they consumed in the timeline order.
-- Every clip consumes a number in the sequence.
-- Allows configuration of padding, increments, and overwriting existing values.

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

-- Function to get all timeline items ordered by their position (No changes needed)
function GetOrderedTimelineItems()
    local trackTypes = {"video"}
    local allItems = {}
    for _, trackType in ipairs(trackTypes) do
        local trackCount = timeline:GetTrackCount(trackType)
        for trackIndex = trackCount, 1, -1 do
            local trackItems = timeline:GetItemListInTrack(trackType, trackIndex)
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


-- Function to clear all markers with a specific note (No changes needed)
function ClearMarkersByNote(noteToClear)
    local items = GetOrderedTimelineItems()
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


-- Function to clear all Shot metadata (No changes needed)
function ClearAllShotMetadata()
    local items = GetOrderedTimelineItems()
    local clearedCount = 0
    print("Clearing all 'Shot' metadata values...")
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
    local config = { padding = 4, increment = 10, clearExisting = false }
    local result = nil
    local win = disp:AddWindow({
        WindowTitle = "Shot Number Configuration v1.4", ID = "ConfigWin", Geometry = { 100, 100, 420, 270 }, Spacing = 10, -- Wider/Taller
        ui:VGroup{ ID = "root", Weight = 1.0,
            ui:HGroup{ ui:Label{ Text = "Number Padding:", Weight = 0.3 }, ui:SpinBox{ ID = "PaddingInput", Value = config.padding, Minimum = 1, Maximum = 10, Weight = 0.7 } },
            ui:HGroup{ ui:Label{ Text = "Increment By:", Weight = 0.3 }, ui:SpinBox{ ID = "IncrementInput", Value = config.increment, Minimum = 1, Maximum = 1000, Weight = 0.7 } },
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
    function win.On.ConfigWin.Close(ev) disp:ExitLoop() end
    function win.On.CancelButton.Clicked(ev) result = nil; disp:ExitLoop() end
    function win.On.OKButton.Clicked(ev)
        config.padding = itm.PaddingInput.Value; config.increment = itm.IncrementInput.Value; config.clearExisting = itm.ClearCheckBox.Checked
        result = config; disp:ExitLoop()
    end
    function win.On.ClearMarkersButton.Clicked(ev)
        print("\n--- Clearing Markers via Button ---")
        local cleared = ClearMarkersByNote("Shotnumber Status")
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

    print("\n--- Starting Shot Numbering (v1.4 - Strict Sequential, Duplicates Marked) ---")
    print("Configuration:")
    print(string.format("  Padding: %d, Increment: %d, Overwrite Existing (First Instances): %s", padding, shotStep, clearExisting and "Yes" or "No"))

    if clearExisting then
        ClearAllShotMetadata()
        -- ClearMarkersByNote(markerNote) -- Optional
    else
        print("\nPreserving existing 'Shot' metadata where found.")
    end

    local items = GetOrderedTimelineItems()
    if #items == 0 then print("\nNo video clips found on the timeline."); return false end

    -- Tracks MediaPoolItems already processed to identify first instances
    local processedMediaPoolItems = {} -- Key: mediaPoolItem, Value: true

    -- SINGLE counter for the sequential number based on timeline position
    local currentShotNumberValue = shotStep

    local modifiedCount = 0       -- Metadata was set/changed (on first instances)
    local duplicateMarkerCount = 0  -- Marker added for a duplicate instance
    local firstInstanceSkippedCount = 0 -- First instance skipped due to existing value (clearExisting=false)
    local firstInstanceMarkerCount = 0 -- Markers added to first instances (Green/Red for skipped)

    print(string.format("\nProcessing %d timeline items...", #items))
    print(string.format("Using format: '%s', Starting number: %s", formatString, string.format(formatString, currentShotNumberValue)))

    for i, itemData in ipairs(items) do
        local timelineItem = itemData.item
        local mediaPoolItem = timelineItem:GetMediaPoolItem()

        -- Calculate the sequential number string for THIS clip position
        local currentSequentialShotString = string.format(formatString, currentShotNumberValue)

        if mediaPoolItem then
            local clipName = mediaPoolItem:GetName() or "Unnamed Clip"
            local clipInfoStr = string.format("'%s' (T%d @ %d)", clipName, itemData.trackIndex, itemData.start)

            -- === Check if this is the first time seeing this source clip ===
            if not processedMediaPoolItems[mediaPoolItem] then
                -- *** FIRST INSTANCE ***
                processedMediaPoolItems[mediaPoolItem] = true -- Mark as seen

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
    print(string.format("Clips processed: %d", #items))
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