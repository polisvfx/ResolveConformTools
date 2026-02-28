--[[
DaVinci Resolve Multi-Platform Link/Copy Creator
A GUI script to create hardlinks, symlinks, or file copies for media files in the timeline.
Uses tokens in naming patterns for custom file naming and supports creating subfolders.
--]]

-- Initialize Resolve API
local resolve = resolve
local projectManager = resolve:GetProjectManager()
local project = projectManager:GetCurrentProject()
local mediaPool = project:GetMediaPool()
local timeline = project:GetCurrentTimeline()

-- Debug function
function log(msg)
    print("[MediaLinkCreator] " .. tostring(msg))
end

-- Utility function to sanitize paths
function stripInvalidChars(str, allowSlashes)
    if not str then return "" end

    local pattern = "[%:%*%?%\"%<%>%|]"  -- Default pattern excluding slashes
    if not allowSlashes then
        pattern = "[%/%\\%:%*%?%\"%<%>%|]"  -- Pattern including slashes
    end

    -- Replace invalid characters with underscores
    return string.gsub(str, pattern, "_")
end

-- Get file extension
function getFileExtension(filename)
    if not filename then return "" end
    return string.match(filename, "%.([^%.]+)$") or ""
end

-- Get operating system
function getOperatingSystem()
    local osType = "windows"  -- Default to Windows

    if package.config:sub(1,1) == "\\" then
        osType = "windows"
    else
        -- Try to check for macOS vs Linux
        local success, result = pcall(function()
            local f = io.popen("uname -s")
            if f then
                local uname = f:read("*a"):lower()
                f:close()
                if string.match(uname, "darwin") then
                    return "macos"
                elseif string.match(uname, "linux") then
                    return "linux"
                end
            end
            return "unix" -- Default to unix-like if we can't determine specifically
        end)

        if success then
            osType = result
        end
    end

    log("Detected OS: " .. osType)
    return osType
end

-- Parse tokens in naming pattern
function parseTokens(pattern, item, mediaPoolItem)
    if not pattern then return "" end

    local result = pattern

    -- Get clip properties and metadata
    local clipProps = {}

    -- Timeline item properties
    if item then
        local timelineClipName = ""
        pcall(function() timelineClipName = item:GetName() or "" end)
        clipProps["TimelineClipName"] = timelineClipName

        -- Try to get other timeline properties if available
        pcall(function()
            local record_frame = item:GetStart()
            clipProps["RecordFrame"] = tostring(record_frame or "")
        end)
    end

    -- Media pool item properties
    if mediaPoolItem then
        -- Basic properties
        pcall(function() clipProps["ClipName"] = mediaPoolItem:GetName() or "" end)

        -- Get file properties
        local fileProps = {"File Path", "FPS", "StartFrame", "Duration", "Format"}
        for _, prop in ipairs(fileProps) do
            pcall(function()
                local value = mediaPoolItem:GetClipProperty(prop)
                clipProps[string.gsub(prop, " ", "")] = value or ""
            end)
        end

        -- Metadata - try both direct metadata and GetMetadata
        local metadataItems = {"Shot", "Scene", "Take", "Description", "Comments", "Keyword", "DailyRoll"}
        for _, meta in ipairs(metadataItems) do
            pcall(function()
                local value = mediaPoolItem:GetMetadata(meta)
                clipProps[meta] = value or ""
            end)
        end

        -- Timecode
        pcall(function()
            clipProps["StartTC"] = mediaPoolItem:GetMetadata("Start TC") or ""
            clipProps["Timecode"] = mediaPoolItem:GetMetadata("Start TC") or "" -- Alias
        end)
    end

    -- Parse tokens - allow slashes for folder creation
    result = string.gsub(result, "%%([%w_]+)", function(token)
        local value = clipProps[token]
        if value then
            -- Convert empty strings to "None" if desired
            if value == "" then value = "" end
            -- Sanitize the token value but allow slashes
            return stripInvalidChars(value, false)
        else
            return ""
        end
    end)

    -- Only sanitize the non-token parts without removing slashes
    result = stripInvalidChars(result, true)

    return result
end

