--[[
Timeline Generator with Duplicate Marker
This script creates a master timeline from selected timelines and optionally marks duplicate clips.
]]--

function print_table(t, indentation)
    if indentation == nil then
        indentation = 0
    end
    local outer_prefix = string.rep("    ", indentation)
    local inner_prefix = string.rep("    ", indentation + 1)
    print(outer_prefix, "{")
    for k, v in pairs(t) do
        if type(v) == "table" then
            print(inner_prefix, k, ": ")
            print_table(v, indentation + 1)
        elseif type(v) == "string" then
            print(inner_prefix, k, string.format([[: "%s"]], v))
        else
            print(inner_prefix, k, ": ", v)
        end
    end
    print(outer_prefix, "}")
end

function sort_clip_infos(clip_infos)
    -- In place sort by startFrame
    table.sort(clip_infos, function(a, b)
        return a.startFrame < b.startFrame
    end)
end

function sort_clip_infos_by_source_name(clip_infos)
    -- In place sort by source name
    table.sort(clip_infos, function(a, b)
        return a.mediaPoolItem:GetName() < b.mediaPoolItem:GetName()
    end)
end

function sort_clip_infos_by_reel_name(clip_infos)
    -- In place sort by reel name metadata
    table.sort(clip_infos, function(a, b)
        local reel_name_a = a.mediaPoolItem:GetClipProperty("Reel Name") or ""
        local reel_name_b = b.mediaPoolItem:GetClipProperty("Reel Name") or ""
        return reel_name_a < reel_name_b
    end)
end

function sort_clip_infos_by_timeline_inpoint(clip_infos)
    -- In place sort by timeline inpoint (requires timelineInpoint to be set)
    table.sort(clip_infos, function(a, b)
        return a.timelineInpoint < b.timelineInpoint
    end)
end

function check_clip_infos_overlap(clip_info_a, clip_info_b, connection_threshold)
    -- Handle normal case
    if clip_info_a.startFrame <= clip_info_a.endFrame and
       clip_info_b.startFrame <= clip_info_b.endFrame then
        return not (clip_info_a.endFrame < clip_info_b.startFrame - connection_threshold or 
                   clip_info_b.endFrame < clip_info_a.startFrame - connection_threshold)
    -- Handle case where one or both clips have reversed frame order
    else
        -- Normalize the start/end frames for comparison
        local start_a = math.min(clip_info_a.startFrame, clip_info_a.endFrame)
        local end_a = math.max(clip_info_a.startFrame, clip_info_a.endFrame)
        local start_b = math.min(clip_info_b.startFrame, clip_info_b.endFrame)
        local end_b = math.max(clip_info_b.startFrame, clip_info_b.endFrame)
        
        return not (end_a < start_b - connection_threshold or end_b < start_a - connection_threshold)
    end
end

function merge_clip_infos(clip_info_a, clip_info_b)
    -- Return new clipinfo that spans clip_info_a and clip_info_b
    assert(clip_info_a.mediaPoolItem == clip_info_b.mediaPoolItem,
        "merge_clip_infos: Cannot merge clip infos from different media pool items")
    
    -- Determine if either clip has reversed start/end frames
    local a_reversed = clip_info_a.startFrame > clip_info_a.endFrame
    local b_reversed = clip_info_b.startFrame > clip_info_b.endFrame
    
    -- For comparison and merging, normalize the frame ranges
    local a_min = a_reversed and clip_info_a.endFrame or clip_info_a.startFrame
    local a_max = a_reversed and clip_info_a.startFrame or clip_info_a.endFrame
    local b_min = b_reversed and clip_info_b.endFrame or clip_info_b.startFrame
    local b_max = b_reversed and clip_info_b.startFrame or clip_info_b.endFrame
    
    -- Create the merged clip with the normalized frame range
    local merged_min = math.min(a_min, b_min)
    local merged_max = math.max(a_max, b_max)
    
    -- Debug info
    print("Merging clips with frames:")
    print("  A: " .. clip_info_a.startFrame .. " to " .. clip_info_a.endFrame .. 
          (a_reversed and " (reversed)" or ""))
    print("  B: " .. clip_info_b.startFrame .. " to " .. clip_info_b.endFrame .. 
          (b_reversed and " (reversed)" or ""))
    print("  Merged: " .. merged_min .. " to " .. merged_max)
    
    -- Keep track of if either of the original clips was reversed
    local is_reversed = a_reversed or b_reversed
    
    -- Preserve retime information from either clip
    local is_retimed = clip_info_a.isRetimed or clip_info_b.isRetimed
    local retimePercentage = clip_info_a.retimePercentage or clip_info_b.retimePercentage
    
    return {
        mediaPoolItem = clip_info_a.mediaPoolItem,
        startFrame = merged_min,
        endFrame = merged_max,
        timelineInpoint = clip_info_a.timelineInpoint, -- Keep the first clip's inpoint for sorting
        isReversed = is_reversed,
        isRetimed = is_retimed,
        retimePercentage = retimePercentage
    }
