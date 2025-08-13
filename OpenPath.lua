-- Cross-Platform Folder Opener for Davinci Resolve
local function openPath(path)
    -- Determine the operating system
    local osName = resolve and resolve.getOSName and resolve.getOSName() or ""
    osName = string.lower(osName)

    -- Normalize path separators
    if osName:match("windows") then
        -- Windows: use backslashes
        path = path:gsub("/", "\\")
    else
        -- macOS and Linux: use forward slashes
        path = path:gsub("\\", "/")
    end

    -- Platform-specific open commands
    local openCommands = {
        windows = string.format('start "" "%s"', path),
        mac = string.format('open "%s"', path),
        linux = string.format('xdg-open "%s"', path)
    }

    -- Determine which command to use
    local command
    if osName:match("windows") then
        command = openCommands.windows
    elseif osName:match("mac") then
        command = openCommands.mac
    elseif osName:match("linux") then
        command = openCommands.linux
    else
        -- Fallback detection if resolve.getOSName fails
        local osResult = io.popen("uname -s"):read('*l')
        if osResult then
            osResult = string.lower(osResult)
            if osResult:match("darwin") then
                command = openCommands.mac
            elseif osResult:match("linux") then
                command = openCommands.linux
            else
                print("Unsupported operating system")
                return false
            end
        else
            -- Last resort, try to detect from package.config
            local isWindows = package.config:sub(1,1) == '\\'
            command = isWindows and openCommands.windows or openCommands.mac
        end
    end

    -- Execute the command
    print("Opening path: " .. path)
    print("Using command: " .. command)
    
    local success, result = pcall(function()
        return os.execute(command)
    end)

    if success then
        print("Path opened successfully")
        return true
    else
        print("Failed to open path")
        print("Error: " .. tostring(result))
        return false
    end
end

-- Example usage: Replace with your desired path
-- Windows example
local windowsPath = [[E:\Work_Offline\Tempomedia_Wholey_Jan25\shottest]]

-- macOS example
local macPath = [[/Users/username/Documents/Work]]

-- Linux example
local linuxPath = [[/home/username/Documents/Work]]

-- Open the specific path (uncomment the one you want to use)
openPath(windowsPath)
-- openPath(macPath)
-- openPath(linuxPath)