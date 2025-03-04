-- User editable variables
local DEFAULT_HANDLES = 15          -- Default number of handles to add
local DEFAULT_COLORSPACE = "ARRI LogC4" -- Default colorspace for Read node
-- You can change these values to your preferences

-- Get the current project
local project = resolve:GetProjectManager():GetCurrentProject()
-- Get the current timeline
local timeline = project:GetCurrentTimeline()

if timeline then
    -- Get selected timeline items
    local selectedItems = timeline:GetCurrentVideoItem()
    
    if selectedItems then
        -- Format timecode for display (assuming 24fps - adjust if needed)
        local function formatTimecode(frames)
            local fps = 24
            local totalSeconds = math.floor(frames / fps)
            local hours = math.floor(totalSeconds / 3600)
            local minutes = math.floor((totalSeconds % 3600) / 60)
            local seconds = totalSeconds % 60
            local remainingFrames = frames % fps
            
            return string.format("%02d:%02d:%02d:%02d", hours, minutes, seconds, remainingFrames)
        end
        
        -- Helper function to write to clipboard
        local function writeToClipboard(text)
            -- Create a temporary file
            local tmpfile = os.tmpname()
            local f = io.open(tmpfile, 'w')
            f:write(text)
            f:close()
            
            -- Check OS and use appropriate clipboard command
            if package.config:sub(1,1) == '\\' then
                -- Windows
                os.execute(string.format('type "%s" | clip', tmpfile))
            else
                -- macOS/Linux
                os.execute(string.format('cat "%s" | pbcopy', tmpfile))
            end
            
            -- Clean up temp file
            os.remove(tmpfile)
        end
        
        -- Get clip information
        local filepath = selectedItems:GetMediaPoolItem():GetClipProperty("File Path")
        -- Convert backslashes to forward slashes for Nuke
        local nukePath = string.gsub(filepath, "\\", "/")
        
        -- Get reel name from clip
        local mediaPoolItem = selectedItems:GetMediaPoolItem()
        local reelName = mediaPoolItem:GetClipProperty("Reel Name") or ""
        
        -- If reel name is empty, try to extract it from the filename
        if reelName == "" then
            -- Try to extract something that looks like a reel name from the filename
            local baseName = string.match(filepath, "([^/\\]+)%.[^.]+$") or ""
            reelName = string.match(baseName, "([A-Z]%d+[A-Z]%d+_%d+_[A-Z]+)") or baseName
        end
        
        -- Initialize frame range variables
        local firstFrame = 1
        local lastFrame
        
        -- Try to extract frame range from filename
        local frameMatch = string.match(nukePath, "%[(%d+)%-(%d+)%]")
        if frameMatch then
            -- Extract first and last frame numbers
            firstFrame, lastFrame = string.match(nukePath, "%[(%d+)%-(%d+)%]")
            firstFrame = tonumber(firstFrame)
            lastFrame = tonumber(lastFrame)
            -- Replace frame range with ####
            nukePath = string.gsub(nukePath, "%[%d+%-%d+%]", "####")
        else
            -- If no frame range in filename, use clip length
            lastFrame = tonumber(mediaPoolItem:GetClipProperty("Frames")) or 
                       tonumber(mediaPoolItem:GetClipProperty("Full Duration")) or
                       tonumber(mediaPoolItem:GetClipProperty("Duration"))
            
            if not lastFrame then
                -- Fallback: Try to get length from media info
                local fps = timeline:GetSetting("timelineFrameRate")
                local duration = mediaPoolItem:GetClipProperty("Duration")
                if duration and fps then
                    lastFrame = math.floor(duration * fps)
                else
                    lastFrame = 1000
                    print("Warning: Could not determine clip length. Using default value.")
                end
            end
        end
        
        local startTC = selectedItems:GetStart()
        local endTC = selectedItems:GetEnd()
        local startFrame = selectedItems:GetLeftOffset()
        local endFrame = startFrame + (endTC - startTC)
        
        -- Create Nuke node setup text
        local nukeText = "set cut_paste_input [stack 0]\n"
        -- Read Node
        nukeText = nukeText .. "Read {\n"
        nukeText = nukeText .. " inputs 0\n"
        nukeText = nukeText .. string.format(' file "%s"\n', nukePath)
        nukeText = nukeText .. string.format(" first %d\n", firstFrame)
        nukeText = nukeText .. string.format(" last %d\n", lastFrame)
        nukeText = nukeText .. string.format(" origlast %d\n", lastFrame)
        nukeText = nukeText .. " origset true\n"
        nukeText = nukeText .. string.format(' colorspace "%s"\n', DEFAULT_COLORSPACE)
        nukeText = nukeText .. " name ReadFromResolve1\n"
        nukeText = nukeText .. " selected true\n"
        nukeText = nukeText .. " xpos 0\n"
        nukeText = nukeText .. " ypos 0\n"		
        nukeText = nukeText .. "}\n"
        -- ModifyMetaData Node
        nukeText = nukeText .. "ModifyMetaData {\n"
        nukeText = nukeText .. " metadata {\n"
        nukeText = nukeText .. string.format("  {set exr/reelName %s}\n", reelName)
        nukeText = nukeText .. string.format("  {set exr/owner %s}\n", reelName)
        nukeText = nukeText .. " }\n"
        nukeText = nukeText .. " name ModifyMetaData1\n"
        nukeText = nukeText .. " xpos 0\n"
        nukeText = nukeText .. " ypos 100\n"	
        nukeText = nukeText .. "}\n"
        -- Group Node
        nukeText = nukeText .. "Group {\n"
        nukeText = nukeText .. " name ShotSetup\n"
        nukeText = nukeText .. " selected true\n"
        nukeText = nukeText .. " addUserKnob {20 Shotdata l \"Shot Settings\"}\n"
        nukeText = nukeText .. " addUserKnob {6 length_from_input l \"Input Length\" +STARTLINE}\n"
        nukeText = nukeText .. " addUserKnob {3 inpoint l \"\" t \"Source first frame.\" -STARTLINE}\n"
        nukeText = nukeText .. ' inpoint {{\"\\[value length_from_input] > 0 ? first_frame : Input1.firstframe\"}}\n'
        nukeText = nukeText .. " addUserKnob {3 outpoint l \"\" -STARTLINE}\n"
        nukeText = nukeText .. ' outpoint {{\"\\[value length_from_input] > 0 ? last_frame : Input1.lastframe\"}}\n'
        nukeText = nukeText .. " addUserKnob {41 firstframe_1 l \"First Frame\" T Input1.firstframe}\n"
        nukeText = nukeText .. " addUserKnob {41 lastframe l \"Last Frame\" T Input1.lastframe}\n"
        nukeText = nukeText .. " addUserKnob {41 handles l \"Included Handles\" T Input1.handles}\n"
        nukeText = nukeText .. " addUserKnob {41 addhandles l \"Add Handles\" T Input1.addhandles}\n"
        nukeText = nukeText .. " addUserKnob {41 shownmetadata l \"\" +STARTLINE T ViewMetaData1.shownmetadata}\n"
        nukeText = nukeText .. " addUserKnob {41 metadatafilter l \"search metadata for\" T ViewMetaData1.metadatafilter}\n"
        nukeText = nukeText .. " xpos 0\n"
        nukeText = nukeText .. " ypos 200\n"	
        nukeText = nukeText .. "}\n"
        -- Input Node
        nukeText = nukeText .. " Input {\n"
        nukeText = nukeText .. "  inputs 0\n"
        nukeText = nukeText .. "  name Input1\n"
        nukeText = nukeText .. "  xpos -29\n"
        nukeText = nukeText .. "  addUserKnob {20 Shotdata l \"Shot Settings\"}\n"
        nukeText = nukeText .. "  addUserKnob {3 firstframe l \"First Frame\"}\n"
        nukeText = nukeText .. string.format("  firstframe %d\n", startFrame)
        nukeText = nukeText .. "  addUserKnob {3 lastframe l \"Last Frame\" -STARTLINE}\n"
        nukeText = nukeText .. string.format("  lastframe %d\n", endFrame)
        nukeText = nukeText .. "  addUserKnob {3 handles l \"Included Handles\"}\n"
        nukeText = nukeText .. "  addUserKnob {3 addhandles l \"Add Handles\"}\n"
        nukeText = nukeText .. string.format("  addhandles %d\n", DEFAULT_HANDLES)
        nukeText = nukeText .. " }\n"
        -- FrameRange Nodes
        nukeText = nukeText .. " FrameRange {\n"
        nukeText = nukeText .. "  first_frame {{parent.Input1.first_frame}}\n"
        nukeText = nukeText .. "  last_frame {{parent.Input1.last_frame}}\n"
        nukeText = nukeText .. '  time ""\n'
        nukeText = nukeText .. "  name OriginalFramerange\n"
        nukeText = nukeText .. "  xpos -29\n"
        nukeText = nukeText .. "  ypos 56\n"
        nukeText = nukeText .. "  disable {{!parent.length_from_input}}\n"
        nukeText = nukeText .. " }\n"
        nukeText = nukeText .. " FrameRange {\n"
        nukeText = nukeText .. "  first_frame {{parent.Input1.firstframe+1}}\n"
        nukeText = nukeText .. "  last_frame {{parent.Input1.lastframe}}\n"
        nukeText = nukeText .. '  time ""\n'
        nukeText = nukeText .. "  name CustomFramerange\n"
        nukeText = nukeText .. "  xpos -29\n"
        nukeText = nukeText .. "  ypos 101\n"
        nukeText = nukeText .. "  disable {{parent.length_from_input}}\n"
        nukeText = nukeText .. " }\n"
        -- TimeOffset Node
        nukeText = nukeText .. " TimeOffset {\n"
        nukeText = nukeText .. '  time_offset {{"(parent.length_from_input < 1 ? -parent.CustomFramerange.knob.first_frame+1001 : -parent.OriginalFramerange.knob.first_frame+1001)-Input1.handles"}}\n'
        nukeText = nukeText .. '  time ""\n'
        nukeText = nukeText .. "  name TimeOffset\n"
        nukeText = nukeText .. "  selected true\n"
        nukeText = nukeText .. "  xpos -29\n"
        nukeText = nukeText .. "  ypos 160\n"
        nukeText = nukeText .. "  addUserKnob {20 setup l Setup}\n"
        nukeText = nukeText .. "  addUserKnob {3 handles l Handles}\n"
        nukeText = nukeText .. "  handles {{parent.Input1.handles}}\n"
        nukeText = nukeText .. " }\n"
        -- Target Range Node
        nukeText = nukeText .. " FrameRange {\n"
        nukeText = nukeText .. "  first_frame {{input.first_frame-Input1.addhandles}}\n"
        nukeText = nukeText .. "  last_frame {{input.last_frame+Input1.addhandles}}\n"
        nukeText = nukeText .. '  time ""\n'
        nukeText = nukeText .. "  name TargetRange\n"
        nukeText = nukeText .. "  xpos -29\n"
        nukeText = nukeText .. "  ypos 222\n"
        nukeText = nukeText .. " }\n"
        -- ViewMetaData Node
        nukeText = nukeText .. " ViewMetaData {\n"
        nukeText = nukeText .. "  metadatafilter timecode\n"
        nukeText = nukeText .. "  name ViewMetaData1\n"
        nukeText = nukeText .. "  xpos -29\n"
        nukeText = nukeText .. "  ypos 298\n"
        nukeText = nukeText .. " }\n"
        -- Output Node
        nukeText = nukeText .. " Output {\n"
        nukeText = nukeText .. "  name Output1\n"
        nukeText = nukeText .. "  xpos -29\n"
        nukeText = nukeText .. "  ypos 361\n"
        nukeText = nukeText .. " }\n"
        nukeText = nukeText .. "end_group"

        -- Copy to clipboard
        writeToClipboard(nukeText)
        
        -- Print clip information to console
        print("Selected Clip Information:")
        print("Filepath:", filepath)
        print("Reel Name:", reelName)
        print("Cut In TC:", formatTimecode(startTC))
        print("Cut Out TC:", formatTimecode(endTC))
        print("Start Frame:", startFrame)
        print("End Frame:", endFrame)
        print("Sequence First Frame:", firstFrame)
        print("Sequence Last Frame:", lastFrame)
        print("Handles:", DEFAULT_HANDLES)
        print("Colorspace:", DEFAULT_COLORSPACE)
        
        -- Print Nuke setup with clear separation
        print("\n=====================================")
        print("NUKE NODE SETUP (Copied to clipboard)")
        print("=====================================\n")
        print(nukeText)
        print("\n=====================================")
        
    else
        print("No clip is currently selected. Please select a clip in the timeline.")
    end
else
    print("No timeline is active. Please open a timeline.")
end