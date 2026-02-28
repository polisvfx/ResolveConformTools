#!/usr/bin/env lua

-- Script to set sequential shot numbers as Clip Names on timeline items
-- Version: 1.0
-- Requires: DaVinci Resolve 20.2+ (uses TimelineItem:SetName API)
--
-- Uses TimelineItem:SetName() to rename each clip instance on the timeline.
-- Each timeline item gets its own unique sequential name, regardless of
-- whether the same source clip appears multiple times.
-- Original clip names can be preserved as a suffix.

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

-- Function to get all timeline items ordered by their position
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


-- Function to restore clip names from their Media Pool source
function RestoreOriginalClipNames()
    local items = GetOrderedTimelineItems()
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
    local config = {
        prefix = "SH_",
        padding = 4,
        increment = 10,
        includeOriginalName = false,
        separator = "_"
    }
    local result = nil

    local win = disp:AddWindow({
        WindowTitle = "Shot Numbering - Clip Name (v1.0)",
        ID = "ConfigWin",
        Geometry = { 100, 100, 450, 340 },
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
        result = config
        disp:ExitLoop()
    end

    function win.On.RestoreButton.Clicked(ev)
        print("\n--- Restoring Original Clip Names ---")
        local restored = RestoreOriginalClipNames()
        itm.RestoreButton.Text = string.format("Restored (%d clips)", restored)
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

    print("\n--- Starting Shot Numbering - Clip Name (v1.0) ---")
    print("Configuration:")
    print(string.format("  Prefix: '%s', Padding: %d, Increment: %d", prefix, padding, shotStep))
    print(string.format("  Include original name: %s, Separator: '%s'", includeOriginalName and "Yes" or "No", separator))

    local items = GetOrderedTimelineItems()
    if #items == 0 then
        print("\nNo video clips found on the timeline.")
        return false
    end

    local currentShotNumberValue = shotStep
    local renamedCount = 0
    local failedCount = 0

    -- Build preview of first clip name
    local previewNumber = string.format(formatString, currentShotNumberValue)
    local previewName = prefix .. previewNumber
    if includeOriginalName then
        previewName = previewName .. separator .. "(clipname)"
    end
    print(string.format("\nProcessing %d timeline items...", #items))
    print(string.format("Name pattern: '%s'", previewName))

    for i, itemData in ipairs(items) do
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
                newName = newName .. separator .. originalName
            end
        end

        local currentName = timelineItem:GetName() or "Unnamed"
        local clipInfoStr = string.format("'%s' (T%d @ %d)", currentName, itemData.trackIndex, itemData.start)

        if timelineItem:SetName(newName) then
            print(string.format("  Renamed %s -> '%s'", clipInfoStr, newName))
            renamedCount = renamedCount + 1
        else
            print(string.format("  FAILED to rename %s -> '%s'", clipInfoStr, newName))
            failedCount = failedCount + 1
        end

        currentShotNumberValue = currentShotNumberValue + shotStep
    end

    print("\n--- Shot Numbering Summary ---")
    print(string.format("Clips processed: %d", #items))
    print(string.format("  Successfully renamed: %d", renamedCount))
    if failedCount > 0 then
        print(string.format("  Failed: %d", failedCount))
    end

    return true
end

-- Run the script
Main()
print("\n--- Script Finished ---")
