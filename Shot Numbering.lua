#!/usr/bin/env lua

-- Script to set Shot metadata field for clips in the timeline
-- Allows configuration of padding, increments, and whether to clear existing Shot values
-- Places markers at 50% of clip duration for clips that already have Shot metadata

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

-- Function to get all timeline items ordered by their position
function GetOrderedTimelineItems()
    local trackTypes = {"video"}
    local allItems = {}
    
    -- Loop through track types
    for _, trackType in ipairs(trackTypes) do
        local trackCount = timeline:GetTrackCount(trackType)
        
        -- Process video tracks in reverse (higher tracks first)
        for trackIndex = trackCount, 1, -1 do
            local trackItems = timeline:GetItemListInTrack(trackType, trackIndex)
            
            -- Add track index and type information to each item
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
    
    -- Sort items by start frame first, then by track index (lower track index wins on tie)
    table.sort(allItems, function(a, b)
        if a.start == b.start then
            return a.trackIndex < b.trackIndex
        end
        return a.start < b.start
    end)
    
    return allItems
end

-- Function to add marker at 50% of clip length with specified color
function AddMarker(item, markerName, markerColor)
    -- Get source start frame and clip duration
    local sourceStartFrame = item:GetSourceStartFrame() 
    local clipDuration = item:GetDuration()
    
    -- Calculate 50% of the clip duration
    local offsetFrame = math.floor(clipDuration * 0.5)
    
    -- Calculate marker position by adding source start frame and offset
    local markerPosition = sourceStartFrame + offsetFrame
    
    -- Add the marker with specified color and "Shotnumber" as the note
    local success = item:AddMarker(markerPosition, markerColor, markerName, "Shotnumber", 1)
    
    if success then
        print(string.format("  Added %s marker '%s' at position %d", markerColor, markerName, markerPosition))
    else
        print(string.format("  Failed to add %s marker to clip", markerColor))
    end
    
    return success
end

-- Function to clear all markers with the "Shotnumber" note
function ClearShotNumberMarkers()
    local items = GetOrderedTimelineItems()
    local clearedCount = 0
    
    print("Clearing all Shot Number markers...")
    
    for _, itemData in ipairs(items) do
        local timelineItem = itemData.item
        
        -- Get all markers on this clip
        local markers = timelineItem:GetMarkers()
        
        -- Check if we got any markers
        if markers then
            local markersDeleted = false
            
            -- Loop through all markers and delete those with "Shotnumber" note
            for frameIdx, markerInfo in pairs(markers) do
                if markerInfo.note == "Shotnumber" then
                    timelineItem:DeleteMarkerAtFrame(frameIdx)
                    markersDeleted = true
                end
            end
            
            if markersDeleted then
                clearedCount = clearedCount + 1
            end
        end
    end
    
    print(string.format("Completed: Cleared Shot Number markers from %d clips", clearedCount))
    return clearedCount
end

-- Function to clear all Shot metadata
function ClearAllShotMetadata()
    local items = GetOrderedTimelineItems()
    local clearedCount = 0
    
    print("Clearing all Shot metadata values...")
    
    for _, itemData in ipairs(items) do
        local timelineItem = itemData.item
        local mediaPoolItem = timelineItem:GetMediaPoolItem()
        
        if mediaPoolItem then
            local metadata = mediaPoolItem:GetMetadata()
            local currentShot = metadata["Shot"]
            
            if currentShot ~= nil and currentShot ~= "" then
                mediaPoolItem:SetMetadata("Shot", "")
                clearedCount = clearedCount + 1
                print(string.format("  Cleared Shot value '%s' from clip '%s'", 
                    currentShot, mediaPoolItem:GetName()))
            end
        end
    end
    
    print(string.format("Completed: Cleared Shot metadata from %d clips", clearedCount))
    return clearedCount
end

