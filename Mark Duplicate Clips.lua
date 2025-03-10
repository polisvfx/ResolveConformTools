--[[
Timeline Duplicate Clip Marker - DaVinci Resolve Script
This script scans the current timeline for duplicate clips and 
marks them with colored markers to easily identify them.
Each set of duplicates gets its own color for easy identification.
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

-- Function to determine if two clips are duplicates
function areClipsDuplicates(clip1, clip2)
    -- Check if they reference the same media
    local path1 = clip1.mediaPoolItem:GetClipProperty("File Path")
    local path2 = clip2.mediaPoolItem:GetClipProperty("File Path")
    
    -- Compare file paths and ensure they're not the same instance
    if path1 and path2 and path1 == path2 then
        -- Ensure they're not the same clip instance
        if clip1.startFrame ~= clip2.startFrame or 
           clip1.trackIndex ~= clip2.trackIndex then
            return true
        end
    end
    
    -- Fallback: Check if names match (less reliable)
    if clip1.name == clip2.name and clip1.name ~= "" then
        if clip1.startFrame ~= clip2.startFrame or 
           clip1.trackIndex ~= clip2.trackIndex then
            return true
        end
    end
    
    return false
end

-- Function to get all clips from the timeline
function getAllClips()
    local clipList = {}
    if timeline then
        local tracksCount = timeline:GetTrackCount("video")
        print("Found " .. tracksCount .. " video tracks")
        
        for trackIndex = 1, tracksCount do
            local clips = timeline:GetItemListInTrack("video", trackIndex)
            if clips then
                print("Track " .. trackIndex .. ": Found " .. #clips .. " clips")
                
                for clipIndex, clip in ipairs(clips) do
                    local mediaPoolItem = clip:GetMediaPoolItem()
                    if mediaPoolItem then
                        local filePath = mediaPoolItem:GetClipProperty("File Path")
                        print("Clip " .. clipIndex .. " on track " .. trackIndex .. ": " .. clip:GetName())
                        print("  File Path: " .. (filePath or "N/A"))
                        
                        table.insert(clipList, {
                            clip = clip,
                            mediaPoolItem = mediaPoolItem,
                            startFrame = clip:GetStart(),
                            name = clip:GetName(),
                            trackIndex = trackIndex
                        })
                    else
                        print("Clip " .. clipIndex .. " on track " .. trackIndex .. " has no media pool item")
                    end
                end
            else
                print("No clips found in track " .. trackIndex)
            end
        end
    end
    
    print("Total clips found: " .. #clipList)
    return clipList
end

-- Main function to find and mark duplicates
function findAndMarkDuplicates()
    -- Check if we have an active timeline
    if not timeline then
        print("No timeline is open. Please open a timeline.")
        return
    end
    
    -- Get all clips from the timeline
    local clips = getAllClips()
    if #clips == 0 then
        print("No clips found in the timeline.")
        return
    end
    
    print("Scanning for duplicates among " .. #clips .. " clips...")
    
    -- Find duplicates
    local duplicateSets = {}
    local processedClips = {}
    
    for i = 1, #clips do
        if not processedClips[i] then
            local currentSet = {clips[i]}
            processedClips[i] = true
            
            for j = i + 1, #clips do
                if not processedClips[j] then
                    print("Comparing clip " .. i .. " (" .. clips[i].name .. ") with clip " .. 
                          j .. " (" .. clips[j].name .. ")")
                          
                    if areClipsDuplicates(clips[i], clips[j]) then
                        print("  MATCH FOUND!")
                        table.insert(currentSet, clips[j])
                        processedClips[j] = true
                    end
                end
            end
            
            if #currentSet > 1 then
                table.insert(duplicateSets, currentSet)
                print("Found duplicate set with " .. #currentSet .. " clips")
            end
        end
    end
    
    -- Mark duplicates with colors
    local markerCount = 0
    local successCount = 0
    
    for setIndex, duplicateSet in ipairs(duplicateSets) do
        local colorIndex = (setIndex - 1) % #colors + 1
        local color = colors[colorIndex]
        
        print("Marking duplicate set " .. setIndex .. " with " .. #duplicateSet .. " clips (Color: " .. color .. ")")
        
        for i, clip in ipairs(duplicateSet) do
            local markerText = "Dup Set #" .. setIndex .. " (" .. i .. "/" .. #duplicateSet .. ")"
            
            -- Add marker to the TimelineItem at 25% of the clip duration
            local sourceStartFrame = clip.clip:GetSourceStartFrame()
            local clipDuration = clip.clip:GetDuration()
            local offsetFrame = math.floor(clipDuration * 0.25)  -- 25% of clip duration
            local markerPosition = sourceStartFrame + offsetFrame
            local success = clip.clip:AddMarker(markerPosition, color, markerText, "Duplicate clip detected", 1, "")
            
            markerCount = markerCount + 1
            
            if success then
                successCount = successCount + 1
                print("  Added marker to clip: " .. clip.name)
            else
                print("  Failed to add marker to clip: " .. clip.name)
            end
        end
    end
    
    -- Show results
    if #duplicateSets > 0 then
        print("Found " .. #duplicateSets .. " sets of duplicates with " .. markerCount .. " total clips.")
        print("Successfully added " .. successCount .. " markers out of " .. markerCount .. " attempts.")
        
        if successCount == 0 then
            print("WARNING: Unable to add markers. This could be due to timeline permissions.")
            print("Duplicate sets were found as follows:")
            for i, set in ipairs(duplicateSets) do
                local clipNames = {}
                for j, clip in ipairs(set) do
                    table.insert(clipNames, clip.name)
                end
                print("Set #" .. i .. ": " .. table.concat(clipNames, ", "))
            end
        end
    else
        print("No duplicate clips were found.")
        print("If you believe there are duplicates, check if:")
        print("1. The duplicates have the same source file path or name")
        print("2. The clips are on video tracks (audio duplicates are not detected)")
    end
end

-- Run the script
findAndMarkDuplicates()