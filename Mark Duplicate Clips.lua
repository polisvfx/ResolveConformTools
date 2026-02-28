--[[
Timeline Duplicate Clip Marker - DaVinci Resolve Script
Version: 1.2 (Corrected Marker Placement)

This script scans the current timeline for duplicate clips (including image sequences)
and marks them with colored markers to easily identify them.
Each set of duplicates gets its own color for easy identification.
Markers are placed at 25% of the clip's duration *within its visible range on the timeline*.
]]

-- Import the required modules
local resolve = Resolve()
local projectManager = resolve:GetProjectManager()
local project = projectManager:GetCurrentProject()
local mediaPool = project:GetMediaPool()
local timeline = project:GetCurrentTimeline()

-- Color palette for markers (using the documented marker colors)
local colors = {
    "Red", "Yellow", "Green", "Cyan", "Blue", "Purple", "Pink", "Fuchsia"
}

-- Function to determine if two clips are duplicates (Same as previous version 1.1)
function areClipsDuplicates(clipInfo1, clipInfo2)
    if not clipInfo1.mediaPoolItem or not clipInfo2.mediaPoolItem then
        -- print("  Skipping comparison: One or both clips lack MediaPoolItem.") -- Less verbose
        return false
    end
    if clipInfo1.mediaPoolItem == clipInfo2.mediaPoolItem then
        if clipInfo1.clip:GetStart() ~= clipInfo2.clip:GetStart() or
           clipInfo1.trackIndex ~= clipInfo2.trackIndex then
             -- print("  Match found based on MediaPoolItem reference.") -- Less verbose
            return true
        end
    end
    -- Fallback check (less likely needed but safe)
    local mediaId1 = clipInfo1.mediaPoolItem:GetMediaId()
    local mediaId2 = clipInfo2.mediaPoolItem:GetMediaId()
    if mediaId1 and mediaId2 and mediaId1 == mediaId2 and mediaId1 ~= "" then
        if clipInfo1.clip:GetStart() ~= clipInfo2.clip:GetStart() or
           clipInfo1.trackIndex ~= clipInfo2.trackIndex then
            -- print("  Match found based on MediaID.") -- Less verbose
            return true
        end
    end
    return false
end