end

function merge_clip_infos_if_close(clip_info_a, clip_info_b, connection_threshold)
    assert(clip_info_a.mediaPoolItem == clip_info_b.mediaPoolItem,
        "merge_clip_infos_if_close: Cannot merge clip infos from different media pool items")
    if check_clip_infos_overlap(clip_info_a, clip_info_b, connection_threshold) then
        return merge_clip_infos(clip_info_a, clip_info_b)
    end
    return nil
end

function merge_clip_infos_if_close_all(clip_infos, connection_threshold)
    -- Merge clip_infos that are close together
    -- Always sort by startFrame for merging process
    sort_clip_infos(clip_infos)
    local i = 1
    while i <= #clip_infos do
        local j = i + 1
        local merged = false
        while j <= #clip_infos do
            local merged_clip_info = merge_clip_infos_if_close(clip_infos[i], clip_infos[j], connection_threshold)
            if merged_clip_info ~= nil then
                print("Merging clips ", clip_infos[i].mediaPoolItem:GetName(), " and ",
                    clip_infos[j].mediaPoolItem:GetName())
                clip_infos[i] = merged_clip_info
                table.remove(clip_infos, j)
                merged = true
            else
                j = j + 1
            end
        end
        if not merged then
            i = i + 1
        end
    end
    return clip_infos
end

-- Function to remove all audio tracks from a timeline
function removeAllAudioTracks(timeline)
    if timeline == nil then
        print("Error: No timeline provided.")
        return
    end
    
    print("Removing audio tracks from timeline...")
    
    -- Use pcall to handle potential errors
    local status, err = pcall(function()
        -- Get current track count
        local trackCount = timeline:GetTrackCount("audio")
        print("Found " .. trackCount .. " audio tracks")
        
        -- Delete all clips from each track
        for i = 1, trackCount do
            local items = timeline:GetItemListInTrack("audio", i)
            if items then
                print("Deleting items from track " .. i)
                timeline:DeleteClips(items)
            end
        end
        
        -- Delete all tracks except track 1
        for i = trackCount, 2, -1 do
            timeline:DeleteTrack("audio", i)
            print("Deleted track " .. i)
        end
    end)
    
    -- Print result
    if status then
        print("Successfully cleared all audio tracks.")
    else
        print("Error: An unexpected error occurred: " .. tostring(err))
    end
end

-- DUPLICATE MARKER FUNCTIONS
-- Function to determine if two clips are duplicates
function areClipsDuplicates(clip1, clip2)
    -- Check if they reference the same media
    local path1 = clip1.mediaPoolItem:GetClipProperty("File Path")
    local path2 = clip2.mediaPoolItem:GetClipProperty("File Path")
    
    -- Fix for false positive matches - require both path match AND same name
    -- Compare file paths and ensure they're not the same instance
    if path1 and path2 and path1 == path2 then
        local name1 = clip1.mediaPoolItem:GetName()
        local name2 = clip2.mediaPoolItem:GetName()
        
        -- Names must also match to be considered duplicates
        if name1 == name2 then
            -- Ensure they're not the same clip instance on the timeline
            if clip1.startFrame ~= clip2.startFrame or 
               clip1.trackIndex ~= clip2.trackIndex then
                return true
            end
        else
            -- Different names means different clips, even if path is the same
            return false
        end
    end
    
    -- Return false for all other cases
    return false