-- Function to show configuration dialog
function ShowConfigDialog()
    -- Create a new dialog window using Fusion UI Manager
    local ui = fusion.UIManager
    local disp = bmd.UIDispatcher(ui)
    
    -- Default values
    local paddingValue = 4
    local incrementValue = 10
    local clearExistingValue = false
    
    local configWindow = disp:AddWindow({
        WindowTitle = "Shot Number Configuration",
        ID = "ConfigWin",
        Geometry = { 100, 100, 400, 200 },
        Spacing = 10,
        
        ui:VGroup{
            ID = "root",
            
            -- Padding setting
            ui:HGroup{
                ui:Label{ ID = "PaddingLabel", Text = "Number Padding:" },
                ui:SpinBox{ 
                    ID = "PaddingInput", 
                    Value = paddingValue, 
                    Minimum = 1, 
                    Maximum = 10,
                    Events = { ValueChanged = true }
                }
            },
            
            -- Increment setting
            ui:HGroup{
                ui:Label{ ID = "IncrementLabel", Text = "Increment By:" },
                ui:SpinBox{ 
                    ID = "IncrementInput", 
                    Value = incrementValue, 
                    Minimum = 1, 
                    Maximum = 100,
                    Events = { ValueChanged = true }
                }
            },
            
            -- Clear Shot values option
            ui:CheckBox{ 
                ID = "ClearCheckBox", 
                Text = "Overwrite Shot Numbers (clear existing values)", 
                Checked = clearExistingValue,
                Events = { Clicked = true }
            },
            
            -- Spacing
            ui:VGap(20),
            
            -- Buttons
            ui:HGroup{
                ui:Button{ ID = "CancelButton", Text = "Cancel" },
                ui:Button{ ID = "OKButton", Text = "OK" }
            }
        }
    })
    
    -- Configuration result to return
    local result = nil
    
    -- Event handlers for input changes
    function configWindow.On.PaddingInput.ValueChanged(ev)
        paddingValue = ev.Value
    end
    
    function configWindow.On.IncrementInput.ValueChanged(ev)
        incrementValue = ev.Value
    end
    
    function configWindow.On.ClearCheckBox.Clicked(ev)
        clearExistingValue = not clearExistingValue
        print("Checkbox clicked - new value:", clearExistingValue)
    end
    
    -- Function to handle the window closed event
    function configWindow.On.ConfigWin.Close(ev)
        disp:ExitLoop()
    end
    
    -- Function to handle the Cancel button
    function configWindow.On.CancelButton.Clicked(ev)
        result = nil
        configWindow:Hide()
        disp:ExitLoop()
    end
    
    -- Function to handle the OK button
    function configWindow.On.OKButton.Clicked(ev)
        -- Debug print for checkbox state
        print("Checkbox state before storing result:", clearExistingValue)
        
        -- Store result using the current values
        result = {
            padding = paddingValue,
            increment = incrementValue,
            clearExisting = clearExistingValue
        }
        
        -- Debug print for result
        print("Stored in result:", result.clearExisting)
        
        -- Hide the window and exit loop
        configWindow:Hide()
        disp:ExitLoop()
    end
    
    -- Show the window and run the event loop
    configWindow:Show()
    disp:RunLoop()
    
    -- Return the configuration values
    return result
end

-- Main script execution
function Main()
    -- Show configuration dialog to get user settings
    local config = ShowConfigDialog()
    
    -- If user canceled, exit
    if config == nil then
        print("Operation canceled by user")
        return false
    end
    
    -- Apply configuration
    local padding = config.padding
    local shotStep = config.increment
    local clearExisting = config.clearExisting
    
    -- Debug config values
    print("\nConfiguration:")
    print(string.format("  Padding: %d", padding))
    print(string.format("  Increment: %d", shotStep))
    print(string.format("  Clear Existing Shot Metadata: %s", clearExisting and "Yes" or "No"))
    
    -- Set up shot number format string based on padding
    local formatString = "%0" .. padding .. "d"
    
    -- First, clear all Shot Number markers
    ClearShotNumberMarkers()
    
    -- Clear Shot metadata if option is enabled
    if clearExisting then
        print("\nClearing all existing Shot metadata as requested...")
        ClearAllShotMetadata()
    else
        print("\nPreserving existing Shot metadata as requested")
    end
    
    -- Now process clips to set new Shot values
    local items = GetOrderedTimelineItems()
    local shotNumber = shotStep -- Start with the same value as increment (e.g., 5, 10, etc.)
    local modifiedCount = 0
    local skippedCount = 0
    
    print("\nProcessing " .. #items .. " timeline items for Shot assignment...")
    print(string.format("Using padding: %d, increment: %d", padding, shotStep))
    print(string.format("Starting number: %s", string.format(formatString, shotNumber)))
    
    for i, itemData in ipairs(items) do
        local timelineItem = itemData.item
        local mediaPoolItem = timelineItem:GetMediaPoolItem()
        
        if mediaPoolItem then
            -- Check if Shot metadata is already set
            local metadata = mediaPoolItem:GetMetadata()
            local currentShot = metadata["Shot"]
            
            if currentShot == nil or currentShot == "" then
                -- Format shot number with configured padding
                local shotValue = string.format(formatString, shotNumber)
                mediaPoolItem:SetMetadata("Shot", shotValue)
                modifiedCount = modifiedCount + 1
                print(string.format("Set %s for clip '%s' (timeline pos: %d, track: %s%d)", 
                    shotValue, mediaPoolItem:GetName(), timelineItem:GetStart(), 
                    itemData.trackType, itemData.trackIndex))
            else
                -- Shot already has value, determine marker color based on if values match
                local markerShotValue = string.format(formatString, shotNumber)
                local markerColor = "Red" -- Default color
                
                -- If the current Shot value equals what would be assigned, use green marker
                if currentShot == markerShotValue then
                    markerColor = "Green"
                    print(string.format("Skipped clip '%s' - already has correct Shot value: '%s' (green marker added)", 
                        mediaPoolItem:GetName(), currentShot))
                else
                    print(string.format("Skipped clip '%s' - has different Shot value: '%s' vs new '%s' (red marker added)", 
                        mediaPoolItem:GetName(), currentShot, markerShotValue))
                end
                
                -- Add the marker with appropriate color
                AddMarker(timelineItem, markerShotValue, markerColor)
                skippedCount = skippedCount + 1
            end
        else
            print("Warning: Couldn't find media pool item for a timeline item")
        end
        
        -- Increment shot number for next item
        shotNumber = shotNumber + shotStep
    end
    
    print(string.format("\nCompleted: %d clips modified, %d clips skipped (already had Shot metadata)", 
        modifiedCount, skippedCount))
    return true
end

-- Run the script
Main()