-- Get timeline items safely
function getTimelineItems()
    if not timeline then return {} end

    local items = {}

    -- Try different methods depending on Resolve version
    local success, result = pcall(function()
        -- First try to get items from all tracks
        if timeline.GetItemList then
            return timeline:GetItemList() or {}
        end

        -- Last resort: try to get items from all video tracks
        local allItems = {}
        local trackCount = timeline:GetTrackCount("video") or 0
        for i = 1, trackCount do
            local trackItems = nil
            pcall(function() trackItems = timeline:GetItemsInTrack("video", i) end)
            if trackItems and #trackItems > 0 then
                for _, item in ipairs(trackItems) do
                    table.insert(allItems, item)
                end
            end
        end
        return allItems
    end)

    if success and result then
        items = result
    end

    log("Found " .. #items .. " total timeline items")
    return items
end

-- Print available tokens
function printAvailableTokens()
    log("\n=== AVAILABLE TOKENS ===")
    log("Timeline Properties:")
    log("  %TimelineClipName - Name of the clip in the timeline")
    log("  %RecordFrame - Starting frame in the timeline")
    log("\nMedia Properties:")
    log("  %ClipName - Name of the clip in the media pool")
    log("  %FilePath - Full path to the source file")
    log("  %FPS - Frames per second")
    log("  %StartFrame - Starting frame of the clip")
    log("  %Duration - Duration of the clip")
    log("  %Format - File format")
    log("\nMetadata:")
    log("  %Shot - Shot metadata")
    log("  %Scene - Scene metadata")
    log("  %Take - Take metadata")
    log("  %Description - Description metadata")
    log("  %Comments - Comments metadata")
    log("  %Keyword - Keyword metadata")
    log("  %StartTC or %Timecode - Start timecode metadata")
    log("\nExample naming patterns:")
    log("  %Scene_%Shot_%TimelineClipName")
    log("  %Scene\\%Shot\\%TimelineClipName (creates subfolders)")
    log("===========================\n")
end

-- Create Windows batch file for a single operation
function createWindowsBatchFile(outputPath, targetFolder, operationType, timelineItems, namingPattern, uniqueClips, addSequenceNumbers)
    -- Open output file
    local file = io.open(outputPath, "w")
    if not file then
        log("Error: Cannot create output file at " .. outputPath)
        return false, 0
    end

    -- Write header
    file:write("@echo off\n")
    file:write("rem Media " .. operationType .. " creator for DaVinci Resolve\n")
    file:write("rem Generated by DaVinci Resolve Multi-Platform Link Creator\n\n")

    local opString = ""
    if operationType == "hardlink" then opString = "hardlinks"
    elseif operationType == "symlink" then opString = "symlinks"
    elseif operationType == "copy" then opString = "copies"
    end

    file:write("echo Creating " .. opString .. "...\n")
    file:write("echo Target folder: " .. targetFolder .. "\n")
    file:write("echo.\n\n")

    -- Ensure target directory exists
    file:write("if not exist \"" .. targetFolder .. "\" mkdir \"" .. targetFolder .. "\"\n\n")

    local count = 0
    local processed = {}
    local usedNames = {} -- Track used names to ensure uniqueness

    -- Process each timeline item
    for _, item in ipairs(timelineItems) do
        -- Safely get media pool item
        local mediaPoolItem = nil
        local success = pcall(function() mediaPoolItem = item:GetMediaPoolItem() end)

        if success and mediaPoolItem then
            -- Safely get clip path
            local clipPath = ""
            pcall(function() clipPath = mediaPoolItem:GetClipProperty("File Path") or "" end)

            if clipPath == "" then
                log("Warning: Could not get file path for clip")
                goto continue
            end

            -- Skip duplicates if uniqueClips is true
            if uniqueClips and processed[clipPath] then
                log("Skipping duplicate: " .. clipPath)
                goto continue
            end

            -- Get clip name for logging
            local clipName = ""
            pcall(function() clipName = item:GetName() or "" end)
            if clipName == "" then
                pcall(function() clipName = mediaPoolItem:GetName() or "Unknown" end)
            end

            -- Get file extension
            local extension = getFileExtension(clipPath)

            -- Parse tokens to generate the output path - allow slashes for subfolders
            local parsedPath = parseTokens(namingPattern, item, mediaPoolItem)

            -- Fallback if the pattern produces an empty result
            if parsedPath == "" then
                parsedPath = stripInvalidChars(clipName, false)
                if parsedPath == "" then
                    parsedPath = "Untitled"
                end
            end

            -- Add extension if not already in name
            if extension ~= "" and not string.match(parsedPath, "%." .. extension .. "$") then
                parsedPath = parsedPath .. "." .. extension
            end

            -- Handle duplicate filenames if enabled
            if addSequenceNumbers then
                if usedNames[parsedPath] then
                    local baseName, pathPart = parsedPath, ""

                    -- Preserve directory structure if present
                    local lastSlash = parsedPath:match(".*[/\\]")
                    if lastSlash then
                        pathPart = lastSlash
                        baseName = parsedPath:sub(#lastSlash + 1)
                    end

                    -- Remove extension for adding sequence number
                    local nameWithoutExt = baseName
                    if extension ~= "" then
                        nameWithoutExt = baseName:sub(1, -(#extension + 2)) -- Remove extension
                    end

                    usedNames[parsedPath] = usedNames[parsedPath] + 1
                    local index = usedNames[parsedPath]

                    -- Reconstruct full path with sequence number
                    if extension ~= "" then
                        parsedPath = pathPart .. nameWithoutExt .. "_" .. index .. "." .. extension
                    else
                        parsedPath = pathPart .. nameWithoutExt .. "_" .. index
                    end
                else
                    usedNames[parsedPath] = 1
                end
            end

            -- Create target path (Windows-style for batch file)
            local targetPath = targetFolder .. "\\" .. parsedPath:gsub("/", "\\")

            -- Create parent directory command for path with subfolders
            local targetDir = string.match(targetPath, "(.*\\)[^\\]+$")
            if targetDir then
                file:write("if not exist \"" .. targetDir .. "\" mkdir \"" .. targetDir .. "\"\n")
            end

            -- Write command based on operation type
            if operationType == "hardlink" then
                file:write("mklink /H \"" .. targetPath .. "\" \"" .. clipPath .. "\"\n")
            elseif operationType == "symlink" then
                file:write("mklink \"" .. targetPath .. "\" \"" .. clipPath .. "\"\n")
            elseif operationType == "copy" then
                file:write("copy \"" .. clipPath .. "\" \"" .. targetPath .. "\"\n")
            end

            count = count + 1
            processed[clipPath] = true
            log("Added " .. operationType .. " command for: " .. clipName)
        end
        ::continue::
    end

    -- Write summary
    file:write("\necho.\n")
    file:write("echo Created " .. count .. " " .. opString .. "\n")
    file:write("echo.\n")
    file:write("pause\n")

    -- Close file
    file:close()

    return true, count
end

-- Create macOS/Linux shell script for a single operation
function createUnixShellScript(outputPath, targetFolder, operationType, timelineItems, namingPattern, uniqueClips, addSequenceNumbers, isLinux)
    -- Open output file
    local file = io.open(outputPath, "w")
    if not file then
        log("Error: Cannot create output file at " .. outputPath)
        return false, 0
    end

    -- Write header
    file:write("#!/bin/bash\n\n")
    file:write("# Media " .. operationType .. " Creator for DaVinci Resolve\n")
    file:write("# Generated by DaVinci Resolve Multi-Platform Link Creator\n\n")

    local opString = ""
    if operationType == "hardlink" then opString = "hardlinks"
    elseif operationType == "symlink" then opString = "symlinks"
    elseif operationType == "copy" then opString = "copies"
    end

    file:write("echo \"Creating " .. opString .. "...\"\n")
    file:write("echo \"Target folder: " .. targetFolder .. "\"\n")
    file:write("echo\n\n")

    -- Ensure target directory exists
    file:write("# Create target directory if it doesn't exist\n")
    file:write("mkdir -p \"" .. targetFolder .. "\"\n\n")

    local count = 0
    local processed = {}
    local usedNames = {} -- Track used names to ensure uniqueness

    -- Process each timeline item
    for _, item in ipairs(timelineItems) do
        -- Safely get media pool item
        local mediaPoolItem = nil
        local success = pcall(function() mediaPoolItem = item:GetMediaPoolItem() end)

        if success and mediaPoolItem then
            -- Safely get clip path
            local clipPath = ""
            pcall(function() clipPath = mediaPoolItem:GetClipProperty("File Path") or "" end)

            if clipPath == "" then
                log("Warning: Could not get file path for clip")
                goto continue
            end

            -- Convert Windows paths to Unix if needed
            clipPath = clipPath:gsub("\\", "/")

            -- Skip duplicates if uniqueClips is true
            if uniqueClips and processed[clipPath] then
                log("Skipping duplicate: " .. clipPath)
                goto continue
            end

            -- Get clip name for logging
            local clipName = ""
            pcall(function() clipName = item:GetName() or "" end)
            if clipName == "" then
                pcall(function() clipName = mediaPoolItem:GetName() or "Unknown" end)
            end

            -- Get file extension
            local extension = getFileExtension(clipPath)

            -- Parse tokens to generate the output path - allow slashes for subfolders
            local parsedPath = parseTokens(namingPattern, item, mediaPoolItem)

            -- Fallback if the pattern produces an empty result
            if parsedPath == "" then
                parsedPath = stripInvalidChars(clipName, false)
                if parsedPath == "" then
                    parsedPath = "Untitled"
                end
            end

            -- Add extension if not already in name
            if extension ~= "" and not string.match(parsedPath, "%." .. extension .. "$") then
                parsedPath = parsedPath .. "." .. extension
            end

            -- Handle duplicate filenames if enabled
            if addSequenceNumbers then
                if usedNames[parsedPath] then
                    local baseName, pathPart = parsedPath, ""

                    -- Preserve directory structure if present
                    local lastSlash = parsedPath:match(".*(/)")
                    if lastSlash then
                        pathPart = lastSlash
                        baseName = parsedPath:sub(#lastSlash + 1)
                    end

                    -- Remove extension for adding sequence number
                    local nameWithoutExt = baseName
                    if extension ~= "" then
                        nameWithoutExt = baseName:sub(1, -(#extension + 2)) -- Remove extension
                    end

                    usedNames[parsedPath] = usedNames[parsedPath] + 1
                    local index = usedNames[parsedPath]

                    -- Reconstruct full path with sequence number
                    if extension ~= "" then
                        parsedPath = pathPart .. nameWithoutExt .. "_" .. index .. "." .. extension
                    else
                        parsedPath = pathPart .. nameWithoutExt .. "_" .. index
                    end
                else
                    usedNames[parsedPath] = 1
                end
            end

            -- Create target path (Unix-style for shell script)
            local targetPath = targetFolder .. "/" .. parsedPath:gsub("\\", "/")

            -- Create parent directory command for path with subfolders
            local targetDir = string.match(targetPath, "(.*)/[^/]+$")
            if targetDir then
                file:write("mkdir -p \"" .. targetDir .. "\"\n")
            end

            -- Write command based on operation type
            if operationType == "hardlink" then
                file:write("ln \"" .. clipPath .. "\" \"" .. targetPath .. "\"\n")
            elseif operationType == "symlink" then
                file:write("ln -s \"" .. clipPath .. "\" \"" .. targetPath .. "\"\n")
            elseif operationType == "copy" then
                file:write("cp \"" .. clipPath .. "\" \"" .. targetPath .. "\"\n")
            end

            count = count + 1
            processed[clipPath] = true
            log("Added " .. operationType .. " command for: " .. clipName)
        end
        ::continue::
    end

    -- Write summary
    file:write("\necho\n")
    file:write("echo \"Created " .. count .. " " .. opString .. "\"\n")
    file:write("echo\n")
    file:write("read -p \"Press Enter to continue...\"\n")

    -- Close file
    file:close()

    -- Make script executable (Unix only)
    if isLinux or getOperatingSystem() == "macos" then
        os.execute("chmod +x \"" .. outputPath .. "\"")
    end

    return true, count
end

-- Helper function to open folder using the operating system's file explorer
function openFolder(folderPath)
    local os = getOperatingSystem()
    local success = false

    if os == "windows" then
        success = pcall(function() os.execute('explorer "' .. folderPath .. '"') end)
    elseif os == "macos" then
        success = pcall(function() os.execute('open "' .. folderPath .. '"') end)
    elseif os == "linux" then
        -- Try several Linux file managers
        local commands = {
            'xdg-open "' .. folderPath .. '"',
            'nautilus "' .. folderPath .. '"',
            'dolphin "' .. folderPath .. '"',
            'thunar "' .. folderPath .. '"',
            'pcmanfm "' .. folderPath .. '"'
        }

        for _, cmd in ipairs(commands) do
            if pcall(function() os.execute(cmd .. " &>/dev/null &") end) then
                success = true
                break
            end
        end
    end

    if success then
        log("Opened folder: " .. folderPath)
    else
        log("Failed to open folder: " .. folderPath)
    end

    return success
end

-- Main UI function
function ShowUI()
    -- Initialize UI variables
    local fu = fu or fusion
    local ui = fu and fu.UIManager
    local disp = bmd.UIDispatcher(ui)
    local width, height = 600, 500  -- Increased size for more options

    -- Get home directory
    local homeDir = os.getenv("HOME") or os.getenv("USERPROFILE") or "C:\\"

    -- Detect current OS
    local currentOS = getOperatingSystem()

    -- Create window
    local win = disp:AddWindow({
        ID = "LinkCreatorWindow",
        WindowTitle = "Multi-Platform Link Creator",
        Geometry = {100, 100, width, height},
        Spacing = 10,

        ui:VGroup{
            ID = "root",
            -- Target folder selection - with more space
            ui:HGroup{
                ID = "targetFolder",
                ui:Label{
                    ID = "TargetFolderLabel",
                    Text = "Target Folder:",
                    MinimumSize = {100, 24}
                },
                ui:TextEdit{
                    ID = "TargetFolderEdit",
                    Text = homeDir .. ((currentOS == "windows") and "\\DaVinci_Links" or "/DaVinci_Links"),
                    PlaceholderText = "Path to destination folder",
                    MinimumSize = {450, 24}  -- Wider text field for better readability
                }
            },

            -- Naming pattern
            ui:HGroup{
                ID = "namingPattern",
                ui:Label{
                    ID = "NamingPatternLabel",
                    Text = "Naming Pattern:",
                    MinimumSize = {100, 24}
                },
                ui:TextEdit{
                    ID = "NamingPatternEdit",
                    Text = "%TimelineClipName",
                    PlaceholderText = "%TimelineClipName",
                    MinimumSize = {450, 24}  -- Wider text field for better readability
                }
            },

            -- Example pattern with subfolders
            ui:Label{
                ID = "PatternExample",
                Text = "Example subfolder pattern: " .. ((currentOS == "windows") and "%Scene\\%Shot\\%TimelineClipName" or "%Scene/%Shot/%TimelineClipName")
            },

            -- Help text for tokens
            ui:Label{
                ID = "TokenHelpLabel",
                Text = "Available tokens: %TimelineClipName, %ClipName, %Shot, %Scene, etc."
            },

            -- Operation type selection
            ui:Label{
                ID = "OperationsLabel",
                Text = "Operation Types:",
                Weight = 0
            },

            ui:HGroup{
                ID = "OperationsGroup",
                Weight = 0,
                ui:CheckBox{
                    ID = "HardlinkCheck",
                    Text = "Hardlinks",
                    Checked = true
                },
                ui:CheckBox{
                    ID = "SymlinkCheck",
                    Text = "Symlinks",
                    Checked = false
                },
                ui:CheckBox{
                    ID = "CopyCheck",
                    Text = "Copy Files",
                    Checked = false
                }
            },

            -- Platform selection
            ui:Label{
                ID = "PlatformLabel",
                Text = "Create scripts for:",
                Weight = 0
            },

            ui:HGroup{
                ID = "PlatformGroup",
                Weight = 0,
                ui:CheckBox{
                    ID = "WindowsCheck",
                    Text = "Windows (.bat)",
                    Checked = (currentOS == "windows")
                },
                ui:CheckBox{
                    ID = "MacOSCheck",
                    Text = "macOS (.sh)",
                    Checked = (currentOS == "macos")
                },
                ui:CheckBox{
                    ID = "LinuxCheck",
                    Text = "Linux (.sh)",
                    Checked = (currentOS == "linux")
                }
            },

            -- Additional options
            ui:CheckBox{
                ID = "UniqueClipsCheck",
                Text = "Only process unique clips (ignore duplicates)",
                Checked = true
            },

            ui:CheckBox{
                ID = "AddSequenceCheck",
                Text = "Add sequence numbers to duplicate filenames",
                Checked = true
            },

            ui:CheckBox{
                ID = "SaveInTargetCheck",
                Text = "Save scripts in target folder",
                Checked = true
            },

            ui:CheckBox{
                ID = "OpenFolderCheck",
                Text = "Open target folder after creating scripts",
                Checked = true
            },

            -- Status text
            ui:Label{
                ID = "StatusLabel",
                Text = "Ready to generate scripts."
            },

            -- Button group
            ui:HGroup{
                ID = "buttons",
                ui:Button{
                    ID = "CancelButton",
                    Text = "Cancel"
                },
                ui:Button{
                    ID = "GenerateButton",
                    Text = "Generate Scripts"
                }
            }
        }
    })

    -- Set up event handlers

    -- Track if we should proceed with generation
    local run_export = false

    -- Window close handler
    function win.On.LinkCreatorWindow.Close(ev)
        disp:ExitLoop()
        run_export = false
    end

    -- Cancel button handler
    function win.On.CancelButton.Clicked(ev)
        log("Cancel clicked")
        disp:ExitLoop()
        run_export = false
    end

    -- Generate button handler
    function win.On.GenerateButton.Clicked(ev)
        log("Generate clicked")
        run_export = true
        disp:ExitLoop()
    end

    -- Show window and run event loop
    win:Show()
    disp:RunLoop()
    win:Hide()

    -- If user clicked Generate, process the export
    if run_export then
        local itm = win:GetItems()

        -- Get values from UI
        local targetFolder = itm.TargetFolderEdit.PlainText
        local namingPattern = itm.NamingPatternEdit.PlainText

        -- Get operations
        local operations = {
            hardlink = itm.HardlinkCheck.Checked,
            symlink = itm.SymlinkCheck.Checked,
            copy = itm.CopyCheck.Checked
        }

        -- Get platforms
        local platforms = {
            windows = itm.WindowsCheck.Checked,
            macos = itm.MacOSCheck.Checked,
            linux = itm.LinuxCheck.Checked
        }

        -- Get other options
        local uniqueClips = itm.UniqueClipsCheck.Checked
        local addSequence = itm.AddSequenceCheck.Checked
        local saveInTarget = itm.SaveInTargetCheck.Checked
        local openFolder = itm.OpenFolderCheck.Checked

        -- Make sure at least one operation and platform is selected
        if not (operations.hardlink or operations.symlink or operations.copy) then
            log("Error: No operation type selected")
            itm.StatusLabel.Text = "Error: Please select at least one operation type (Hardlink, Symlink, or Copy)"
            return false
        end

        if not (platforms.windows or platforms.macos or platforms.linux) then
            log("Error: No platform selected")
            itm.StatusLabel.Text = "Error: Please select at least one platform (Windows, macOS, or Linux)"
            return false
        end

        log("Processing with settings:")
        log("- Target Folder: " .. targetFolder)
        log("- Naming Pattern: " .. namingPattern)
        log("- Operations: " ..
            (operations.hardlink and "Hardlink " or "") ..
            (operations.symlink and "Symlink " or "") ..
            (operations.copy and "Copy " or ""))
        log("- Platforms: " ..
            (platforms.windows and "Windows " or "") ..
            (platforms.macos and "macOS " or "") ..
            (platforms.linux and "Linux " or ""))
        log("- Unique Clips Only: " .. tostring(uniqueClips))
        log("- Add Sequence Numbers: " .. tostring(addSequence))
        log("- Save in Target: " .. tostring(saveInTarget))
        log("- Open Folder: " .. tostring(openFolder))

        -- Check if timeline is available
        if not timeline then
            log("Error: No timeline is open")
            itm.StatusLabel.Text = "Error: No timeline is open"
            return false
        end

        -- Get timeline items
        local timelineItems = getTimelineItems()
        if #timelineItems == 0 then
            log("Error: No clips found in timeline")
            itm.StatusLabel.Text = "Error: No clips found in timeline"
            return false
        end

        -- Prepare output paths
        local baseDir = saveInTarget and targetFolder or homeDir
        local scriptPaths = {}
        local successCount = 0

        -- Replace backslashes in baseDir with forward slashes for consistency
        baseDir = baseDir:gsub("\\", "/")

        -- Ensure target directory exists if saving there
        if saveInTarget then
            -- Create target directory if it doesn't exist
            if currentOS == "windows" then
                os.execute('if not exist "' .. targetFolder .. '" mkdir "' .. targetFolder .. '"')
            else
                os.execute('mkdir -p "' .. targetFolder .. '"')
            end
        end

        -- Track success status for different platforms and operations
        local platformStatus = {
            windows = false,
            macos = false,
            linux = false
        }

        -- Keep track of successful scripts
        local createdScripts = {}

        -- Create Windows scripts if selected
        if platforms.windows then
            -- Create separate script for each operation
            if operations.hardlink then
                local winFileName = "resolve_hardlink.bat"
                local winPath = baseDir .. (baseDir:match("/$") and "" or "/") .. winFileName
                winPath = winPath:gsub("/", "\\") -- Convert to Windows path style

                local success, count = createWindowsBatchFile(
                    winPath,
                    targetFolder,
                    "hardlink",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence
                )

                if success then
                    log("Windows hardlink batch file created successfully at: " .. winPath)
                    log("Will create " .. count .. " hardlinks")
                    platformStatus.windows = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, winPath)
                    table.insert(createdScripts, "Windows hardlink script")
                else
                    log("Error creating Windows hardlink batch file")
                end
            end

            if operations.symlink then
                local winFileName = "resolve_symlink.bat"
                local winPath = baseDir .. (baseDir:match("/$") and "" or "/") .. winFileName
                winPath = winPath:gsub("/", "\\") -- Convert to Windows path style

                local success, count = createWindowsBatchFile(
                    winPath,
                    targetFolder,
                    "symlink",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence
                )

                if success then
                    log("Windows symlink batch file created successfully at: " .. winPath)
                    log("Will create " .. count .. " symlinks")
                    platformStatus.windows = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, winPath)
                    table.insert(createdScripts, "Windows symlink script")
                else
                    log("Error creating Windows symlink batch file")
                end
            end

            if operations.copy then
                local winFileName = "resolve_copy.bat"
                local winPath = baseDir .. (baseDir:match("/$") and "" or "/") .. winFileName
                winPath = winPath:gsub("/", "\\") -- Convert to Windows path style

                local success, count = createWindowsBatchFile(
                    winPath,
                    targetFolder,
                    "copy",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence
                )

                if success then
                    log("Windows copy batch file created successfully at: " .. winPath)
                    log("Will create " .. count .. " file copies")
                    platformStatus.windows = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, winPath)
                    table.insert(createdScripts, "Windows copy script")
                else
                    log("Error creating Windows copy batch file")
                end
            end
        end

        -- Create macOS scripts if selected
        if platforms.macos then
            -- Create separate script for each operation
            if operations.hardlink then
                local macFileName = "resolve_hardlink_mac.sh"
                local macPath = baseDir .. (baseDir:match("/$") and "" or "/") .. macFileName
                macPath = macPath:gsub("\\", "/") -- Convert to Unix path style

                local success, count = createUnixShellScript(
                    macPath,
                    targetFolder,
                    "hardlink",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence,
                    false -- Not Linux
                )

                if success then
                    log("macOS hardlink shell script created successfully at: " .. macPath)
                    log("Will create " .. count .. " hardlinks")
                    platformStatus.macos = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, macPath)
                    table.insert(createdScripts, "macOS hardlink script")
                else
                    log("Error creating macOS hardlink shell script")
                end
            end

            if operations.symlink then
                local macFileName = "resolve_symlink_mac.sh"
                local macPath = baseDir .. (baseDir:match("/$") and "" or "/") .. macFileName
                macPath = macPath:gsub("\\", "/") -- Convert to Unix path style

                local success, count = createUnixShellScript(
                    macPath,
                    targetFolder,
                    "symlink",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence,
                    false -- Not Linux
                )

                if success then
                    log("macOS symlink shell script created successfully at: " .. macPath)
                    log("Will create " .. count .. " symlinks")
                    platformStatus.macos = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, macPath)
                    table.insert(createdScripts, "macOS symlink script")
                else
                    log("Error creating macOS symlink shell script")
                end
            end

            if operations.copy then
                local macFileName = "resolve_copy_mac.sh"
                local macPath = baseDir .. (baseDir:match("/$") and "" or "/") .. macFileName
                macPath = macPath:gsub("\\", "/") -- Convert to Unix path style

                local success, count = createUnixShellScript(
                    macPath,
                    targetFolder,
                    "copy",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence,
                    false -- Not Linux
                )

                if success then
                    log("macOS copy shell script created successfully at: " .. macPath)
                    log("Will create " .. count .. " file copies")
                    platformStatus.macos = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, macPath)
                    table.insert(createdScripts, "macOS copy script")
                else
                    log("Error creating macOS copy shell script")
                end
            end
        end

        -- Create Linux scripts if selected
        if platforms.linux then
            -- Create separate script for each operation
            if operations.hardlink then
                local linuxFileName = "resolve_hardlink_linux.sh"
                local linuxPath = baseDir .. (baseDir:match("/$") and "" or "/") .. linuxFileName
                linuxPath = linuxPath:gsub("\\", "/") -- Convert to Unix path style

                local success, count = createUnixShellScript(
                    linuxPath,
                    targetFolder,
                    "hardlink",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence,
                    true -- Is Linux
                )

                if success then
                    log("Linux hardlink shell script created successfully at: " .. linuxPath)
                    log("Will create " .. count .. " hardlinks")
                    platformStatus.linux = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, linuxPath)
                    table.insert(createdScripts, "Linux hardlink script")
                else
                    log("Error creating Linux hardlink shell script")
                end
            end

            if operations.symlink then
                local linuxFileName = "resolve_symlink_linux.sh"
                local linuxPath = baseDir .. (baseDir:match("/$") and "" or "/") .. linuxFileName
                linuxPath = linuxPath:gsub("\\", "/") -- Convert to Unix path style

                local success, count = createUnixShellScript(
                    linuxPath,
                    targetFolder,
                    "symlink",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence,
                    true -- Is Linux
                )

                if success then
                    log("Linux symlink shell script created successfully at: " .. linuxPath)
                    log("Will create " .. count .. " symlinks")
                    platformStatus.linux = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, linuxPath)
                    table.insert(createdScripts, "Linux symlink script")
                else
                    log("Error creating Linux symlink shell script")
                end
            end

            if operations.copy then
                local linuxFileName = "resolve_copy_linux.sh"
                local linuxPath = baseDir .. (baseDir:match("/$") and "" or "/") .. linuxFileName
                linuxPath = linuxPath:gsub("\\", "/") -- Convert to Unix path style

                local success, count = createUnixShellScript(
                    linuxPath,
                    targetFolder,
                    "copy",
                    timelineItems,
                    namingPattern,
                    uniqueClips,
                    addSequence,
                    true -- Is Linux
                )

                if success then
                    log("Linux copy shell script created successfully at: " .. linuxPath)
                    log("Will create " .. count .. " file copies")
                    platformStatus.linux = true
                    successCount = successCount + 1
                    table.insert(scriptPaths, linuxPath)
                    table.insert(createdScripts, "Linux copy script")
                else
                    log("Error creating Linux copy shell script")
                end
            end
        end

        -- Open target folder if requested
        if openFolder and successCount > 0 then
            local folderToOpen = saveInTarget and targetFolder or baseDir
            openFolder(folderToOpen)
        end

        -- Update status message
        local statusMessage = ""
        if successCount > 0 then
            statusMessage = "Created " .. successCount .. " script"
            if successCount > 1 then statusMessage = statusMessage .. "s" end
            statusMessage = statusMessage .. ": " .. table.concat(createdScripts, ", ")
        else
            statusMessage = "Error: Failed to create any scripts"
        end

        itm.StatusLabel.Text = statusMessage
        return (successCount > 0)
    end

    return false
end

-- Main script execution
function Main()
    -- Display version info
    log("DaVinci Resolve Multi-Platform Link Creator v1.0.0")

    -- Check Resolve API access
    if not resolve then
        log("Error: Unable to access Resolve API")
        return
    end

    if not project then
        log("Error: No project is currently open")
        return
    end

    if not timeline then
        log("Error: No timeline is currently open")
        return
    end

    -- Print available tokens
    printAvailableTokens()

    -- Show UI and process
    ShowUI()
end

-- Start the script
Main()