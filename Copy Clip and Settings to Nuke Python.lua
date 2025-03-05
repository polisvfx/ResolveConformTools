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
        
        -- Get clip resolution with better error handling
        local width, height = 1920, 1080  -- Default fallback values
        
        -- Try different methods to get resolution
        local resX = mediaPoolItem:GetClipProperty("Resolution X")
        local resY = mediaPoolItem:GetClipProperty("Resolution Y")
        
        if resX and resY and resX ~= "" and resY ~= "" then
            width = tonumber(resX) or width
            height = tonumber(resY) or height
        else
            -- Try alternate properties that might contain resolution
            local resolution = mediaPoolItem:GetClipProperty("Resolution")
            if resolution then
                -- Try to extract dimensions from a string like "1920x1080"
                local w, h = string.match(resolution, "(%d+)%s*x%s*(%d+)")
                if w and h then
                    width, height = tonumber(w) or width, tonumber(h) or height
                end
            end
            print("Using default resolution: " .. width .. "x" .. height)
        end
        
        -- Get clip FPS
        local clipFPS = 24.0  -- Default fallback value
        
        -- Try different methods to get FPS
        local fps = mediaPoolItem:GetClipProperty("FPS")
        if fps and fps ~= "" then
            clipFPS = tonumber(fps) or clipFPS
        else
            -- Try to get FPS from timeline
            local timelineFPS = timeline:GetSetting("timelineFrameRate")
            if timelineFPS and timelineFPS ~= "" then
                clipFPS = tonumber(timelineFPS) or clipFPS
            end
            print("Using default FPS: " .. clipFPS)
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
        
        -- Create Nuke Python code
        local pythonCode = [[
import nuke

# Clear any existing nodes
for n in nuke.allNodes():
    nuke.delete(n)

def modify_project_settings():
    print("Modifying project settings...")
    
    # Calculate handles
    handles = ]] .. DEFAULT_HANDLES .. [[
    
    # Calculate clip length
    clip_length = ]] .. endFrame .. [[ - ]] .. startFrame .. [[
    
    # Calculate frame range
    first_frame = 1001 - handles
    
    # Calculate last frame 
    last_frame = 1001 + clip_length + handles
    
    # Set frame range
    nuke.root()["first_frame"].setValue(first_frame)
    print("Successfully set first_frame to " + str(first_frame))
    
    nuke.root()["last_frame"].setValue(last_frame)
    print("Successfully set last_frame to " + str(last_frame))
    
    # Set fps to match the clip's FPS
    nuke.root()["fps"].setValue(]] .. clipFPS .. [[)
    print("Successfully set fps to ]] .. clipFPS .. [[")
    
    # Create a custom format with the clip's resolution
    try:
        # Format: "<width> <height> <name>"
        format_string = "]] .. width .. [[ ]] .. height .. [[ Plate"
        nuke.addFormat(format_string)
        print(f"Created new format: {format_string}")
        
        # Set project format to the new format
        nuke.root()["format"].setValue("Plate")
        print("Set project format to Plate")
    except Exception as e:
        print(f"Error creating custom format: {e}")
        
        # Fallback approach - try to find a similar format
        try:
            existing_formats = nuke.formats()
            print(f"Available formats: {[f.name() for f in existing_formats]}")
            
            # Try to find a format with similar resolution
            for f in existing_formats:
                if str(]] .. width .. [[) in f.name() or str(]] .. height .. [[) in f.name():
                    nuke.root()["format"].setValue(f.name())
                    print(f"Using existing format: {f.name()}")
                    break
        except Exception as e2:
            print(f"Format fallback also failed: {e2}")
    
    # Set view settings
    try:
        nuke.Root()['views'].fromScript('left right')
        print("Successfully set views")
    except Exception as e:
        print(f"Could not set views: {e}")
    
    print("Project settings modification completed")

# First modify the project settings
modify_project_settings()

# Create Read node
read = nuke.createNode("Read")
read["file"].setValue("]] .. nukePath .. [[")
read["first"].setValue(]] .. firstFrame .. [[)
read["last"].setValue(]] .. lastFrame .. [[)
read["origfirst"].setValue(]] .. firstFrame .. [[)
read["origlast"].setValue(]] .. lastFrame .. [[)
read["colorspace"].setValue("]] .. DEFAULT_COLORSPACE .. [[")
read["name"].setValue("ReadFromResolve1")

# Create ModifyMetaData node with metadata
try:
    # Create a new ModifyMetaData node
    metadata_node = nuke.createNode("ModifyMetaData")
    metadata_node.setName("ModifyMetaData1")
    
    # Create the metadata string with both entries
    metadata_str = "{set exr/reelName ]] .. reelName .. [[}\n{set exr/owner ]] .. reelName .. [[}"
    
    # Use fromScript method to set the metadata values
    metadata_node["metadata"].fromScript(metadata_str)
    
    print("Successfully created ModifyMetaData node with both entries")
except Exception as e:
    print(f"Error creating ModifyMetaData node: {e}")

# Create Group node for shot setup
group = nuke.createNode("Group")
group.setName("ShotSetup")

# Connect ModifyMetaData node to the Read node
read.setInput(0, None)  # Ensure it has no inputs
metadata_node.setInput(0, read)  # Connect the metadata node to the read node

# Connect ShotSetup group to the ModifyMetaData node
group.setInput(0, metadata_node)  # Connect the group to the metadata node

# Enter the group to add nodes inside it
group.begin()

# Add custom knobs
tab = nuke.Tab_Knob("Shotdata", "Shot Settings")
group.addKnob(tab)

length_from_input = nuke.Boolean_Knob("length_from_input", "Input Length")
group.addKnob(length_from_input)

inpoint = nuke.Int_Knob("inpoint", "")
inpoint.setTooltip("Source first frame.")
group.addKnob(inpoint)
inpoint.setExpression("[value length_from_input] > 0 ? first_frame : Input1.firstframe")

outpoint = nuke.Int_Knob("outpoint", "")
group.addKnob(outpoint)
outpoint.setExpression("[value length_from_input] > 0 ? last_frame : Input1.lastframe")

# Link other knobs
firstframe_link = nuke.Link_Knob("firstframe_1", "First Frame")
firstframe_link.setLink("Input1.firstframe")
group.addKnob(firstframe_link)

lastframe_link = nuke.Link_Knob("lastframe", "Last Frame")
lastframe_link.setLink("Input1.lastframe")
group.addKnob(lastframe_link)

handles_link = nuke.Link_Knob("handles", "Included Handles")
handles_link.setLink("Input1.handles")
group.addKnob(handles_link)

addhandles_link = nuke.Link_Knob("addhandles", "Add Handles")
addhandles_link.setLink("Input1.addhandles")
group.addKnob(addhandles_link)

metadatafilter_link = nuke.Link_Knob("metadatafilter", "search metadata for")
metadatafilter_link.setLink("ViewMetaData1.metadatafilter")
group.addKnob(metadatafilter_link)

# Create Input node inside group
input_node = nuke.createNode("Input")
input_node.setName("Input1")

# Add custom knobs to Input node
tab_input = nuke.Tab_Knob("Shotdata", "Shot Settings")
input_node.addKnob(tab_input)

firstframe_knob = nuke.Int_Knob("firstframe", "First Frame")
input_node.addKnob(firstframe_knob)
input_node["firstframe"].setValue(]] .. startFrame .. [[)

lastframe_knob = nuke.Int_Knob("lastframe", "Last Frame")
input_node.addKnob(lastframe_knob)
input_node["lastframe"].setValue(]] .. endFrame .. [[)

handles_knob = nuke.Int_Knob("handles", "Included Handles")
input_node.addKnob(handles_knob)

addhandles_knob = nuke.Int_Knob("addhandles", "Add Handles")
input_node.addKnob(addhandles_knob)
input_node["addhandles"].setValue(]] .. DEFAULT_HANDLES .. [[)

# Create FrameRange nodes
original_range = nuke.createNode("FrameRange")
original_range.setName("OriginalFramerange")
original_range["first_frame"].setExpression("parent.Input1.first_frame")
original_range["last_frame"].setExpression("parent.Input1.last_frame")
original_range["disable"].setExpression("!parent.length_from_input")

custom_range = nuke.createNode("FrameRange")
custom_range.setName("CustomFramerange")
custom_range["first_frame"].setExpression("parent.Input1.firstframe+1")
custom_range["last_frame"].setExpression("parent.Input1.lastframe")
custom_range["disable"].setExpression("parent.length_from_input")

# Create TimeOffset node
time_offset = nuke.createNode("TimeOffset")
time_offset.setName("TimeOffset")
time_offset["time_offset"].setExpression('(parent.length_from_input < 1 ? -parent.CustomFramerange.knob.first_frame+1001 : -parent.OriginalFramerange.knob.first_frame+1001)-Input1.handles')

# Add custom knob to TimeOffset
setup_tab = nuke.Tab_Knob("setup", "Setup")
time_offset.addKnob(setup_tab)

handles_to = nuke.Int_Knob("handles", "Handles")
time_offset.addKnob(handles_to)
time_offset["handles"].setExpression("parent.Input1.handles")

# Create target FrameRange node
target_range = nuke.createNode("FrameRange")
target_range.setName("TargetRange")
target_range["first_frame"].setExpression("input.first_frame-Input1.addhandles")
target_range["last_frame"].setExpression("input.last_frame+Input1.addhandles")

# Create ViewMetaData node
view_metadata = nuke.createNode("ViewMetaData")
view_metadata.setName("ViewMetaData1")
view_metadata["metadatafilter"].setValue("timecode")

# Create Output node
output = nuke.createNode("Output")
output.setName("Output1")

# Exit the group
group.end()

# Arrange nodes nicely
nuke.autoplace_all()

# Select the group for easier access
group.setSelected(True)

# Close all node panels
for node in nuke.allNodes():
    try:
        node.hideControlPanel()
    except:
        pass  # Some nodes might not have panels, so ignore errors

print("Nuke setup created successfully")
]]
        
        -- Copy to clipboard
        writeToClipboard(pythonCode)
        
        -- Print clip information to console
        print("Selected Clip Information:")
        print("Filepath:", filepath)
        print("Reel Name:", reelName)
        print("Resolution:", width.."x"..height)
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
        print("NUKE PYTHON CODE (Copied to clipboard)")
        print("=====================================\n")
        print(pythonCode)
        print("\n=====================================")
        
    else
        print("No clip is currently selected. Please select a clip in the timeline.")
    end
else
    print("No timeline is active. Please open a timeline.")
end