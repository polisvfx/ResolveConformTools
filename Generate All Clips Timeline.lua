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
        local reel_name_a = a.mediaPoolItem:GetMetadata("Reel Name") or ""
        local reel_name_b = b.mediaPoolItem:GetMetadata("Reel Name") or ""
        return reel_name_a < reel_name_b
    end)
end

function sort_clip_infos_by_timeline_inpoint(clip_infos)
    -- In place sort by timeline inpoint (requires timelineInpoint to be set)
    table.sort(clip_infos, function(a, b)
        return a.timelineInpoint < b.timelineInpoint
    end)
end

function merge_clip_infos(clip_info_a, clip_info_b)
    -- Return new clipinfo that spans clip_info_a and clip_info_b
    assert(clip_info_a.mediaPoolItem == clip_info_b.mediaPoolItem,
        "merge_clip_infos: Cannot merge clip infos from different media pool items")
    return {
        mediaPoolItem = clip_info_a.mediaPoolItem,
        startFrame = math.min(clip_info_a.startFrame, clip_info_b.startFrame),
        endFrame = math.max(clip_info_a.endFrame, clip_info_b.endFrame),
        timelineInpoint = clip_info_a.timelineInpoint -- Keep the first clip's inpoint for sorting
    }
end

function check_clip_infos_overlap(clip_info_a, clip_info_b, connection_threshold)
    return not (clip_info_a.endFrame < clip_info_b.startFrame - connection_threshold or clip_info_b.endFrame <
               clip_info_a.startFrame - connection_threshold)
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
        "Red", "Yellow", "Green", "Cyan", "Blue", "Purple", "Pink", "Fuchsia"
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
                    Text = "24",
                    PlaceholderText = "24"
                }
            },
            ui:CheckBox{
                ID = "includeDisabledItems",
                Text = "Include Disabled Clips"
            },
            ui:CheckBox{
                ID = "videoOnly",
                Text = "Video Only (No Audio)"
            },
            ui:CheckBox{
                ID = "markDuplicates",
                Text = "Mark Duplicate Clips with Colored Markers"
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

        -- Get timelines
        resolve = Resolve()
        projectManager = resolve:GetProjectManager()
        project = projectManager:GetCurrentProject()
        media_pool = project:GetMediaPool()
        num_timelines = project:GetTimelineCount()
        selected_bin = media_pool:GetCurrentFolder()

        -- Iterate through timelines, figure out what clips we need and what frames are required.
        -- We'll make a table where the key is a clip identifier and the value is a clipinfo.
        local clips = {}

        -- Mapping of timeline name to timeline object
        project_timelines = {}
        for timeline_idx = 1, num_timelines do
            runner_timeline = project:GetTimelineByIndex(timeline_idx)
            project_timelines[runner_timeline:GetName()] = runner_timeline
        end

        -- Iterate through timelines in the current folder.
        local selected_clips
        if itm.selectionMethod.CurrentText == "Current Bin" then
            selected_clips = selected_bin:GetClipList()
        elseif itm.selectionMethod.CurrentText == "Current Selection" then
            selected_clips = media_pool:GetSelectedClips()
        else
            assert(false, "Unknown selection method.")
        end
        
        for _, media_pool_item in pairs(selected_clips) do
            -- Check if it's a timeline
            if type(media_pool_item) == nil or type(media_pool_item) == "number" then
                print("Skipping", media_pool_item)
            elseif media_pool_item:GetClipProperty("Type") == "Timeline" then
                desired_timeline_name = media_pool_item:GetName()
                curr_timeline = project_timelines[desired_timeline_name]

                num_tracks = curr_timeline:GetTrackCount("video")
                for track_idx = 1, num_tracks do
                    track_items = curr_timeline:GetItemListInTrack("video", track_idx)
                    for _, track_item in pairs(track_items) do
                        if (track_item == nil or type(track_item) == "number") then
                            print("Skipping ", track_item)
                        elseif allow_disabled_clips or track_item:GetClipEnabled() then
                            -- Add clip and clipinfo to clips.
                            if (track_item:GetMediaPoolItem() == nil) then
                                print("could not retrieve media item for clip ", track_item:GetName())
                            else
                                media_item = track_item:GetMediaPoolItem()
                                id = media_item:GetMediaId()
                                local start_frame = track_item:GetSourceStartFrame()
                                local end_frame = track_item:GetSourceEndFrame() - 1
                                if clips[id] == nil then
                                    clips[id] = {}
                                end
                                clip_info = {
                                    mediaPoolItem = media_item,
                                    startFrame = start_frame,
                                    endFrame = end_frame,
                                    timelineInpoint = track_item:GetStart() -- Store the inpoint on timeline for sorting
                                }
                                clips[id][#clips[id] + 1] = clip_info
                            end
                        end
                    end
                end
            end
        end

        print("Unmerged clips:")
        print_table(clips)

        -- for each clips[id], merge clip_infos that are less than a certain amount of frames apart
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
            -- First, let's debug by printing the reel names
            print("Sorting by Reel Name - showing all clip reel names:")
            for i, clip_info in ipairs(all_clip_infos) do
                local reel_name = clip_info.mediaPoolItem:GetClipProperty("Reel Name") or "NO_REEL_NAME"
                print("Clip #" .. i .. ": " .. clip_info.mediaPoolItem:GetName() .. " - Reel Name: " .. reel_name)
            end
            
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
            
            -- Print the sorted order for verification
            print("After sorting - clips in sorted order:")
            for i, clip_info in ipairs(all_clip_infos) do
                local reel_name = clip_info.mediaPoolItem:GetClipProperty("Reel Name") or "NO_REEL_NAME"
                local inpoint = clip_info.timelineInpoint or 0
                print("Position #" .. i .. ": " .. clip_info.mediaPoolItem:GetName() .. 
                      " - Reel Name: " .. reel_name .. " - Timeline Inpoint: " .. inpoint)
            end
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
        for _, clip_info in ipairs(all_clip_infos) do
            media_pool:AppendToTimeline({clip_info})
        end

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

        print("Done!")
    end
end

-- Run the main function
main()