# Main Sequence – Live Stock & Production Monitor

Two parts that work together:

| File | What it does | Where it runs |
|---|---|---|
| `main.lua` | UE4SS mod. Reads your stations, containers and machines and writes `live_stock_levels.json` every 10 s (or on **F9**). | Inside the game |
| `live_stock_monitor.py` | Desktop window that reads that JSON and shows stock, bottlenecks, and the 100 %-efficiency material balance. | On your PC, separate from the game |

The Python app never talks to the game. The only link between the two is the JSON file.

---

## Requirements

- **Main Sequence** (Steam, Windows)
- **[MSCore](https://github.com/xxalex3xx97/MSCore)**, the mod loader and shared library for Main Sequence. Install it with **MSCore Manager** (`MSCoreManager.exe`, from MSCore's latest release): close the game, run the Manager, and click **Install/Repair MSCore**. That installs UE4SS and MSCore and puts MSCore first in the mod list. Developed and tested with MSCore **0.8.4**. (This mod's scripts don't call MSCore directly, but they need the working UE4SS setup it provides.)
- **Python 3.10 or newer** with Tkinter. Tkinter ships with the standard Windows installer from python.org. No `pip install` is needed, because the app uses only the standard library.

---

## 1. Install the Lua mod

The location matters here. `main.lua` must be inside a UE4SS mod folder.

1. Find the UE4SS `Mods` folder that MSCore set up:
   `…\Main Sequence\Windows\MainSequence\Binaries\Win64\ue4ss\Mods\`

   It already contains an `MSCore` folder and a `mods.txt`.
2. Create this structure and put `main.lua` in it:

   ```
   Mods\
   └── LiveStockLevels\
       └── Scripts\
           └── main.lua
   ```

   The folder name `LiveStockLevels` is just a suggestion, and any name works.
3. Enable the mod by adding a line to `Mods\mods.txt`, **below** the MSCore line so it loads after it:

   ```
   MSCore : 1
   LiveStockLevels : 1
   ```

   Leave the existing `MSCore : 1` line as it is and just add yours.
4. Start the game and load into your save.

When it's working, the UE4SS console shows lines starting with `[LiveStockLevels]`, and a file called **`live_stock_levels.json`** appears in the game's `Binaries\Win64` folder.

---

## 2. Run the Python app

The script can live **anywhere**, for example your Documents or Desktop. It doesn't need to be near the game.

1. Put `live_stock_monitor.py` in a folder of your choice.
2. Double-click it, or run `python live_stock_monitor.py`.
3. The app looks for `live_stock_levels.json` automatically in your Steam libraries. If it can't find it, click the browse button and choose the file from `…\Binaries\Win64\`. The path is remembered in `stock_monitor_ui.json`, which is created next to the script.

To skip auto-detection you can set the environment variable `STOCK_JSON_PATH` to the full path of the JSON file.

Only one copy of the app can run at a time. A second launch just brings the existing window to the front.

---

## 3. Using it

**In game:** nothing to do. The mod exports every 10 seconds. Press **F9** for an immediate export.

**In the app:** the window refreshes automatically (default every 10 s). The controls are:

- **Refresh (sec)**, **Auto-Refresh**, **Refresh Now**, and **Always on Top**
- **Search** and **Qty Filter** to narrow the lists
- **Issues Only** to hide everything that's healthy

Tabs:

| Tab | Shows |
|---|---|
| **Production & Bottlenecks** | Each production line per station, its status, and the root cause of any stall. |
| **100% Capacity & Requirements** | What each material needs, and what is produced, if every machine ran at 100 % at the same time. |
| **By Station / Storage** | Stock held at each station. |
| **Global Stock Totals** | Total stock, with trends since the app started. |

### Reading the 100 % Capacity tab

| Column | Meaning |
|---|---|
| **100 % Demand (/m)** | Items per minute that all machines would consume at full speed. |
| **Max Supply (/m)** | Items per minute your machines could produce at full speed. |
| **Net Balance (/m)** | Supply minus demand. Negative means you need more machines. |
| **Current Stock** | What you hold right now. |
| **Recommendation** | `BUILD +N× …` for a machine shortfall, `LOW RAW BUFFER (X m left)` for raw materials, or `Sufficient`. |

**"4.7m left"** means 4.7 *minutes*: current stock divided by 100 % demand. It assumes everything runs flat out and ignores incoming mining, so compare the "ensure ≥ N/m mining" figure against your actual mining or import rate.

**End Product** rows show 0 demand because nothing in your factory consumes them.

---

## Settings you may want to change

All are near the top of `live_stock_monitor.py`.

| Setting | Default | Purpose |
|---|---|---|
| `MAIN_BASE_KEYWORD` | `"hub"` | Only stations whose name contains this text are counted in the totals, machines and balances. This excludes remote mining outposts. Set it to a word from **your** main base name, or `""` to include every station. |
| `PROGRESS_TICKS_PER_SEC` | `60.0` | How fast machine progress advances. If cycle times look 60× too long or too short compared to the in-game craft time, set it to `1.0`. |
| `REFRESH_INTERVAL_SECONDS` | `10` | Default refresh interval. |

In `main.lua`, `AUTO_EXPORT = true` controls the 10-second background export. Set it to `false` to export only when you press F9.

---

## Troubleshooting

**The app says "Waiting for JSON file…"**
The mod hasn't written the file yet. Check that the mod is enabled and you're loaded into a save, then press F9. Confirm `live_stock_levels.json` exists in `Binaries\Win64` and that the path shown at the bottom right of the app matches it.

**No `[LiveStockLevels]` lines in the UE4SS console**
The mod isn't loading. Check the folder structure (`Mods\<name>\Scripts\main.lua`), that it's enabled in `mods.txt` (or with `enabled.txt`), and that MSCore is installed and enabled above it.

**A machine type is missing from the app**
The mod exports only the machine types it recognises. Look in the UE4SS console for lines like `[LiveStockLevels] Skipped inventory owner: BPO_SomethingMachine (x3)`. If one of those is a crafting machine, add its name to the matching list in `main.lua` (the `string.find(parent_name, …)` block).

**Demand or supply looks wrong by a constant factor**
Check `PROGRESS_TICKS_PER_SEC` against a recipe whose craft time you know.

**Numbers include remote bases, or nothing at all**
Check `MAIN_BASE_KEYWORD`. A station is included only if its name contains that text, ignoring case.

**Odd trends after changing settings**
Click **Clear All** in the app, or delete `stock_history.json`, to reset history.

---

## Known limitations

- Items are assumed to produce 1 per cycle. The mod doesn't yet read each recipe's real output quantity, so recipes that make several items per cycle will be under-counted.
- Research machines (Sci Labs) may not report a recipe, so their consumption might not be counted.
- The 100 % figures are theoretical. They assume every machine is running and never starved.
- The mod depends on game internals, so a game update may require changes.
