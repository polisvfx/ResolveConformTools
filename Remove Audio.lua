--
-- Remove Audio - part of ResolveConformTools
-- Copyright (C) 2026 Maris
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU General Public License as published by
-- the Free Software Foundation, either version 3 of the License, or
-- (at your option) any later version.
--
-- This program is distributed in the hope that it will be useful,
-- but WITHOUT ANY WARRANTY; without even the implied warranty of
-- MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
-- GNU General Public License for more details.
--
-- You should have received a copy of the GNU General Public License
-- along with this program.  If not, see <https://www.gnu.org/licenses/>.
--
-- SPDX-License-Identifier: GPL-3.0-or-later
--
--[[
    Script to remove all audio tracks from the active timeline in DaVinci Resolve.
    Requires DaVinci Resolve Studio.
    Version: 1.0
--]]

function main()
    -- Get resolve object
    local resolve = bmd.scriptapp("Resolve")
    if resolve == nil then
        print("Error: Unable to connect to DaVinci Resolve.")
        return
    end

    -- Get the project manager
    local projectManager = resolve:GetProjectManager()
    if projectManager == nil then
        print("Error: Unable to get Project Manager.")
        return
    end

    -- Get the current project
    local project = projectManager:GetCurrentProject()
    if project == nil then
        print("Error: No project is currently open.")
        return
    end

    -- Get the active timeline
    local timeline = project:GetCurrentTimeline()
    if timeline == nil then
        print("Error: No timeline is currently active.")
        return
    end

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

-- Call the main function
main()