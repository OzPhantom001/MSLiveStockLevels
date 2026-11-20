-- ==============================================================================
-- Main Sequence — Crash-Safe Live Stock & Machine Telemetry Exporter (UE4SS)
-- ==============================================================================

local function json_escape(str)
    str = tostring(str)
    str = str:gsub('\\', '\\\\')
    str = str:gsub('"', '\\"')
    str = str:gsub('\n', ' ')
    str = str:gsub('\r', '')
    return str
end

-- Filters out CDOs, reinstanced blueprints, trash objects, and transient world items
local function is_live_world_object(obj)
    if not obj or not obj:IsValid() then return false end
    local ok, full_name = pcall(function() return obj:GetFullName() end)
    if not ok or not full_name then return false end

    if string.find(full_name, "Default__", 1, true)
        or string.find(full_name, "REINST_", 1, true)
        or string.find(full_name, "TRASH_", 1, true)
        or string.find(full_name, "/Engine/Transient", 1, true) then
        return false
    end
    return true
end

local function get_clean_obj_name(obj)
    if not obj or not obj:IsValid() then
        return "None"
    end
    local ok, name = pcall(function() return obj:GetFName():ToString() end)
    if ok and name and name ~= "" then
        return name
    end
    return "None"
end

local function extract_text_value(val)
    if val == nil then return nil end

    local str = nil
    if type(val) == "string" then
        str = val
    elseif type(val) == "userdata" then
        local ok_str, s = pcall(function() return val:ToString() end)
        if ok_str and s then str = s end
    end

    if str and str ~= "" and str ~= "None" and not string.find(str, "BP_StationObject", 1, true) then
        return str
    end
    return nil
end

local function try_read_blueprint_name(station_obj, struct_prop_name)
    if not is_live_world_object(station_obj) then return nil end

    local ok_struct, bp_struct = pcall(function() return station_obj[struct_prop_name] end)
    if not ok_struct or bp_struct == nil then return nil end

    local ok_name, display_name = pcall(function() return bp_struct.DisplayName end)
    if not ok_name then return nil end

    return extract_text_value(display_name)
end

local function resolve_station_display_name(station_obj, fallback_id)
    if not is_live_world_object(station_obj) then
        return fallback_id
    end

    local bp_name = try_read_blueprint_name(station_obj, "Blueprint")
    if bp_name then return bp_name end

    local pending_name = try_read_blueprint_name(station_obj, "PendingBlueprint")
    if pending_name then return pending_name end

    return fallback_id
end

local function find_owning_station(obj)
    local curr = obj
    local depth = 0
    while curr and curr:IsValid() and depth < 12 do
        local name = get_clean_obj_name(curr)
        if string.find(name, "BP_StationObject", 1, true) then
            return curr, name, resolve_station_display_name(curr, name)
        end
        local ok, outer = pcall(function() return curr:GetOuter() end)
        if not ok then break end
        curr = outer
        depth = depth + 1
    end
    return nil, "Unassigned_Station", "Unassigned Station"
end

local function safe_primitive(obj, prop_name, default_val)
    local ok, val = pcall(function() return obj[prop_name] end)
    if ok and val ~= nil then
        return val
    end
    return default_val
end

-- Reads a numeric property that may be a plain number OR a fixed-point struct like (Value=123)
local function safe_number(obj, prop_name, default_val)
    local ok, val = pcall(function() return obj[prop_name] end)
    if not ok or val == nil then return default_val end
    local n = tonumber(val)
    if n then return n end
    local ok_v, inner = pcall(function() return val.Value end)
    if ok_v then
        n = tonumber(inner)
        if n then return n end
    end
    return default_val
end

-- Reads recipe input/output item stack arrays safely from a Recipe object
local function read_recipe_stacks(rec_obj, prop_names)
    local result = {}
    if not rec_obj or not rec_obj:IsValid() then return result end

    for _, pname in ipairs(prop_names) do
        local ok_arr, arr = pcall(function() return rec_obj[pname] end)
        if ok_arr and arr then
            pcall(function()
                arr:ForEach(function(_, elem)
                    pcall(function()
                        local entry = elem
                        if elem.get then entry = elem:get() end
                        if entry and entry.Item and entry.Item:IsValid() then
                            local iname = get_clean_obj_name(entry.Item)
                            local iamt = math.floor(tonumber(entry.Amount) or 1)
                            if iname ~= "None" and iamt > 0 then
                                result[iname] = (result[iname] or 0) + iamt
                            end
                        end
                    end)
                end)
            end)
            if next(result) ~= nil then
                break
            end
        end
    end
    return result