end

-- Function to get all clips from the timeline
function getAllClips(timeline)
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
function findAndMarkDuplicates(timeline)
    -- Check if we have an active timeline
    if not timeline then
        print("No timeline is open. Please open a timeline.")
        return
    end
    
    -- Get all clips from the timeline
    local clips = getAllClips(timeline)
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
    
    -- Color palette for markers (using the documented marker colors)
    local colors = {
        "Yellow", "Green", "Cyan", "Blue", "Purple", "Pink", "Fuchsia", "Lavender", "Rose", "Cocoa", "Sand", "Sky", "Mint", "Lemon"
    }
    
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
-- END DUPLICATE MARKER FUNCTIONS

-- RETIME DETECTION FUNCTION
-- Helper function to check retiming properties
function checkRetimeProperties(clip)
    -- Check for retiming property
    local speed = nil
    local success = pcall(function() speed = tonumber(clip.clip:GetClipProperty("Speed")) end)
    
    -- If speed is not 100% (normal speed), it's retimed
    if success and speed and speed ~= 100.0 then
        print("Detected retimed clip via property: " .. clip.name .. " with speed: " .. speed .. "%")
        clip.retimePercentage = speed -- Store for marker text
        return true
    end
    
    -- Check if any other retiming attributes are present
    local retiming_attributes = {
        "Retime Process", -- Check for alternative retime processes
        "Motion Estimation", -- Motion estimation settings
        "Frame Interpolation", -- Optical flow, nearest, etc.
        "Retime Curve" -- Custom speed curve
    }
    
    for _, attr in ipairs(retiming_attributes) do
        local value = nil
        local attr_success = pcall(function() value = clip.clip:GetClipProperty(attr) end)
        if attr_success and value and value ~= "" and value ~= "None" then
            print("Detected retimed clip via property: " .. clip.name .. " with " .. attr .. ": " .. value)
            return true
        end
    end
    
    return false
end

function isClipRetimed(clip)
    -- Check if clip and clip.clip are valid
    if not clip or not clip.clip then
        print("Warning: Invalid clip object in isClipRetimed")
        return false
    end
    
    -- Minimum difference thresholds (same as in Clipmismatch.txt)
    local MIN_FRAME_DIFF = 3
    local MIN_PERCENT_DIFF = 3.0
    local retimePercentage = 100.0
    
    -- Safely get durations using pcall
    local timelineDuration, sourceDuration = 0, 0
    local success = pcall(function()
        timelineDuration = clip.clip:GetEnd() - clip.clip:GetStart()
        sourceDuration = clip.clip:GetSourceEndFrame() - clip.clip:GetSourceStartFrame()
    end)
    
    if not success or sourceDuration == 0 then
        print("Warning: Could not calculate durations for clip: " .. clip.name)
        -- Fall back to checking properties
        return checkRetimeProperties(clip)
    end
    
    -- Calculate differences
    local frameDiff = math.abs(timelineDuration - sourceDuration)
    retimePercentage = (timelineDuration / sourceDuration) * 100
    local percentDiff = math.abs(retimePercentage - 100)
    
    -- Check if significant mismatch exists
    if frameDiff > MIN_FRAME_DIFF and percentDiff > MIN_PERCENT_DIFF then
        print("Detected retimed clip: " .. clip.name .. " with speed: " .. string.format("%.1f", retimePercentage) .. "%")
        clip.retimePercentage = retimePercentage -- Store for marker text
        return true
    end
    
    -- Fall back to property checks if no duration mismatch found
    return checkRetimeProperties(clip)
end