-- Function to get all clips from the timeline (Same as previous version 1.1)
function getAllClips()
    local clipList = {}
    if timeline then
        local tracksCount = timeline:GetTrackCount("video")
        print("Scanning " .. tracksCount .. " video tracks...")

        for trackIndex = 1, tracksCount do
            local clipsInTrack = timeline:GetItemListInTrack("video", trackIndex)
            if clipsInTrack then
                -- print("Track " .. trackIndex .. ": Found " .. #clipsInTrack .. " clips") -- Less verbose

                for clipIndex, clip in ipairs(clipsInTrack) do
                    local mediaPoolItem = clip:GetMediaPoolItem()
                    if mediaPoolItem then
                        local clipName = clip:GetName()
                        table.insert(clipList, {
                            clip = clip,
                            mediaPoolItem = mediaPoolItem,
                            startFrame = clip:GetStart(), -- Timeline start frame
                            name = clipName,
                            trackIndex = trackIndex
                        })
                    else
                         -- Print only if verbose debugging is needed
                         -- print("  Clip " .. clipIndex .. " on track " .. trackIndex .. " ('" .. clip:GetName() .."') has no associated Media Pool item. Skipping.")
                    end
                end
            end
        end
    end

    print("Total valid clips collected for analysis: " .. #clipList)
    return clipList
end


-- Main function to find and mark duplicates
function findAndMarkDuplicates()
    if not timeline then
        print("ERROR: No timeline is open. Please open a timeline.")
        return
    end

    local clips = getAllClips()
    if #clips < 2 then
        print("Not enough clips found in the timeline to search for duplicates.")
        return
    end

    print("Scanning for duplicates among " .. #clips .. " clips...")

    local duplicateSets = {}
    local processedIndices = {}

    for i = 1, #clips do
        if not processedIndices[i] then
            local currentSet = nil
            for j = i + 1, #clips do
                if not processedIndices[j] then
                    if areClipsDuplicates(clips[i], clips[j]) then
                        if not currentSet then
                            currentSet = { clips[i] }
                            processedIndices[i] = true
                        end
                        table.insert(currentSet, clips[j])
                        processedIndices[j] = true
                    end
                end
            end
            if currentSet then
                table.insert(duplicateSets, currentSet)
                -- print("Found duplicate set with " .. #currentSet .. " clips (Source: '" .. clips[i].name .."')") -- Less verbose
            end
        end
    end

    -- Mark duplicates with colors
    local totalMarkersAttempted = 0
    local totalMarkersSucceeded = 0

    if #duplicateSets > 0 then
        print("\nMarking " .. #duplicateSets .. " duplicate sets...")
        for setIndex, duplicateSet in ipairs(duplicateSets) do
            local colorIndex = (setIndex - 1) % #colors + 1
            local color = colors[colorIndex]

            print(string.format("Marking Set #%d (%d clips) with Color: %s", setIndex, #duplicateSet, color))

            for i, clipInfo in ipairs(duplicateSet) do
                local markerText = "Dup Set #" .. setIndex .. " (" .. i .. "/" .. #duplicateSet .. ")"
                local markerNote = "Duplicate of source: " .. clipInfo.name

                -- *** CORRECTED MARKER PLACEMENT LOGIC ***
                local timelineDuration = clipInfo.clip:GetDuration()
                local sourceStartFrame = clipInfo.clip:GetLeftOffset() -- Start frame within the source media

                if timelineDuration and sourceStartFrame and timelineDuration > 0 then
                    -- Calculate the offset *within the timeline duration*
                    local offsetInTimeline = math.floor(timelineDuration * 0.25)

                    -- Calculate the target frame *relative to the source media start*
                    local markerFrameId = sourceStartFrame + offsetInTimeline

                    -- Add the marker using the calculated source frame ID
                    -- AddMarker(frameId, colorName, name, note, duration)
                    local success = clipInfo.clip:AddMarker(markerFrameId, color, markerText, markerNote, 1)

                    totalMarkersAttempted = totalMarkersAttempted + 1

                    if success then
                        totalMarkersSucceeded = totalMarkersSucceeded + 1
                        print(string.format("  [%d/%d] Added marker to '%s' on T%d @ timeline %d (Source Frame: %d)",
                                            i, #duplicateSet, clipInfo.name, clipInfo.trackIndex, clipInfo.clip:GetStart(), markerFrameId))
                    else
                        print(string.format("  [%d/%d] FAILED to add marker to '%s' on T%d @ timeline %d (Target Source Frame: %d)",
                                            i, #duplicateSet, clipInfo.name, clipInfo.trackIndex, clipInfo.clip:GetStart(), markerFrameId))
                    end
                elseif not timelineDuration or timelineDuration <= 0 then
                     print(string.format("  [%d/%d] Skipped marker for '%s' on T%d (0 or invalid duration: %s)",
                                        i, #duplicateSet, clipInfo.name, clipInfo.trackIndex, tostring(timelineDuration)))
                elseif not sourceStartFrame then
                     print(string.format("  [%d/%d] Skipped marker for '%s' on T%d (Could not get source start frame)",
                                        i, #duplicateSet, clipInfo.name, clipInfo.trackIndex))
                end
            end
        end

        -- Show final results
        print("\n--- Summary ---")
        print("Found " .. #duplicateSets .. " sets of duplicate clips.")
        print("Attempted to add " .. totalMarkersAttempted .. " markers.")
        print("Successfully added " .. totalMarkersSucceeded .. " markers.")

        if totalMarkersSucceeded < totalMarkersAttempted then
            print("WARNING: Some markers could not be added. This might indicate issues like locked tracks, markers outside the valid source frame range, or other timeline problems.")
        end
         if totalMarkersSucceeded == 0 and totalMarkersAttempted > 0 then
             print("ERROR: Failed to add any markers. Please check track locks, script permissions, and ensure clips have valid source ranges.")
         end

    else
        print("\n--- Summary ---")
        print("No duplicate clips were found in the timeline.")
        print("(Checked based on underlying Media Pool source items)")
    end
end

-- Run the main function
print("--- Starting Timeline Duplicate Clip Marker Script (v1.2) ---")
findAndMarkDuplicates()
print("--- Script Finished ---")