end

-- Reads per-item LIMIT values from the container actor's LogisticFilters (0x1D0):
--   array of (ItemStack=(Item, Amount), Action=<enum>)
-- Action omitted/0 = LIMIT (default enum value); Action=BUFFER entries are ignored.
local function read_container_logistic_limits(container_obj)
    local limits = {}
    if not is_live_world_object(container_obj) then
        return limits
    end

    local ok_lf, filters = pcall(function() return container_obj.LogisticFilters end)
    if not ok_lf or not filters then
        return limits
    end

    pcall(function()
        filters:ForEach(function(_, elem)
            pcall(function()
                local entry = elem
                if elem.get then entry = elem:get() end

                -- Skip non-LIMIT entries (BUFFER)
                local ok_a, action = pcall(function() return entry.Action end)
                if ok_a and action ~= nil then
                    local an = tonumber(action)
                    if an ~= nil then
                        if an ~= 0 then return end
                    elseif string.find(tostring(action), "BUFFER", 1, true) then
                        return
                    end
                end

                local stack = entry.ItemStack
                if stack and stack.Item and stack.Item:IsValid() then
                    local name = get_clean_obj_name(stack.Item)
                    local amt = math.floor(tonumber(stack.Amount) or 0)
                    if name ~= "None" and name ~= "Unknown" and amt > 0 then
                        limits[name] = (limits[name] or 0) + amt
                    end
                end
            end)
        end)
    end)

    return limits
end

-- Reads an inventory's contents from ItemTotals (0x88): array of { Item, Amount }.
-- Returns: items (item_name -> amount), expected (array of item names from ItemFilters).
-- ItemFilters (0x98) is only touched when read_expected is true (machine inventories),
-- since the container path deliberately avoids it.
local function read_inventory_totals_safe(inv_obj, read_expected)
    local items = {}
    local expected = {}
    if not is_live_world_object(inv_obj) then
        return items, expected
    end

    local ok_tot, totals = pcall(function() return inv_obj.ItemTotals end)
    if ok_tot and totals then
        pcall(function()
            totals:ForEach(function(_, elem)
                pcall(function()
                    local entry = elem
                    if elem.get then entry = elem:get() end
                    local item = entry.Item
                    if item and item:IsValid() then
                        local name = get_clean_obj_name(item)
                        local amt = tonumber(entry.Amount) or 0
                        if name ~= "None" and amt > 0 then
                            items[name] = (items[name] or 0) + math.floor(amt)
                        end
                    end
                end)
            end)
        end)
    end

    if read_expected then
        local ok_flt, filters = pcall(function() return inv_obj.ItemFilters end)
        if ok_flt and filters then
            pcall(function()
                filters:ForEach(function(_, elem)
                    pcall(function()
                        local flt = elem
                        if elem.get then flt = elem:get() end
                        if flt and flt.bHasFilter and flt.FilterItem and flt.FilterItem:IsValid() then
                            local n = get_clean_obj_name(flt.FilterItem)
                            if n ~= "None" then table.insert(expected, n) end
                        end
                    end)
                end)
            end)
        end
    end

    return items, expected
end

local skipped_owners_logged = {} -- log each skipped owner class only once per game session