-- MAIN SCRIPT EXECUTION
function main()
    -- Draw window to get user parameters.
    local ui = fu.UIManager
    local disp = bmd.UIDispatcher(ui)
    local width, height = 500, 380

    local is_windows = package.config:sub(1, 1) ~= "/"

    win = disp:AddWindow({
        ID = "MyWin",
        WindowTitle = "Generate All Clips Timeline",
        Geometry = {100, 100, width, height},
        Spacing = 10,
        ui:VGroup{
            ID = "root",
            ui:HGroup{
                ID = "dst",
                ui:Label{
                    ID = "DstLabel",
                    Text = "New Timeline Name"
                },
                ui:TextEdit{
                    ID = "DstTimelineName",
                    Text = "",
                    PlaceholderText = "Master Timeline"
                }
            },
            ui:HGroup{
                ui:Label{
                    ID = "selectionMethodLabel",
                    Text = "Select Timelines By:"
                }, 
                ui:ComboBox{
                    ID = "selectionMethod",
                    Text = "Current Selection"
                }
            },
            ui:HGroup{
                ui:Label{
                    ID = "sortingMethodLabel",
                    Text = "Sort Clips By:"
                }, 
                ui:ComboBox{
                    ID = "sortingMethod",
                    Text = "Source Name"
                }
            },
            ui:HGroup{
                ui:Label{
                    ID = "ConnectionThresholdLabel",
                    Text = "Connection Threshold (Frames)"
                }, 
                ui:TextEdit{
                    ID = "ConnectionThreshold",
                    Text = "25",
                    PlaceholderText = "25"
                }
            },
            ui:CheckBox{
                ID = "includeDisabledItems",
                Text = "Include Disabled Clips"
            },
            ui:CheckBox{
                ID = "videoOnly",
                Text = "Video Only (No Audio)",
				Checked = true
            },
            ui:CheckBox{
                ID = "markDuplicates",
                Text = "Mark Duplicate Clips with Colored Markers",
				Checked = true
            },
			ui:CheckBox{
				ID = "markRetimedClips",
				Text = "Mark Retimed Clips with Red Markers",
				Checked = true
            },
            ui:HGroup{
                ID = "buttons",
                ui:Button{
                    ID = "cancelButton",
                    Text = "Cancel"
                },
                ui:Button{
                    ID = "goButton",
                    Text = "Go"
                }
            }
        }
    })

    run_export = false

    -- The window was closed
    function win.On.MyWin.Close(ev)
        disp:ExitLoop()
        run_export = false
    end

    function win.On.cancelButton.Clicked(ev)
        print("Cancel Clicked")
        disp:ExitLoop()
        run_export = false
    end

    function win.On.goButton.Clicked(ev)
        print("Go Clicked")
        disp:ExitLoop()
        run_export = true
    end

    -- Add your GUI element based event functions here:
    itm = win:GetItems()
    itm.selectionMethod:AddItem('Current Selection')
    itm.selectionMethod:AddItem('Current Bin')

    -- Add sorting method options
    itm.sortingMethod:AddItem('Source Name')
    itm.sortingMethod:AddItem('Source Inpoint')
    itm.sortingMethod:AddItem('Inpoint on Timeline')
    itm.sortingMethod:AddItem('Reel Name')
    itm.sortingMethod:AddItem('None')

    win:Show()
    disp:RunLoop()
    win:Hide()

    if run_export then
        assert(itm.DstTimelineName.PlainText ~= nil and itm.DstTimelineName.PlainText ~= "",
            "Found empty New Timeline Name! Refusing to run")
        local connection_threshold = tonumber(itm.ConnectionThreshold.PlainText)
        dst_timeline_name = itm.DstTimelineName.PlainText
        allow_disabled_clips = itm.includeDisabledItems.Checked
        video_only = itm.videoOnly.Checked
        sorting_method = itm.sortingMethod.CurrentText
        mark_duplicates = itm.markDuplicates.Checked
		mark_retimed_clips = itm.markRetimedClips.Checked

        -- Get timelines
        resolve = Resolve()
        projectManager = resolve:GetProjectManager()
        project = projectManager:GetCurrentProject()
        media_pool = project:GetMediaPool()
        num_timelines = project:GetTimelineCount()
        selected_bin = media_pool:GetCurrentFolder()

        -- Initialize table to store clips
        local clips = {}

        -- Mapping of timeline name to timeline object
        project_timelines = {}
        for timeline_idx = 1, num_timelines do
            runner_timeline = project:GetTimelineByIndex(timeline_idx)
            project_timelines[runner_timeline:GetName()] = runner_timeline
        end

        -- Safely get selected clips
        local selected_clips = {}
        if itm.selectionMethod.CurrentText == "Current Bin" then
            if selected_bin then
                local bin_clips = selected_bin:GetClipList()
                if bin_clips then
                    selected_clips = bin_clips
                end
            end
        elseif itm.selectionMethod.CurrentText == "Current Selection" then
            local sel_clips = media_pool:GetSelectedClips()
            if sel_clips then
                selected_clips = sel_clips
            end
        else
            print("Unknown selection method.")
            return
        end

        -- Check if we have any clips to process
        local has_clips = false
        for _, _ in pairs(selected_clips) do
            has_clips = true
            break
        end

        if not has_clips then
            print("No clips selected or found in bin. Please select clips or timelines.")
            return
        end

        print("Processing selected clips...")
        
        -- Process each selected clip
        for _, media_pool_item in pairs(selected_clips) do
            -- Skip invalid items
            if type(media_pool_item) == "nil" or type(media_pool_item) == "number" then
                print("Skipping invalid item")
                goto continue
            end
            
            -- Try to get clip property to check if it's a timeline
            local clip_type = ""
            pcall(function() clip_type = media_pool_item:GetClipProperty("Type") end)
            
            if clip_type == "Timeline" then
                local timeline_name = media_pool_item:GetName()
                print("Processing timeline: " .. timeline_name)
                
                local curr_timeline = project_timelines[timeline_name]
                if not curr_timeline then
                    print("Timeline not found in project: " .. timeline_name)
                    goto continue
                end
                
                local num_tracks = curr_timeline:GetTrackCount("video")
                for track_idx = 1, num_tracks do
                    local track_items = curr_timeline:GetItemListInTrack("video", track_idx)
                    if not track_items then
                        print("No items in track " .. track_idx)
                        goto continue_track
                    end
                    
                    for item_index, track_item in ipairs(track_items) do
                        -- Check item validity with more detailed reporting
                        if not track_item then
                            print("Track item #" .. item_index .. " is nil")
                            goto continue_item
                        end
                        
                        -- Try to get name for better error reporting
                        local item_name = "Unknown"
                        pcall(function() item_name = track_item:GetName() end)
                        
                        -- Print detailed information about the clip
                        print("Processing item #" .. item_index .. ": " .. item_name)
                        
                        -- Check if clip is enabled
                        local is_enabled = true
                        pcall(function() is_enabled = track_item:GetClipEnabled() end)
                        
                        if allow_disabled_clips or is_enabled then
                            local media_item = nil
                            local get_media_success = pcall(function() 
                                media_item = track_item:GetMediaPoolItem() 
                            end)
                            
                            if not get_media_success or not media_item then
                                print("  Could not retrieve media item for clip: " .. item_name)
                                goto continue_item
                            end
                            
                            local id = nil
                            local get_id_success = pcall(function() id = media_item:GetMediaId() end)
                            
                            if not get_id_success or not id then
                                print("  Could not retrieve media ID for clip: " .. item_name)
                                goto continue_item
                            end
                            
                            -- Get source frame range
                            local start_frame, end_frame
                            local frame_success = pcall(function()
                                start_frame = track_item:GetSourceStartFrame()
                                end_frame = track_item:GetSourceEndFrame() - 1
                            end)
                            
                            if not frame_success or not start_frame or not end_frame then
                                print("  Could not retrieve frame range for clip: " .. item_name)
                                goto continue_item
                            end
                            
                            -- Check if reversed
                            local is_reversed = start_frame > end_frame
                            
                            -- Always create clip info regardless of reversed status
                            if is_reversed then
                                print("  FOUND REVERSED CLIP: " .. item_name)
                                print("  Source frames: " .. start_frame .. " to " .. end_frame)
                            end
                            
                            if not clips[id] then
                                clips[id] = {}
                            end
                            
                            -- Check if clip is retimed (before adding to tracking table)
                            local is_retimed = false
                            local retime_percentage = nil
                            
                            if mark_retimed_clips then
                                -- Create temporary clip data structure for retime check
                                local temp_clip = {
                                    clip = track_item,
                                    mediaPoolItem = media_item,
                                    name = item_name,
                                    startFrame = track_item:GetStart(),
                                    trackIndex = track_idx
                                }
                                
                                -- Check if retimed using the same detection function
                                is_retimed = isClipRetimed(temp_clip)
                                if is_retimed then
                                    print("  FOUND RETIMED CLIP: " .. item_name)
                                    if temp_clip.retimePercentage then
                                        print("  Speed: " .. temp_clip.retimePercentage .. "%")
                                        retime_percentage = temp_clip.retimePercentage
                                    end
                                end
                            end
                            
                            -- Create the clip info with the original frame values
                            local clip_info = {
                                mediaPoolItem = media_item,
                                startFrame = start_frame,
                                endFrame = end_frame,
                                timelineInpoint = track_item:GetStart(),
                                isReversed = is_reversed,
                                isRetimed = is_retimed,
                                retimePercentage = retime_percentage
                            }
                            
                            -- Always add the clip to our tracking table
                            table.insert(clips[id], clip_info)
                            print("  Added clip to processing list: " .. item_name .. 
                                  " (Start: " .. start_frame .. ", End: " .. end_frame .. ")")
                        else
                            print("  Skipping disabled clip: " .. item_name)
                        end
                        
                        ::continue_item::
                    end
                    
                    ::continue_track::
                end
            else
                print("Skipping non-timeline item: " .. media_pool_item:GetName())
            end
            
            ::continue::
        end

        if next(clips) == nil then
            print("No valid clips found to process.")
            return
        end

        print("Unmerged clips:")
        print_table(clips)

        -- For each clips[id], merge clip_infos that are less than a certain amount of frames apart
        for id, clip_infos in pairs(clips) do
            clips[id] = merge_clip_infos_if_close_all(clip_infos, connection_threshold)
        end

        print("Merged Clips")
        print_table(clips)

        -- Create a flat list of all clip_infos for final sorting
        local all_clip_infos = {}
        for _, clip_infos in pairs(clips) do
            for _, clip_info in pairs(clip_infos) do
                table.insert(all_clip_infos, clip_info)
            end
        end

        -- Apply the selected sorting method
        print("Sorting using method: " .. sorting_method)
        if sorting_method == "Source Inpoint" then
            table.sort(all_clip_infos, function(a, b)
                return a.startFrame < b.startFrame
            end)
        elseif sorting_method == "Source Name" then
            -- Sort by source name (file name), then by timeline inpoint for duplicates
            table.sort(all_clip_infos, function(a, b)
                local name_a = a.mediaPoolItem:GetName()
                local name_b = b.mediaPoolItem:GetName()
                
                -- If source names are the same (duplicates), sort by timeline inpoint
                if name_a == name_b then
                    return a.timelineInpoint < b.timelineInpoint
                end
                
                return name_a < name_b
            end)
        elseif sorting_method == "Inpoint on Timeline" then
            table.sort(all_clip_infos, function(a, b)
                return a.timelineInpoint < b.timelineInpoint
            end)
        elseif sorting_method == "Reel Name" then
            -- Now sort by reel name, falling back to filename if no reel name is present
            table.sort(all_clip_infos, function(a, b)
                local reel_name_a = a.mediaPoolItem:GetClipProperty("Reel Name") or ""
                local reel_name_b = b.mediaPoolItem:GetClipProperty("Reel Name") or ""
                local name_a = a.mediaPoolItem:GetName()
                local name_b = b.mediaPoolItem:GetName()
                
                -- If both clips have reel names, use those for comparison
                if reel_name_a ~= "" and reel_name_b ~= "" then
                    -- If reel names are different, sort by reel
                    if reel_name_a ~= reel_name_b then
                        return reel_name_a < reel_name_b
                    end
                    
                    -- If reel names are identical (same clip), sort by timeline inpoint
                    return a.timelineInpoint < b.timelineInpoint
                
                -- If only one clip has a reel name, put clips with reel names first
                elseif reel_name_a ~= "" and reel_name_b == "" then
                    return true
                elseif reel_name_a == "" and reel_name_b ~= "" then
                    return false
                
                -- If neither clip has a reel name, fall back to sorting by filename
                else
                    -- If clip names are different, sort by clip name
                    if name_a ~= name_b then
                        return name_a < name_b
                    end
                    
                    -- If clip names are the same (duplicates), sort by timeline inpoint
                    return a.timelineInpoint < b.timelineInpoint
                end
            end)
        elseif sorting_method == "None" then
            -- No sorting, leave in original order after merging
            print("No sorting applied")
        else
            print("Unknown sorting method, using default (Source Name)")
            table.sort(all_clip_infos, function(a, b)
                return a.mediaPoolItem:GetName() < b.mediaPoolItem:GetName()
            end)
        end

        print("Adding all clips to new timeline...")
        local new_timeline = media_pool:CreateEmptyTimeline(dst_timeline_name)
        assert(project:SetCurrentTimeline(new_timeline), "couldn't set current timeline to the new timeline")

        -- Add clips in the sorted order
        local append_success_count = 0
        local append_error_count = 0
        
        for i, clip_info in ipairs(all_clip_infos) do
            print("Adding clip #" .. i .. " to timeline:")
            print("  Clip name: " .. clip_info.mediaPoolItem:GetName())
            print("  Frame range: " .. clip_info.startFrame .. " to " .. clip_info.endFrame)
            
            -- Add information about clip status
            local status_info = ""
            if clip_info.isReversed then
                status_info = "REVERSED"
            end
            if clip_info.isRetimed then
                if status_info ~= "" then
                    status_info = status_info .. ", RETIMED"
                else
                    status_info = "RETIMED"
                end
            end
            if status_info ~= "" then
                print("  Special clip status: " .. status_info)
            end
            
            -- For reversed clips, create a normalized version
            if clip_info.isReversed then
                print("  Using special handling for reversed clip")
                
                -- Create a new timeline item with the clip playing in reverse
                local success = false
                
                -- Try different approaches
                -- 1. First try: Swap start/end frames and preserve speed direction
                local swap_approach = {
                    mediaPoolItem = clip_info.mediaPoolItem,
                    startFrame = clip_info.endFrame,  -- Swap start/end for reversed clip
                    endFrame = clip_info.startFrame,
                    importVideo = true,
                    importAudio = true
                }
                
                print("  Trying approach 1: Swapped start/end frames")
                success = pcall(function() media_pool:AppendToTimeline({swap_approach}) end)
                
                -- 2. Second try: Use normalized frame range and apply speed change after
                if not success then
                    print("  Approach 1 failed, trying approach 2: Add normalized and modify speed")
                    
                    local normalized_info = {
                        mediaPoolItem = clip_info.mediaPoolItem,
                        startFrame = math.min(clip_info.startFrame, clip_info.endFrame),
                        endFrame = math.max(clip_info.startFrame, clip_info.endFrame)
                    }
                    
                    -- Add to timeline
                    success = pcall(function() 
                        media_pool:AppendToTimeline({normalized_info})
                        
                        -- Try to get the added clip and modify its speed
                        local new_timeline = project:GetCurrentTimeline()
                        if new_timeline then
                            local tracks = new_timeline:GetTrackCount("video")
                            if tracks > 0 then
                                local items = new_timeline:GetItemListInTrack("video", 1)
                                if items and #items > 0 then
                                    local latest_item = items[#items] -- Get most recently added item
                                    if latest_item then
                                        -- Try to set speed to negative to play in reverse
                                        latest_item:SetClipProperty("Speed", "-100")
                                    end
                                end
                            end
                        end
                    end)
                end
                
                -- 3. Last resort: Try with original values
                if not success then
                    print("  Approach 2 failed, trying original values as last resort")
                    success = pcall(function() media_pool:AppendToTimeline({clip_info}) end)
                end
                
                if success then
                    print("  Successfully added reversed clip")
                    append_success_count = append_success_count + 1
                else
                    print("  Failed to add reversed clip after all attempts")
                    append_error_count = append_error_count + 1
                end
            else
                -- For normal clips, use standard approach
                local success = pcall(function() media_pool:AppendToTimeline({clip_info}) end)
                
                if success then
                    append_success_count = append_success_count + 1
                else
                    print("  Failed to add normal clip: " .. clip_info.mediaPoolItem:GetName())
                    append_error_count = append_error_count + 1
                end
            end
        end
        
        print("Clip addition summary: " .. append_success_count .. " succeeded, " .. 
              append_error_count .. " failed")

        print("New timeline created: " .. dst_timeline_name)
        
        -- Remove audio if video only option is selected
        if video_only then
            print("Video Only option selected - removing all audio tracks...")
            removeAllAudioTracks(new_timeline)
        end
        
        -- Mark duplicates if option is selected
        if mark_duplicates then
            print("Marking duplicates in the new timeline...")
            findAndMarkDuplicates(new_timeline)
        end
        
        -- Mark retimed clips if option is selected
        if mark_retimed_clips then
            print("Marking retimed clips in the new timeline...")
            local clipList = getAllClips(new_timeline)
            local markerCount = 0
            local successCount = 0
            
            -- Loop through all clips on the new timeline
            for i, timeline_clip in ipairs(clipList) do
                -- Find the matching original clip_info to determine if it was retimed
                local isMatched = false
                for _, clip_info in ipairs(all_clip_infos) do
                    if timeline_clip.mediaPoolItem:GetName() == clip_info.mediaPoolItem:GetName() and
                       timeline_clip.clip:GetSourceStartFrame() >= math.min(clip_info.startFrame, clip_info.endFrame) and
                       timeline_clip.clip:GetSourceEndFrame() <= math.max(clip_info.startFrame, clip_info.endFrame) + 1 then
                        
                        -- If the original clip was marked retimed, mark this clip too
                        if clip_info.isRetimed then
                            -- Add marker to the TimelineItem at 50% of the clip duration
                            local sourceStartFrame = timeline_clip.clip:GetSourceStartFrame()
                            local clipDuration = timeline_clip.clip:GetDuration()
                            local markerPosition = sourceStartFrame + math.floor(clipDuration * 0.5)
                            
                            -- Use red color for retimed clips
                            local markerText = "Retimed Clip"
                            local speedValue = clip_info.retimePercentage or "Unknown"
                            
                            local success = pcall(function() 
                                return timeline_clip.clip:AddMarker(markerPosition, "Red", markerText, 
                                                 "Manual check recommended. Speed: " .. tostring(speedValue) .. "%", 1, "")
                            end)
                            
                            markerCount = markerCount + 1
                            
                            if success then
                                successCount = successCount + 1
                                print("  Added retime marker to clip: " .. timeline_clip.name)
                            else
                                print("  Failed to add retime marker to clip: " .. timeline_clip.name)
                            end
                            
                            isMatched = true
                            break
                        end
                    end
                end
            end
            
            print("Successfully added " .. successCount .. " retime markers out of " .. markerCount .. " attempts.")
        end

        print("Done!")
    end
end

-- Run the main function
main()