local function export_stock_to_json()
    local inventories = FindAllOf("Inventory")
    if not inventories then
        print("[LiveStockLevels] No Inventory instances found.\n")
        return
    end

    local global_totals = {}
    local station_totals = {}
    local station_limits = {}
    local station_limit_stock = {} -- stock held ONLY in containers that carry a limit for that item
    local station_names = {}
    local container_count = 0

    -- Index machine inventories by their parent machine object pointer/name
    -- so we never have to dereference ObjectProperty fields on AssemblerObject
    local machine_inv_map = {}
    local unmatched_owners = {} -- owner class name -> count (diagnostic: inventories we did not export)

    for _, inv in ipairs(inventories) do
        if is_live_world_object(inv) then
            local parent = nil
            local ok_outer, out_obj = pcall(function() return inv:GetOuter() end)
            if ok_outer then parent = out_obj end

            if is_live_world_object(parent) then
                local parent_name = get_clean_obj_name(parent)
                local inv_name = get_clean_obj_name(inv)

                -- 1. Storage containers & direct station inventories
                if string.find(parent_name, "BP_ContainerObject", 1, true)
                    or string.find(parent_name, "BP_StationObject", 1, true) then

                    local is_empty = safe_primitive(inv, "bEmpty", true) == true
                    if not is_empty then
                        local _, station_id, station_label = find_owning_station(parent)
                        container_count = container_count + 1
                        station_totals[station_id] = station_totals[station_id] or {}
                        station_limits[station_id] = station_limits[station_id] or {}
                        station_limit_stock[station_id] = station_limit_stock[station_id] or {}
                        station_names[station_id] = station_label

                        local item_limits = read_container_logistic_limits(parent)

                        local items, _ = read_inventory_totals_safe(inv)
                        for item_name, amount in pairs(items) do
                            if amount > 0 then
                                global_totals[item_name] = (global_totals[item_name] or 0) + amount
                                station_totals[station_id][item_name] = (station_totals[station_id][item_name] or 0) + amount
                                local lim = item_limits[item_name]
                                if lim and lim > 0 then
                                    station_limits[station_id][item_name] = (station_limits[station_id][item_name] or 0) + lim
                                    station_limit_stock[station_id][item_name] = (station_limit_stock[station_id][item_name] or 0) + amount
                                end
                            end
                        end
                    end

                -- 2. Machine inventories (Assemblers, Smelters, etc.) discovered via Outer!
                elseif string.find(parent_name, "AssemblerObject", 1, true)
                    or string.find(parent_name, "CrafterObject", 1, true)
                    or string.find(parent_name, "SmelterObject", 1, true)
                    or string.find(parent_name, "BPO_AdvancedAssembler", 1, true)
                    or string.find(parent_name, "SciLab", 1, true)
                    or string.find(parent_name, "RelicAnalyzer", 1, true)
                    or string.find(parent_name, "RelicSynthesizer", 1, true)
                    or ((string.find(parent_name, "Object_C", 1, true) or string.find(parent_name, "BPO_", 1, true))
                        and safe_primitive(parent, "MaxProgress", nil) ~= nil) then

                    local entry = machine_inv_map[parent_name]
                    if not entry then
                        local _, station_id, station_label = find_owning_station(parent)
                        station_names[station_id] = station_label
                        entry = {
                            obj = parent,
                            id = parent_name,
                            station_id = station_id,
                            inv_list = {}
                        }
                        machine_inv_map[parent_name] = entry
                    end

                    local items, expected = read_inventory_totals_safe(inv, true)
                    local inv_id = tonumber(safe_primitive(inv, "ID", 0)) or 0
                    table.insert(entry.inv_list, {
                        inv_id = inv_id,
                        inv_name = inv_name,
                        items = items,
                        expected = expected
                    })
                else
                    local base = string.gsub(parent_name, "_C_%d+$", "")
                    unmatched_owners[base] = (unmatched_owners[base] or 0) + 1
                end
            end
        end
    end

    -- Diagnostic: inventory owners that were NOT exported (find Advanced Assembler / Science Lab class names here)
    for owner, cnt in pairs(unmatched_owners) do
        if not skipped_owners_logged[owner] then
            skipped_owners_logged[owner] = true
            print(string.format("[LiveStockLevels] Skipped inventory owner: %s (x%d)\n", owner, cnt))
        end
    end

    -- Build machine records safely using primitive properties + matched inventories
    local machines = {}
    for _, entry in pairs(machine_inv_map) do
        local m = entry.obj
        if is_live_world_object(m) then
            local progress = math.floor(safe_number(m, "Progress", 0))
            local max_progress = math.floor(safe_number(m, "MaxProgress", 0))
            local b_working = safe_primitive(m, "bWorking", false) == true
            local b_prog_complete = safe_primitive(m, "bProgressComplete", false) == true
            local b_manual = safe_primitive(m, "bManualMode", false) == true
            local b_has_error = safe_primitive(m, "bHasError", false) == true
            local overspeed = math.floor(safe_number(m, "OverSpeedChange", 0))
            local power_use = tonumber(safe_primitive(m, "PowerConsumptionRate", 0)) or 0
            local in_inv_id = tonumber(safe_primitive(m, "InputInventoryID", 0)) or 0
            local out_inv_id = tonumber(safe_primitive(m, "OutputInventoryID", 0)) or 0

            -- Safe Recipe FName + Input/Output Ratio Read
            local recipe_name = "None"
            local recipe_inputs = {}
            local recipe_outputs = {}
            local ok_rec, rec_obj = pcall(function() return m.Recipe end)
            if ok_rec and rec_obj and rec_obj:IsValid() then
                recipe_name = get_clean_obj_name(rec_obj)
                recipe_inputs = read_recipe_stacks(rec_obj, {"Inputs", "InputItems", "Ingredients", "RequiredItems"})
                recipe_outputs = read_recipe_stacks(rec_obj, {"Outputs", "OutputItems", "Results", "ProducedItems"})
            end

            -- Match input vs output inventory by ID (InputInventoryID / OutputInventoryID)
            local inputs = {}
            local expected_inputs = {}
            local outputs = {}

            table.sort(entry.inv_list, function(a, b) return a.inv_id < b.inv_id end)
            for idx, inv_data in ipairs(entry.inv_list) do
                if (in_inv_id > 0 and inv_data.inv_id == in_inv_id) or (in_inv_id == 0 and idx == 1) then
                    inputs = inv_data.items
                    expected_inputs = inv_data.expected
                elseif (out_inv_id > 0 and inv_data.inv_id == out_inv_id) or (out_inv_id == 0 and idx == 2) then
                    outputs = inv_data.items
                end
            end

            table.insert(machines, {
                id = entry.id,
                name = entry.id,
                station_id = entry.station_id,
                recipe = recipe_name,
                working_item = "None",
                progress = progress,
                max_progress = max_progress,
                b_working = b_working,
                b_progress_complete = b_prog_complete,
                b_manual = b_manual,
                b_has_error = b_has_error,
                overspeed = overspeed,
                power_use = power_use,
                inputs = inputs,
                expected_inputs = expected_inputs,
                outputs = outputs,
                recipe_inputs = recipe_inputs,
                recipe_outputs = recipe_outputs
            })
        end
    end

    table.sort(machines, function(a, b)
        if a.station_id == b.station_id then return a.id < b.id end
        return a.station_id < b.station_id
    end)

    local file = io.open("live_stock_levels.json", "w")
    if not file then
        print("[LiveStockLevels] ERROR: Could not open live_stock_levels.json for writing.\n")
        return
    end

    file:write("{\n")
    file:write(string.format('  "timestamp": "%s",\n', os.date("%Y-%m-%d %H:%M:%S")))
    file:write(string.format('  "active_containers": %d,\n', container_count))
    file:write(string.format('  "active_machines": %d,\n', #machines))

    file:write('  "station_names": {\n')
    local n_lines = {}
    for st_id, st_label in pairs(station_names) do
        table.insert(n_lines, string.format('    "%s": "%s"', json_escape(st_id), json_escape(st_label)))
    end
    table.sort(n_lines)
    file:write(table.concat(n_lines, ",\n") .. "\n  },\n")

    file:write('  "global_stock": {\n')
    local g_lines = {}
    for item, qty in pairs(global_totals) do
        table.insert(g_lines, string.format('    "%s": %d', json_escape(item), qty))
    end
    table.sort(g_lines)
    file:write(table.concat(g_lines, ",\n") .. "\n  },\n")

    file:write('  "by_station": {\n')
    local s_blocks = {}
    for st_id, items in pairs(station_totals) do
        local i_lines = {}
        for item, qty in pairs(items) do
            table.insert(i_lines, string.format('      "%s": %d', json_escape(item), qty))
        end
        table.sort(i_lines)
        table.insert(s_blocks, string.format('    "%s": {\n%s\n    }', json_escape(st_id), table.concat(i_lines, ",\n")))
    end
    table.sort(s_blocks)
    file:write(table.concat(s_blocks, ",\n") .. "\n  },\n")

    file:write('  "station_limits": {\n')
    local lim_blocks = {}
    for st_id, items in pairs(station_limits) do
        local l_lines = {}
        for item, lim in pairs(items) do
            table.insert(l_lines, string.format('      "%s": %d', json_escape(item), lim))
        end
        table.sort(l_lines)
        if #l_lines > 0 then
            table.insert(lim_blocks, string.format('    "%s": {\n%s\n    }', json_escape(st_id), table.concat(l_lines, ",\n")))
        end
    end
    table.sort(lim_blocks)
    file:write(table.concat(lim_blocks, ",\n") .. "\n  },\n")

    file:write('  "station_limit_stock": {\n')
    local ls_blocks = {}
    for st_id, items in pairs(station_limit_stock) do
        local l_lines = {}
        for item, qty in pairs(items) do
            table.insert(l_lines, string.format('      "%s": %d', json_escape(item), qty))
        end
        table.sort(l_lines)
        if #l_lines > 0 then
            table.insert(ls_blocks, string.format('    "%s": {\n%s\n    }', json_escape(st_id), table.concat(l_lines, ",\n")))
        end
    end
    table.sort(ls_blocks)
    file:write(table.concat(ls_blocks, ",\n") .. "\n  },\n")

    file:write('  "machines": [\n')
    local m_blocks = {}
    for _, m in ipairs(machines) do
        local in_lines = {}
        for k, v in pairs(m.inputs) do
            table.insert(in_lines, string.format('"%s": %d', json_escape(k), v))
        end
        table.sort(in_lines)

        local exp_lines = {}
        for _, k in ipairs(m.expected_inputs) do
            table.insert(exp_lines, string.format('"%s"', json_escape(k)))
        end

        local out_lines = {}
        for k, v in pairs(m.outputs) do
            table.insert(out_lines, string.format('"%s": %d', json_escape(k), v))
        end
        table.sort(out_lines)

        local rec_in_lines = {}
        for k, v in pairs(m.recipe_inputs) do
            table.insert(rec_in_lines, string.format('"%s": %d', json_escape(k), v))
        end
        table.sort(rec_in_lines)

        local rec_out_lines = {}
        for k, v in pairs(m.recipe_outputs) do
            table.insert(rec_out_lines, string.format('"%s": %d', json_escape(k), v))
        end
        table.sort(rec_out_lines)

        local block = string.format(
            '    {\n' ..
            '      "id": "%s",\n' ..
            '      "name": "%s",\n' ..
            '      "station_id": "%s",\n' ..
            '      "recipe": "%s",\n' ..
            '      "working_item": "%s",\n' ..
            '      "progress": %d,\n' ..
            '      "max_progress": %d,\n' ..
            '      "b_working": %s,\n' ..
            '      "b_progress_complete": %s,\n' ..
            '      "b_manual": %s,\n' ..
            '      "b_has_error": %s,\n' ..
            '      "overspeed": %d,\n' ..
            '      "power_use": %d,\n' ..
            '      "inputs": {%s},\n' ..
            '      "expected_inputs": [%s],\n' ..
            '      "outputs": {%s},\n' ..
            '      "recipe_inputs": {%s},\n' ..
            '      "recipe_outputs": {%s}\n' ..
            '    }',
            json_escape(m.id),
            json_escape(m.name),
            json_escape(m.station_id),
            json_escape(m.recipe),
            json_escape(m.working_item),
            m.progress,
            m.max_progress,
            tostring(m.b_working),
            tostring(m.b_progress_complete),
            tostring(m.b_manual),
            tostring(m.b_has_error),
            m.overspeed,
            m.power_use,
            table.concat(in_lines, ", "),
            table.concat(exp_lines, ", "),
            table.concat(out_lines, ", "),
            table.concat(rec_in_lines, ", "),
            table.concat(rec_out_lines, ", ")
        )
        table.insert(m_blocks, block)
    end
    file:write(table.concat(m_blocks, ",\n") .. "\n  ]\n")
    file:write("}\n")
    file:close()

    -- print(string.format("[LiveStockLevels] Exported %d containers and %d machines.\n", container_count, #machines))
end

RegisterKeyBind(Key.F9, function()
    ExecuteInGameThread(function()
        export_stock_to_json()
    end)
end)

-- Set to false to disable the 10 s background export (F9 still exports on demand).
-- Useful for isolating crashes: if the game stops crashing with this false, the periodic export is involved.
local AUTO_EXPORT = true

if AUTO_EXPORT then
    LoopAsync(10000, function()
        ExecuteInGameThread(function()
            export_stock_to_json()
        end)
        return false
    end)
end
