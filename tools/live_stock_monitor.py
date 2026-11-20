import json
import os
import sys
import re
import socket
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, filedialog
import tkinter.font as tkfont

# ==============================================================================
# CONFIGURATION
# ==============================================================================
JSON_FILENAME = "live_stock_levels.json"
GAME_JSON_SUBPATH = os.path.join("Main Sequence", "Windows", "MainSequence", "Binaries", "Win64", JSON_FILENAME)


def find_default_json_path() -> str:
    """Best-effort location of the file the Lua mod writes. Order:
    1. STOCK_JSON_PATH environment variable
    2. Any Steam library (read from Steam's libraryfolders.vdf) that has Main Sequence installed
    3. A copy next to this script
    The user can always change it with the file picker; the choice is remembered."""
    env = os.environ.get("STOCK_JSON_PATH")
    if env:
        return env

    steam_roots = []
    try:
        import winreg  # Windows only
        for hive, sub, val in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    steam_roots.append(winreg.QueryValueEx(k, val)[0])
            except OSError:
                pass
    except ImportError:
        pass
    for env_var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_var)
        if base:
            steam_roots.append(os.path.join(base, "Steam"))

    libraries = []
    for root in steam_roots:
        libraries.append(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    libraries.append(m.group(1).replace("\\\\", "\\"))
        except OSError:
            pass

    for lib in libraries:
        candidate = os.path.join(lib, "steamapps", "common", GAME_JSON_SUBPATH)
        if os.path.isdir(os.path.dirname(candidate)):
            return candidate
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), JSON_FILENAME)


DEFAULT_JSON_PATH = find_default_json_path()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_CONFIG_PATH = os.path.join(BASE_DIR, "stock_monitor_ui.json")
HISTORY_PATH = os.path.join(BASE_DIR, "stock_history.json")

REFRESH_INTERVAL_SECONDS = 10

# MaxProgress is 16.16 fixed-point (e.g. 11796480 = 180.0 * 65536). Progress advances
# PROGRESS_TICKS_PER_SEC fixed-point-ticks each second at 100% speed (game runs 60 ticks/s,
# so Basic Circuit = 180 ticks = 3.0 s per cycle). If cycle times look 60x too slow/fast
# vs. the in-game craft time, change PROGRESS_TICKS_PER_SEC to 1.0.
# Only stations whose name contains this text (case-insensitive) are included in ALL totals,
# machines and balances. Set to "" to include every station.
MAIN_BASE_KEYWORD = "hub"

PROGRESS_FIXED_POINT_SCALE = 65536.0
PROGRESS_TICKS_PER_SEC = 60.0
PROGRESS_UNITS_PER_SEC_OVERRIDE = PROGRESS_FIXED_POINT_SCALE * PROGRESS_TICKS_PER_SEC
MAX_HISTORY_SAMPLES = 50
SPARKLINE_W = 84
SPARKLINE_H = 18

QTY_FILTER_MODES = (
    "Off",
    "Below (<)",
    "At or Below (<=)",
    "Above (>)",
    "At or Above (>=)",
)


def restrict_to_main_base(data: dict) -> dict:
    """Drops every station (and its machines/stock) whose name lacks MAIN_BASE_KEYWORD, then
    rebuilds global_stock from the remaining stations only."""
    kw = (MAIN_BASE_KEYWORD or "").strip().lower()
    if not kw or not isinstance(data, dict):
        return data
    names = data.get("station_names", {}) or {}
    keep = {sid for sid, nm in names.items() if kw in str(nm).lower()}
    by_station = {sid: items for sid, items in (data.get("by_station", {}) or {}).items() if sid in keep}
    global_stock: dict[str, int] = {}
    for items in by_station.values():
        for item, qty in items.items():
            global_stock[item] = global_stock.get(item, 0) + qty
    data["by_station"] = by_station
    data["global_stock"] = global_stock
    data["station_names"] = {sid: nm for sid, nm in names.items() if sid in keep}
    for key in ("station_limits", "station_limit_stock"):
        if isinstance(data.get(key), dict):
            data[key] = {sid: v for sid, v in data[key].items() if sid in keep}
    data["machines"] = [m for m in (data.get("machines", []) or []) if m.get("station_id") in keep]
    data["active_machines"] = len(data["machines"])
    return data


# Game-data typos: recipe/item names that should be treated as another item (lowercase keys).
ITEM_KEY_ALIASES = {
    "magetriccoils": "magneticcoils",
    "mageticcoils": "magneticcoils",
}


def normalize_item_key(raw_name: str) -> str:
    """Normalizes ITEM_X / REC_X into a canonical key for matching recipe outputs to inputs."""
    if not raw_name or raw_name == "None":
        return ""
    key = raw_name.strip()
    for prefix in ("ITEM_", "REC_"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    key = key.lower()
    return ITEM_KEY_ALIASES.get(key, key)


def format_item_name(raw_name: str) -> str:
    """Strips 'ITEM_' / 'REC_' prefixes and converts CamelCase to readable spacing."""
    if not raw_name or raw_name == "None":
        return "None"
    name = raw_name.removeprefix("ITEM_").removeprefix("REC_")
    name = {"MageticCoils": "MagneticCoils"}.get(name, name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return name


def get_machine_type_label(raw_id: str, display_name: str | None) -> str:
    """Extracts clean machine archetype name (e.g. 'Assembler')."""
    clean = (display_name or raw_id).strip()
    if clean.startswith(("BP_", "BPO_")):
        clean = clean.removeprefix("BPO_").removeprefix("BP_")
        if "_" in clean:
            clean = clean.rsplit("_", 1)[0]
        clean = clean.replace("Object_C", "").replace("_C", "")
        clean = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", clean)
    return clean.strip() or "Machine"


def format_machine_label(raw_id: str, display_name: str | None) -> str:
    """Formats individual machine label with short instance ID."""
    short_id = raw_id.split("_")[-1][-5:] if "_" in raw_id else raw_id
    mtype = get_machine_type_label(raw_id, display_name)
    return f"{mtype}  [#{short_id}]"


def format_station_label(raw_id: str, display_name: str | None) -> str:
    """Formats station header using actual in-game name if available."""
    short_id = raw_id.split("_")[-1][-5:] if raw_id.startswith("BP_StationObject_C_") else raw_id
    if display_name and display_name.strip() and display_name.strip() != raw_id:
        return f"{display_name.strip()}  [#{short_id}]"
    if raw_id.startswith("BP_StationObject_C_"):
        return f"Station #{short_id}"
    return raw_id


def format_elapsed(last_changed_ts: float | None, now_ts: float) -> str:
    """Formats elapsed time since a quantity last changed."""
    if not last_changed_ts:
        return "—"
    elapsed = max(0, int(now_ts - last_changed_ts))
    clock_str = time.strftime("%H:%M:%S", time.localtime(last_changed_ts))
    if elapsed < 5:
        rel = "Just now"
    elif elapsed < 60:
        rel = f"{elapsed}s ago"
    elif elapsed < 3600:
        mins, secs = divmod(elapsed, 60)
        rel = f"{mins}m {secs:02d}s ago"
    elif elapsed < 86400:
        hours, rem = divmod(elapsed, 3600)
        mins = rem // 60
        rel = f"{hours}h {mins:02d}m ago"
    else:
        days = elapsed // 86400
        hours = (elapsed % 86400) // 3600
        rel = f"{days}d {hours}h ago"
    return f"{rel} ({clock_str})"


class SparklineFactory:
    """Generates native tk.PhotoImage mini line charts without external dependencies."""

    def __init__(self, width: int = SPARKLINE_W, height: int = SPARKLINE_H):
        self.w = width
        self.h = height
        self._cache: dict[tuple, tk.PhotoImage] = {}

    def get_image(self, samples: list[int], bg_hex: str) -> tk.PhotoImage:
        pts = self._to_points(samples)
        if not samples:
            color = "#475569"
        elif samples[-1] > samples[0]:
            color = "#4ade80"
        elif samples[-1] < samples[0]:
            color = "#f87171"
        else:
            color = "#64748b"

        cache_key = (pts, bg_hex, color)
        if cache_key in self._cache:
            return self._cache[cache_key]

        img = tk.PhotoImage(width=self.w, height=self.h)
        img.put(bg_hex, to=(0, 0, self.w, self.h))
        img.put("#293244", to=(1, self.h - 1, self.w - 1, self.h))

        if len(pts) == 1:
            x, y = pts[0]
            self._draw_dot(img, x, y, color)
        elif len(pts) > 1:
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                self._draw_line(img, x0, y0, x1, y1, color)
            lx, ly = pts[-1]
            self._draw_dot(img, lx, ly, "#ffffff")

        if len(self._cache) > 1500:
            self._cache.clear()
        self._cache[cache_key] = img
        return img

    def _to_points(self, samples: list[int]) -> tuple[tuple[int, int], ...]:
        if not samples:
            return ()
        pad_x = 2
        pad_y = 2
        usable_w = max(1, self.w - 1 - 2 * pad_x)
        usable_h = max(1, self.h - 2 - 2 * pad_y)

        n = len(samples)
        if n == 1:
            return ((self.w - 1 - pad_x, self.h // 2),)

        lo = min(samples)
        hi = max(samples)
        span = hi - lo

        step_x = usable_w / max(1, MAX_HISTORY_SAMPLES - 1)
        start_x = (self.w - 1 - pad_x) - (n - 1) * step_x

        coords = []
        for i, val in enumerate(samples):
            x = int(round(start_x + i * step_x))
            if span == 0:
                y = self.h // 2
            else:
                norm = (val - lo) / span
                y = int(round(pad_y + (1.0 - norm) * usable_h))
            coords.append((max(0, min(self.w - 1, x)), max(0, min(self.h - 1, y))))
        return tuple(coords)

    def _draw_line(self, img: tk.PhotoImage, x0: int, y0: int, x1: int, y1: int, color: str):
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            img.put(color, (x0, y0))
            if y0 + 1 < self.h:
                img.put(color, (x0, y0 + 1))
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def _draw_dot(self, img: tk.PhotoImage, cx: int, cy: int, color: str):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                px, py = cx + dx, cy + dy
                if 0 <= px < self.w and 0 <= py < self.h:
                    img.put(color, (px, py))


class StockMonitorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Main Sequence — Production & Stock Diagnostics")

        self.ui_state = self._load_json_file(UI_CONFIG_PATH, {})
        self.root.geometry(self.ui_state.get("geometry", "1440x860"))
        self.root.minsize(1040, 600)
        self._normal_geometry: str = self.ui_state.get("geometry", "1440x860")
        self._layout_ready = False   # becomes True once saved pane layout has been restored
        self._save_after_id: str | None = None

        # High-contrast dark slate palette
        self.bg_dark = "#11151c"
        self.bg_panel = "#181e29"
        self.bg_row_alt = "#1d2432"
        self.bg_header = "#222b3b"
        self.bg_card = "#141922"
        self.border_subtle = "#2a3447"
        self.fg_primary = "#e2e8f0"
        self.fg_muted = "#94a3b8"
        self.accent = "#38bdf8"
        self.green = "#4ade80"
        self.amber = "#fbbf24"
        self.red = "#f87171"

        self.root.configure(bg=self.bg_dark)

        # State variables
        self.json_path = tk.StringVar(value=self.ui_state.get("json_path", DEFAULT_JSON_PATH))
        self.refresh_sec = tk.IntVar(value=self.ui_state.get("refresh_sec", REFRESH_INTERVAL_SECONDS))
        self.search_query = tk.StringVar(value="")
        saved_mode = self.ui_state.get("qty_filter_mode", "Off")
        if saved_mode not in QTY_FILTER_MODES:
            saved_mode = "Off"
        self.qty_filter_mode = tk.StringVar(value=saved_mode)
        self.qty_filter_val = tk.StringVar(value=str(self.ui_state.get("qty_filter_val", "100")))
        self.issues_only = tk.BooleanVar(value=self.ui_state.get("issues_only", False))
        self.auto_refresh_enabled = tk.BooleanVar(value=self.ui_state.get("auto_refresh", True))
        self.always_on_top = tk.BooleanVar(value=self.ui_state.get("always_on_top", False))
        self.balance_scope = tk.StringVar(value=self.ui_state.get("balance_scope", "Global Factory"))

        self.active_trace_filter: tuple[str, str, str] | None = None
        self.root_cause_lookup: dict[str, dict] = {}
        self.line_group_lookup: dict[str, dict] = {}
        self.machine_lookup: dict[str, dict] = {}
        self.station_graphs: dict[str, dict] = {}
        self.selected_inspector_target: tuple[str, str] | None = None

        self.previous_global: dict[str, int] = {}
        self.previous_station: dict[str, dict[str, int]] = {}
        self.previous_machines: dict[str, dict] = {}
        self.machine_metrics: dict[str, dict] = {}
        self.progress_rate_peak: dict[str, float] = {}   # machine id -> peak progress units/sec at 100% speed
        self.last_file_mtime: float = 0.0
        self.last_data: dict | None = None
        self.after_id: str | None = None

        raw_hist = self._load_json_file(HISTORY_PATH, {})
        self.history_global: dict[str, dict] = self._hydrate_history(raw_hist.get("global", {}))
        self.history_station: dict[str, dict] = self._hydrate_history(raw_hist.get("station", {}))

        self.sparklines = SparklineFactory()
        self._active_images: list[tk.PhotoImage] = []

        self._setup_styles()
        self._build_ui()
        self._toggle_always_on_top()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self._restore_window_state()
        # Panes only have their real size once the window is mapped, so restore dividers a bit later
        self.root.after(250, self._restore_layout)
        self.root.after(900, self._restore_layout)   # second pass in case the first ran before final sizing

        self.refresh_data(force=True)
        self._schedule_next_refresh()

    def _load_json_file(self, path: str, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def _hydrate_history(self, section: dict) -> dict[str, dict]:
        hydrated = {}
        for key, entry in section.items():
            samples = deque(entry.get("samples", []), maxlen=MAX_HISTORY_SAMPLES)
            hydrated[key] = {
                "samples": samples,
                "last_changed": entry.get("last_changed"),
            }
        return hydrated

    def _save_history(self):
        try:
            payload = {
                "global": {
                    k: {"samples": list(v["samples"]), "last_changed": v["last_changed"]}
                    for k, v in self.history_global.items()
                },
                "station": {
                    k: {"samples": list(v["samples"]), "last_changed": v["last_changed"]}
                    for k, v in self.history_station.items()
                },
            }
            with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            pass

    def _record_sample(self, store: dict[str, dict], key: str, qty: int, sample_ts: float):
        entry = store.get(key)
        if entry is None:
            store[key] = {
                "samples": deque([qty], maxlen=MAX_HISTORY_SAMPLES),
                "last_changed": sample_ts,
            }
            return

        samples: deque = entry["samples"]
        prev_qty = samples[-1] if samples else None
        samples.append(qty)
        if prev_qty is None or qty != prev_qty:
            entry["last_changed"] = sample_ts

    # ---- layout persistence helpers -------------------------------------------------
    def _on_root_configure(self, event):
        if event.widget is not self.root:
            return
        try:
            if self.root.state() == "normal":
                self._normal_geometry = self.root.geometry()
        except Exception:
            pass
        self._schedule_ui_save()

    def _schedule_ui_save(self, delay_ms: int = 1000):
        """Debounced save so layout survives a crash / kill, not just a clean close."""
        if not self._layout_ready:
            return
        if self._save_after_id:
            try:
                self.root.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._save_after_id = self.root.after(delay_ms, self._save_ui_state)

    def _restore_window_state(self):
        try:
            if self.ui_state.get("zoomed"):
                self.root.state("zoomed")
        except Exception:
            pass

    def _pane_fraction(self, pane: tk.PanedWindow, idx: int, vertical: bool) -> float | None:
        try:
            size = pane.winfo_height() if vertical else pane.winfo_width()
            if size < 100:
                return None
            coord = pane.sash_coord(idx)
            pos = coord[1] if vertical else coord[0]
            return round(pos / size, 5)
        except Exception:
            return None

    def _get_pane_state(self) -> dict:
        saved = dict(self.ui_state.get("panes", {}))
        panes = {
            "v": (self.v_pane, True),
            "h": (self.h_pane, False),
            "insp": (self.insp_v_pane, True),
        }
        for key, (pane, vertical) in panes.items():
            frac = self._pane_fraction(pane, 0, vertical)
            if frac is not None:
                saved[key] = frac
        return saved

    def _restore_layout(self):
        panes = self.ui_state.get("panes", {})
        try:
            self.root.update_idletasks()
            for key, pane, vertical in (
                ("v", self.v_pane, True),
                ("h", self.h_pane, False),
                ("insp", self.insp_v_pane, True),
            ):
                frac = panes.get(key)
                if frac is None:
                    continue
                size = pane.winfo_height() if vertical else pane.winfo_width()
                if size < 100:
                    continue
                pos = int(size * float(frac))
                if vertical:
                    pane.sash_place(0, 0, pos)
                else:
                    pane.sash_place(0, pos, 0)
        except Exception as exc:
            print(f"[layout] restore failed: {exc}", file=sys.stderr)
        finally:
            self._layout_ready = True

    def _save_ui_state(self):
        # Start from what we loaded so keys we don't touch (or can't read right now) are never lost.
        state = dict(self.ui_state)

        def put(key, getter):
            try:
                state[key] = getter()
            except Exception as exc:
                print(f"[layout] could not save '{key}': {exc}", file=sys.stderr)

        def _geometry():
            zoomed = self.root.state() == "zoomed"
            return self._normal_geometry if zoomed else self.root.geometry()

        put("geometry", _geometry)
        put("zoomed", lambda: self.root.state() == "zoomed")
        put("json_path", lambda: self.json_path.get())
        put("refresh_sec", lambda: max(1, int(self.refresh_sec.get())))
        put("auto_refresh", lambda: self.auto_refresh_enabled.get())
        put("always_on_top", lambda: self.always_on_top.get())
        put("qty_filter_mode", lambda: self.qty_filter_mode.get())
        put("qty_filter_val", lambda: self.qty_filter_val.get())
        put("issues_only", lambda: self.issues_only.get())
        put("balance_scope", lambda: self.balance_scope.get())
        put("station_columns", lambda: self._get_tree_layout(self.station_tree))
        put("global_columns", lambda: self._get_tree_layout(self.global_tree))
        put("line_columns", lambda: self._get_tree_layout(self.line_tree))
        put("rc_columns", lambda: self._get_tree_layout(self.rc_tree))
        put("balance_columns", lambda: self._get_tree_layout(self.balance_tree))
        put("selected_tab", lambda: self.notebook.index(self.notebook.select()))
        if self._layout_ready:          # don't overwrite saved dividers with pre-restore values
            put("panes", self._get_pane_state)

        try:
            tmp_path = UI_CONFIG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, UI_CONFIG_PATH)   # atomic: never leaves a half-written file
            self.ui_state = state
        except Exception as exc:
            print(f"[layout] could not write {UI_CONFIG_PATH}: {exc}", file=sys.stderr)

    def _get_tree_layout(self, tree: ttk.Treeview) -> dict:
        widths = {"#0": tree.column("#0", "width")}
        for col in tree["columns"]:
            widths[col] = tree.column(col, "width")

        disp = tree["displaycolumns"]
        order = list(tree["columns"]) if (disp == ("#all",) or disp == "#all") else list(disp)
        return {"order": order, "widths": widths}

    def _apply_tree_layout(self, tree: ttk.Treeview, layout: dict | None):
        if not layout:
            return
        valid_cols = set(tree["columns"])
        saved_order = [c for c in layout.get("order", []) if c in valid_cols]
        for c in tree["columns"]:
            if c not in saved_order:
                saved_order.append(c)
        if saved_order:
            tree["displaycolumns"] = saved_order

        saved_widths = layout.get("widths", {})
        if "#0" in saved_widths:
            tree.column("#0", width=int(saved_widths["#0"]))
        for col, w in saved_widths.items():
            if col in valid_cols:
                tree.column(col, width=int(w))

    def _on_close(self):
        self._save_ui_state()
        self._save_history()
        self.root.destroy()

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("TFrame", background=self.bg_dark)
        style.configure("Panel.TFrame", background=self.bg_panel)
        style.configure("Card.TFrame", background=self.bg_card)
        style.configure("TLabel", background=self.bg_dark, foreground=self.fg_primary, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=self.bg_panel, foreground=self.fg_primary, font=("Segoe UI", 10))
        style.configure("Header.TLabel", background=self.bg_dark, foreground=self.accent, font=("Segoe UI", 11, "bold"))
        style.configure("Section.TLabel", background=self.bg_panel, foreground=self.accent, font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", background=self.bg_panel, foreground=self.fg_muted, font=("Segoe UI", 9))

        style.configure(
            "Filter.TCombobox",
            fieldbackground="#263042",
            background="#263042",
            foreground=self.fg_primary,
            arrowcolor=self.accent,
            borderwidth=0,
            padding=4,
        )
        style.map(
            "Filter.TCombobox",
            fieldbackground=[("readonly", "#263042")],
            foreground=[("readonly", self.fg_primary)],
            selectbackground=[("readonly", "#263042")],
            selectforeground=[("readonly", self.fg_primary)],
        )

        style.configure("TNotebook", background=self.bg_dark, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=self.bg_panel,
            foreground=self.fg_muted,
            padding=[14, 6],
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#263042")],
            foreground=[("selected", self.accent)],
        )

        style.configure(
            "Treeview",
            background=self.bg_panel,
            foreground=self.fg_primary,
            fieldbackground=self.bg_panel,
            rowheight=26,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#242e40",
            foreground=self.accent,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
        )
        style.map("Treeview", background=[("selected", "#2a3952")], foreground=[("selected", "#ffffff")])

    def _build_ui(self):
        # Top Control Bar
        top_bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(12, 8))
        top_bar.pack(fill=tk.X, side=tk.TOP)

        ttk.Label(top_bar, text="Refresh (sec):", style="Panel.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        spin = tk.Spinbox(
            top_bar,
            from_=1,
            to=3600,
            textvariable=self.refresh_sec,
            width=5,
            bg="#263042",
            fg=self.fg_primary,
            buttonbackground="#263042",
            insertbackground=self.fg_primary,
            relief="flat",
            font=("Segoe UI", 10),
            command=self._on_interval_changed,
        )
        spin.pack(side=tk.LEFT, padx=(0, 12))
        spin.bind("<Return>", lambda e: self._on_interval_changed())

        tk.Checkbutton(
            top_bar,
            text="Auto-Refresh",
            variable=self.auto_refresh_enabled,
            command=self._toggle_auto_refresh,
            bg=self.bg_panel,
            fg=self.fg_primary,
            selectcolor="#263042",
            activebackground=self.bg_panel,
            activeforeground=self.fg_primary,
            font=("Segoe UI", 10),
        ).pack(side=tk.LEFT, padx=(0, 10))

        tk.Button(
            top_bar,
            text="Refresh Now",
            command=lambda: self.refresh_data(force=True),
            bg="#263042",
            fg=self.accent,
            activebackground="#334155",
            activeforeground=self.accent,
            relief="flat",
            padx=10,
            pady=3,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 12))

        tk.Checkbutton(
            top_bar,
            text="Always on Top",
            variable=self.always_on_top,
            command=self._toggle_always_on_top,
            bg=self.bg_panel,
            fg=self.fg_primary,
            selectcolor="#263042",
            activebackground=self.bg_panel,
            activeforeground=self.fg_primary,
            font=("Segoe UI", 10),
        ).pack(side=tk.LEFT, padx=(0, 12))

        tk.Button(
            top_bar,
            text="Clear History",
            command=self._clear_history,
            bg="#263042",
            fg=self.fg_muted,
            activebackground="#334155",
            activeforeground=self.red,
            relief="flat",
            padx=8,
            pady=3,
            font=("Segoe UI", 9),
            cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(6, 0))

        tk.Button(
            top_bar,
            text="Reset Columns",
            command=self._reset_columns,
            bg="#263042",
            fg=self.fg_muted,
            activebackground="#334155",
            activeforeground=self.fg_primary,
            relief="flat",
            padx=8,
            pady=3,
            font=("Segoe UI", 9),
            cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(6, 0))

        tk.Button(
            top_bar,
            text="Select JSON...",
            command=self._browse_json,
            bg="#263042",
            fg=self.fg_muted,
            activebackground="#334155",
            activeforeground=self.fg_primary,
            relief="flat",
            padx=8,
            pady=3,
            font=("Segoe UI", 9),
            cursor="hand2",
        ).pack(side=tk.RIGHT)

        # Filter / Search Bar
        filter_bar = ttk.Frame(self.root, padding=(12, 7))
        filter_bar.pack(fill=tk.X)

        ttk.Label(filter_bar, text="Search:").pack(side=tk.LEFT, padx=(0, 6))
        entry_search = tk.Entry(
            filter_bar,
            textvariable=self.search_query,
            bg=self.bg_panel,
            fg=self.fg_primary,
            insertbackground=self.fg_primary,
            relief="flat",
            font=("Segoe UI", 10),
            width=22,
        )
        entry_search.pack(side=tk.LEFT, ipady=4)
        self.search_query.trace_add("write", lambda *_: self._render_tables())

        ttk.Label(filter_bar, text="Qty Filter:").pack(side=tk.LEFT, padx=(12, 6))
        combo_qty = ttk.Combobox(
            filter_bar,
            textvariable=self.qty_filter_mode,
            values=QTY_FILTER_MODES,
            state="readonly",
            style="Filter.TCombobox",
            width=15,
            font=("Segoe UI", 9),
        )
        combo_qty.pack(side=tk.LEFT)
        combo_qty.bind("<<ComboboxSelected>>", lambda *_: self._on_qty_filter_changed())

        entry_qty = tk.Entry(
            filter_bar,
            textvariable=self.qty_filter_val,
            bg=self.bg_panel,
            fg=self.fg_primary,
            insertbackground=self.fg_primary,
            relief="flat",
            font=("Segoe UI", 10),
            width=8,
            justify="right",
        )
        entry_qty.pack(side=tk.LEFT, padx=(6, 0), ipady=4)
        self.qty_filter_val.trace_add("write", lambda *_: self._on_qty_filter_changed())

        tk.Checkbutton(
            filter_bar,
            text="Issues Only",
            variable=self.issues_only,
            command=self._on_issues_only_changed,
            bg=self.bg_dark,
            fg=self.amber,
            selectcolor="#263042",
            activebackground=self.bg_dark,
            activeforeground=self.amber,
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(12, 0))

        tk.Button(
            filter_bar,
            text="Clear All",
            command=self._clear_filters,
            bg=self.bg_panel,
            fg=self.fg_muted,
            relief="flat",
            padx=8,
            pady=2,
            font=("Segoe UI", 9),
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=(8, 0))

        # Active root-cause trace chip
        self.trace_chip_frame = tk.Frame(filter_bar, bg="#2d2236", highlightbackground="#f87171", highlightthickness=1)
        self.trace_chip_label = tk.Label(
            self.trace_chip_frame,
            text="",
            bg="#2d2236",
            fg="#fca5a5",
            font=("Segoe UI", 9, "bold"),
            padx=8,
            pady=2,
        )
        self.trace_chip_label.pack(side=tk.LEFT)
        tk.Button(
            self.trace_chip_frame,
            text="✕",
            command=self._clear_trace_filter,
            bg="#2d2236",
            fg="#ffffff",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            padx=5,
        ).pack(side=tk.LEFT)

        self.summary_label = ttk.Label(
            filter_bar,
            text="Stations: 0 | Lines: 0 | Root Causes: 0",
            style="Header.TLabel",
        )
        self.summary_label.pack(side=tk.RIGHT)

        # Notebook Tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        # Tab 1: Production & Bottlenecks
        tab_prod = ttk.Frame(self.notebook)
        self.notebook.add(tab_prod, text="  Production & Bottlenecks  ")
        self._build_production_tab(tab_prod)

        # Tab 2: 100% Capacity & Material Requirements
        tab_balance = ttk.Frame(self.notebook)
        self.notebook.add(tab_balance, text="  100% Capacity & Requirements  ")
        self._build_balance_tab(tab_balance)

        # Tab 3: By Station / Storage
        tab_station = ttk.Frame(self.notebook)
        self.notebook.add(tab_station, text="  By Station / Storage  ")
        self.station_tree = self._create_tree(
            tab_station,
            columns=("qty", "limit_pct", "delta", "last_updated"),
            headings=("Trend (50)   Station / Item", "Quantity", "Limit %", "Change", "Last Updated"),
            widths=(420, 140, 145, 110, 210),
            anchors=("w", "e", "e", "e", "w"),
        )
        self._apply_tree_layout(self.station_tree, self.ui_state.get("station_columns"))

        # Tab 4: Global Totals
        tab_global = ttk.Frame(self.notebook)
        self.notebook.add(tab_global, text="  Global Stock Totals  ")
        self.global_tree = self._create_tree(
            tab_global,
            columns=("qty", "delta", "last_updated"),
            headings=("Trend (50)   Item Name", "Total Quantity", "Change", "Last Updated"),
            widths=(460, 140, 110, 220),
            anchors=("w", "e", "e", "w"),
        )
        self._apply_tree_layout(self.global_tree, self.ui_state.get("global_columns"))

        try:
            self.notebook.select(int(self.ui_state.get("selected_tab", 0)))
        except Exception:
            pass
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._schedule_ui_save(300), add="+")

        # Bottom Status Bar
        status_bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(12, 6))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.status_label = ttk.Label(status_bar, text="Initializing...", style="Status.TLabel")
        self.status_label.pack(side=tk.LEFT)

        self.path_label = ttk.Label(status_bar, text=self.json_path.get(), style="Status.TLabel")
        self.path_label.pack(side=tk.RIGHT)

    def _build_balance_tab(self, parent: ttk.Frame):
        hdr = ttk.Frame(parent, style="Panel.TFrame", padding=(10, 6))
        hdr.pack(fill=tk.X)

        self.balance_title_label = ttk.Label(
            hdr,
            text="100% EFFICIENCY MATERIAL BALANCE (Downstream Demand at Full Machine Load vs. Max Supply Capacity)",
            style="Section.TLabel",
        )
        self.balance_title_label.pack(side=tk.LEFT)

        scope_cb = ttk.Combobox(
            hdr,
            textvariable=self.balance_scope,
            values=("Global Factory", "Per Station"),
            state="readonly",
            style="Filter.TCombobox",
            width=16,
            font=("Segoe UI", 9),
        )
        scope_cb.pack(side=tk.RIGHT)
        scope_cb.bind("<<ComboboxSelected>>", lambda *_: self._on_balance_scope_changed())
        ttk.Label(hdr, text="Scope:", style="Panel.TLabel").pack(side=tk.RIGHT, padx=(0, 6))

        self.balance_tree = self._create_tree(
            parent,
            columns=("tier", "demand_100", "max_supply", "net_balance", "stock", "action_needed"),
            headings=(
                "Material / Resource",
                "Tier",
                "100% Demand (/m)",
                "Max Supply (/m)",
                "Net Balance (/m)",
                "Current Stock",
                "Bottleneck / Build Recommendation",
            ),
            widths=(250, 115, 135, 135, 135, 115, 420),
            anchors=("w", "center", "e", "e", "e", "e", "w"),
        )
        self._apply_tree_layout(self.balance_tree, self.ui_state.get("balance_columns"))

    def _on_balance_scope_changed(self):
        self._render_tables()
        self._save_ui_state()

    def _build_production_tab(self, parent: ttk.Frame):
        v_pane = tk.PanedWindow(
            parent,
            orient=tk.VERTICAL,
            bg=self.bg_dark,
            sashwidth=6,
            sashrelief=tk.FLAT,
            bd=0,
        )
        v_pane.pack(fill=tk.BOTH, expand=True)
        self.v_pane = v_pane

        # --- TOP PANE: ROOT-CAUSE BOTTLENECK TRIAGE ---
        rc_frame = ttk.Frame(v_pane, style="Panel.TFrame")
        rc_header = ttk.Frame(rc_frame, style="Panel.TFrame", padding=(10, 5))
        rc_header.pack(fill=tk.X)

        self.rc_title_label = ttk.Label(
            rc_header,
            text="ROOT-CAUSE BOTTLENECKS (Click any row to isolate its downstream cascade)",
            style="Section.TLabel",
        )
        self.rc_title_label.pack(side=tk.LEFT)

        tk.Button(
            rc_header,
            text="Show All Lines",
            command=self._clear_trace_filter,
            bg="#263042",
            fg=self.fg_primary,
            activebackground="#334155",
            relief="flat",
            padx=8,
            pady=2,
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
        ).pack(side=tk.RIGHT)

        self.rc_tree = self._create_tree(
            rc_frame,
            columns=("station", "cause", "direct_impact", "cascade_impact", "producer_status"),
            headings=("Missing / Blocked Resource", "Station", "Root Issue", "Direct Impact", "Downstream Cascade", "Upstream Producer Status"),
            widths=(200, 180, 185, 220, 230, 240),
            anchors=("w", "w", "w", "w", "w", "w"),
        )
        self.rc_tree.configure(height=4)
        self._apply_tree_layout(self.rc_tree, self.ui_state.get("rc_columns"))
        self.rc_tree.bind("<<TreeviewSelect>>", self._on_root_cause_selected)
        v_pane.add(rc_frame, minsize=110, height=155)

        # --- BOTTOM PANE: LEFT = GROUPED LINES, RIGHT = VISUAL FLOW + INSPECTOR ---
        h_pane = tk.PanedWindow(
            v_pane,
            orient=tk.HORIZONTAL,
            bg=self.bg_dark,
            sashwidth=6,
            sashrelief=tk.FLAT,
            bd=0,
        )
        v_pane.add(h_pane, minsize=300)
        self.h_pane = h_pane

        # Left: Grouped Production Lines Tree
        lines_frame = ttk.Frame(h_pane, style="Panel.TFrame")
        lines_hdr = ttk.Frame(lines_frame, style="Panel.TFrame", padding=(10, 6))
        lines_hdr.pack(fill=tk.X)
        ttk.Label(
            lines_hdr,
            text="PRODUCTION LINES BY STATION (Grouped by Recipe — Expand + for Machines)",
            style="Section.TLabel",
        ).pack(side=tk.LEFT)

        self.line_tree = self._create_tree(
            lines_frame,
            columns=("active", "status", "throughput", "inputs_compact", "output_buf"),
            headings=("Station / Production Line / Machine", "Active", "Health", "Throughput", "Input Buffers", "Output"),
            widths=(255, 68, 175, 110, 200, 100),
            anchors=("w", "center", "w", "e", "w", "w"),
        )
        self._apply_tree_layout(self.line_tree, self.ui_state.get("line_columns"))
        self.line_tree.bind("<<TreeviewSelect>>", self._on_line_selected)
        h_pane.add(lines_frame, minsize=460, width=780)

        # Right: Graphical Flow Diagram + Supply Chain Details
        insp_frame = ttk.Frame(h_pane, style="Card.TFrame")
        insp_hdr = tk.Frame(insp_frame, bg="#222b3b", padx=10, pady=6)
        insp_hdr.pack(fill=tk.X)
        tk.Label(
            insp_hdr,
            text="LIVE PRODUCTION FLOW & BOTTLENECK TRACE",
            bg="#222b3b",
            fg=self.accent,
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            insp_hdr,
            text="(Click any node in diagram to jump)",
            bg="#222b3b",
            fg=self.fg_muted,
            font=("Segoe UI", 8),
        ).pack(side=tk.RIGHT)

        insp_v_pane = tk.PanedWindow(
            insp_frame,
            orient=tk.VERTICAL,
            bg=self.bg_dark,
            sashwidth=5,
            sashrelief=tk.FLAT,
            bd=0,
        )
        insp_v_pane.pack(fill=tk.BOTH, expand=True)
        self.insp_v_pane = insp_v_pane
        for _pane in (self.v_pane, self.h_pane, insp_v_pane):
            _pane.bind("<ButtonRelease-1>", lambda _e: self._schedule_ui_save(300), add="+")

        # Top half of inspector: Scrollable Graphical Flow Canvas
        canvas_wrap = tk.Frame(insp_v_pane, bg="#0d1117")
        self._edge_labels: list = []
        self._pill_font = None
        self.flow_canvas = tk.Canvas(
            canvas_wrap,
            bg="#0d1117",
            highlightthickness=0,
            bd=0,
        )
        flow_scroll_y = ttk.Scrollbar(canvas_wrap, orient=tk.VERTICAL, command=self.flow_canvas.yview)
        flow_scroll_x = ttk.Scrollbar(canvas_wrap, orient=tk.HORIZONTAL, command=self.flow_canvas.xview)
        self.flow_canvas.configure(yscrollcommand=flow_scroll_y.set, xscrollcommand=flow_scroll_x.set)

        flow_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        flow_scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.flow_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.flow_canvas.bind("<Configure>", lambda _e: self._draw_flow_diagram())
        insp_v_pane.add(canvas_wrap, minsize=220, height=340)

        # Bottom half of inspector: Detailed Breakdown Text
        text_wrap = tk.Frame(insp_v_pane, bg=self.bg_card)
        self.inspector_text = tk.Text(
            text_wrap,
            bg=self.bg_card,
            fg=self.fg_primary,
            relief="flat",
            wrap=tk.WORD,
            padx=12,
            pady=8,
            font=("Segoe UI", 9),
            state=tk.DISABLED,
            cursor="arrow",
        )
        insp_scroll = ttk.Scrollbar(text_wrap, orient=tk.VERTICAL, command=self.inspector_text.yview)
        self.inspector_text.configure(yscrollcommand=insp_scroll.set)
        insp_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.inspector_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.inspector_text.tag_configure("h1", foreground="#ffffff", font=("Segoe UI", 11, "bold"))
        self.inspector_text.tag_configure("sub", foreground=self.fg_muted, font=("Segoe UI", 9))
        self.inspector_text.tag_configure("sec", foreground=self.accent, font=("Segoe UI", 9, "bold"))
        self.inspector_text.tag_configure("ok", foreground=self.green, font=("Segoe UI", 9, "bold"))
        self.inspector_text.tag_configure("warn", foreground=self.amber, font=("Segoe UI", 9, "bold"))
        self.inspector_text.tag_configure("bad", foreground=self.red, font=("Segoe UI", 9, "bold"))
        self.inspector_text.tag_configure("item", foreground=self.fg_primary, font=("Segoe UI", 9, "bold"))
        self.inspector_text.tag_configure("detail", foreground=self.fg_muted, font=("Segoe UI", 9))

        insp_v_pane.add(text_wrap, minsize=120, height=220)
        h_pane.add(insp_frame, minsize=460, width=620)

    def _create_tree(self, parent, columns, headings, widths, anchors) -> ttk.Treeview:
        container = ttk.Frame(parent)
        container.pack(fill=tk.BOTH, expand=True)

        tree = ttk.Treeview(container, columns=columns, show="tree headings")
        tree.heading("#0", text=headings[0], anchor=anchors[0])
        tree.column("#0", width=widths[0], minwidth=140, stretch=False, anchor=anchors[0])
        for col, head, w, anc in zip(columns, headings[1:], widths[1:], anchors[1:]):
            tree.heading(col, text=head, anchor=anc)
            tree.column(col, width=w, minwidth=60, stretch=False, anchor=anc)

        scrollbar_y = ttk.Scrollbar(container, orient=tk.VERTICAL, command=tree.yview)
        scrollbar_x = ttk.Scrollbar(container, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        scrollbar_y.pack(side=tk.RIGHT, fill=tk.Y)
        scrollbar_x.pack(side=tk.BOTTOM, fill=tk.X)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._bind_column_reorder(tree)

        tree.tag_configure("station_header", background=self.bg_header, foreground=self.accent, font=("Segoe UI", 10, "bold"))
        tree.tag_configure("gain", foreground=self.green)
        tree.tag_configure("loss", foreground=self.red)
        tree.tag_configure("even", background=self.bg_panel, foreground=self.fg_primary)
        tree.tag_configure("odd", background=self.bg_row_alt, foreground=self.fg_primary)

        # Limit percentage color-scale tags (100%+ green -> warning amber -> alert red)
        tree.tag_configure("lim_ok", foreground=self.green, font=("Segoe UI", 10, "bold"))
        tree.tag_configure("lim_warn", foreground=self.amber, font=("Segoe UI", 10, "bold"))
        tree.tag_configure("lim_alert", foreground=self.red, font=("Segoe UI", 10, "bold"))

        tree.tag_configure("st_ok", background=self.bg_panel, foreground=self.green)
        tree.tag_configure("st_bad", background=self.bg_panel, foreground=self.red, font=("Segoe UI", 10, "bold"))
        tree.tag_configure("st_warn", background=self.bg_panel, foreground=self.amber, font=("Segoe UI", 10, "bold"))
        tree.tag_configure("st_idle", background=self.bg_panel, foreground=self.fg_muted)
        tree.tag_configure("sub_ok", background=self.bg_row_alt, foreground=self.fg_muted, font=("Segoe UI", 9))
        tree.tag_configure("sub_bad", background=self.bg_row_alt, foreground=self.red, font=("Segoe UI", 9, "bold"))
        tree.tag_configure("sub_warn", background=self.bg_row_alt, foreground=self.amber, font=("Segoe UI", 9))
        tree.tag_configure("rc_healthy", background=self.bg_panel, foreground=self.green)

        return tree

    def _bind_column_reorder(self, tree: ttk.Treeview):
        drag_state = {"col": None}

        def on_press(event):
            if tree.identify_region(event.x, event.y) == "heading":
                col_id = tree.identify_column(event.x)
                if col_id != "#0":
                    idx = int(col_id.replace("#", "")) - 1
                    disp = tree["displaycolumns"]
                    cols = list(tree["columns"]) if (disp == ("#all",) or disp == "#all") else list(disp)
                    if 0 <= idx < len(cols):
                        drag_state["col"] = cols[idx]

        def on_release(event):
            src_col = drag_state["col"]
            drag_state["col"] = None
            if not src_col:
                self._save_ui_state()
                return

            if tree.identify_region(event.x, event.y) == "heading":
                target_id = tree.identify_column(event.x)
                if target_id != "#0":
                    idx = int(target_id.replace("#", "")) - 1
                    disp = tree["displaycolumns"]
                    cols = list(tree["columns"]) if (disp == ("#all",) or disp == "#all") else list(disp)
                    if 0 <= idx < len(cols):
                        dst_col = cols[idx]
                        if dst_col != src_col:
                            cols.remove(src_col)
                            new_idx = cols.index(dst_col)
                            cols.insert(new_idx, src_col)
                            tree["displaycolumns"] = cols
            self._save_ui_state()

        tree.bind("<ButtonPress-1>", on_press, add="+")
        tree.bind("<ButtonRelease-1>", on_release, add="+")

    def _on_qty_filter_changed(self):
        self._render_tables()
        self._save_ui_state()

    def _on_issues_only_changed(self):
        self._render_tables()
        self._save_ui_state()

    def _clear_trace_filter(self):
        self.active_trace_filter = None
        self.trace_chip_frame.pack_forget()
        for sel in self.rc_tree.selection():
            self.rc_tree.selection_remove(sel)
        self._render_tables()

    def _clear_filters(self):
        self.search_query.set("")
        self.qty_filter_mode.set("Off")
        self.issues_only.set(False)
        self._clear_trace_filter()
        self._save_ui_state()

    def _matches_qty_filter(self, qty: int) -> bool:
        mode = self.qty_filter_mode.get()
        if mode == "Off":
            return True
        raw_val = self.qty_filter_val.get().strip().replace(",", "")
        if not raw_val:
            return True
        try:
            threshold = int(float(raw_val))
        except ValueError:
            return True

        if mode == "Below (<)":
            return qty < threshold
        elif mode == "At or Below (<=)":
            return qty <= threshold
        elif mode == "Above (>)":
            return qty > threshold
        elif mode == "At or Above (>=)":
            return qty >= threshold
        return True

    def _evaluate_limit_status(self, qty: int, limit: int | None) -> tuple[str, str | None, str | None]:
        """Returns (display_str, hex_color, tree_tag) scaled from 100% (green) down to warning/alert."""
        if not limit or limit <= 0:
            return "—", None, None

        pct = (qty / limit) * 100.0
        pct_str = f"{pct:.0f}% ({qty:,}/{limit:,})"

        if pct >= 100.0:
            return pct_str, self.green, "lim_ok"
        elif pct >= 50.0:
            return pct_str, self.amber, "lim_warn"
        else:
            return pct_str, self.red, "lim_alert"

    def _reset_columns(self):
        self.station_tree["displaycolumns"] = ("qty", "limit_pct", "delta", "last_updated")
        self.station_tree.column("#0", width=420)
        self.station_tree.column("qty", width=140)
        self.station_tree.column("limit_pct", width=145)
        self.station_tree.column("delta", width=110)
        self.station_tree.column("last_updated", width=210)

        self.global_tree["displaycolumns"] = ("qty", "delta", "last_updated")
        self.global_tree.column("#0", width=460)
        self.global_tree.column("qty", width=140)
        self.global_tree.column("delta", width=110)
        self.global_tree.column("last_updated", width=220)

        self.line_tree["displaycolumns"] = ("active", "status", "throughput", "inputs_compact", "output_buf")
        self.line_tree.column("#0", width=255)
        self.line_tree.column("active", width=68)
        self.line_tree.column("status", width=175)
        self.line_tree.column("throughput", width=110)
        self.line_tree.column("inputs_compact", width=200)
        self.line_tree.column("output_buf", width=100)

        self.balance_tree["displaycolumns"] = ("tier", "demand_100", "max_supply", "net_balance", "stock", "action_needed")
        self.balance_tree.column("#0", width=250)
        self.balance_tree.column("tier", width=115)
        self.balance_tree.column("demand_100", width=135)
        self.balance_tree.column("max_supply", width=135)
        self.balance_tree.column("net_balance", width=135)
        self.balance_tree.column("stock", width=115)
        self.balance_tree.column("action_needed", width=420)
        self._save_ui_state()

    def _clear_history(self):
        self.history_global.clear()
        self.history_station.clear()
        self.machine_metrics.clear()
        self._save_history()
        self.refresh_data(force=True)

    def _toggle_always_on_top(self):
        self.root.attributes("-topmost", self.always_on_top.get())
        self._save_ui_state()

    def _toggle_auto_refresh(self):
        if self.auto_refresh_enabled.get():
            self._schedule_next_refresh()
        elif self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self._save_ui_state()

    def _on_interval_changed(self):
        try:
            val = int(self.refresh_sec.get())
            if val < 1:
                self.refresh_sec.set(1)
        except (ValueError, tk.TclError):
            self.refresh_sec.set(REFRESH_INTERVAL_SECONDS)
        if self.auto_refresh_enabled.get():
            self._schedule_next_refresh()
        self._save_ui_state()

    def _schedule_next_refresh(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
        try:
            delay_ms = max(1, int(self.refresh_sec.get())) * 1000
        except (ValueError, tk.TclError):
            delay_ms = REFRESH_INTERVAL_SECONDS * 1000
        self.after_id = self.root.after(delay_ms, self._auto_refresh_tick)

    def _auto_refresh_tick(self):
        self.refresh_data(force=False)
        if self.auto_refresh_enabled.get():
            self._schedule_next_refresh()

    def _update_relative_timestamps(self):
        now_ts = time.time()
        for parent_iid in self.station_tree.get_children():
            st_raw = parent_iid.removeprefix("ST::")
            st_entry = self.history_station.get(f"{st_raw}::__TOTAL__")
            if st_entry:
                self.station_tree.set(parent_iid, "last_updated", format_elapsed(st_entry.get("last_changed"), now_ts))

            for child_iid in self.station_tree.get_children(parent_iid):
                hist_key = child_iid.removeprefix("STI::")
                entry = self.history_station.get(hist_key)
                if entry:
                    self.station_tree.set(child_iid, "last_updated", format_elapsed(entry.get("last_changed"), now_ts))

        for item_iid in self.global_tree.get_children():
            item_raw = item_iid.removeprefix("GLB::")
            entry = self.history_global.get(item_raw)
            if entry:
                self.global_tree.set(item_iid, "last_updated", format_elapsed(entry.get("last_changed"), now_ts))

    def _browse_json(self):
        chosen = filedialog.askopenfilename(
            title="Select live_stock_levels.json",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
        )
        if chosen:
            self.json_path.set(chosen)
            self.path_label.config(text=chosen)
            self.last_file_mtime = 0.0
            self._save_ui_state()
            self.refresh_data(force=True)

    # ==========================================================================
    # MACHINE THROUGHPUT & ROOT-CAUSE DEPENDENCY GRAPH ENGINE
    # ==========================================================================
    def _update_machine_metrics(self, machines: list[dict], sample_ts: float):
        for m in machines:
            mid = m["id"]
            prog = m.get("progress", 0)
            max_prog = m.get("max_progress", 0)
            recipe = m.get("recipe", "None")
            b_working = m.get("b_working", False)
            out_total = sum(m.get("outputs", {}).values())

            prev = self.machine_metrics.get(mid)
            if prev is None or prev.get("recipe") != recipe or max_prog <= 0:
                self.machine_metrics[mid] = {
                    "last_ts": sample_ts,
                    "last_progress": prog,
                    "last_out_total": out_total,
                    "recipe": recipe,
                    "cycles_per_min": 0.0,
                    "items_per_min": 0.0,
                    "stall_seconds": 0.0,
                    "progress_delta": 0,
                }
                continue

            dt = sample_ts - prev["last_ts"]
            if dt <= 0.1:
                continue

            delta_prog = prog - prev["last_progress"]
            if delta_prog < 0 and max_prog > 0:
                delta_prog = (max_prog - prev["last_progress"]) + prog

            cycles_done = (delta_prog / max_prog) if max_prog > 0 else 0.0
            instant_cpm = (cycles_done / dt) * 60.0

            # Calibrate progress-units/sec at 100% speed (only when < ~half a cycle passed, so no wrap error)
            if b_working and delta_prog > 0 and delta_prog < max_prog * 0.5:
                spd = max(0.1, 1.0 + m.get("overspeed", 0) / 100.0)
                rate100 = (delta_prog / dt) / spd
                if rate100 > self.progress_rate_peak.get(mid, 0.0):
                    self.progress_rate_peak[mid] = rate100

            old_cpm = prev.get("cycles_per_min", 0.0)
            if b_working and delta_prog > 0:
                smoothed_cpm = instant_cpm if old_cpm == 0.0 else (0.65 * instant_cpm + 0.35 * old_cpm)
                stall_sec = 0.0
            else:
                smoothed_cpm = 0.0 if not b_working else (old_cpm * 0.5)
                stall_sec = prev.get("stall_seconds", 0.0) + dt

            out_delta = max(0, out_total - prev.get("last_out_total", out_total))
            items_per_min = (out_delta / dt) * 60.0 if out_delta > 0 else smoothed_cpm

            self.machine_metrics[mid] = {
                "last_ts": sample_ts,
                "last_progress": prog,
                "last_out_total": out_total,
                "recipe": recipe,
                "cycles_per_min": smoothed_cpm,
                "items_per_min": items_per_min,
                "stall_seconds": stall_sec,
                "progress_delta": delta_prog,
            }

    def _classify_machine(self, m: dict, metric: dict) -> dict:
        recipe = m.get("recipe", "None")
        if not recipe or recipe == "None":
            return {
                "state": "IDLE",
                "label": "○ IDLE (No Recipe)",
                "tag": "st_idle",
                "is_issue": False,
                "missing_items": [],
                "missing_norms": [],
            }

        if m.get("b_has_error", False):
            return {
                "state": "ERROR",
                "label": "✖ MACHINE ERROR",
                "tag": "st_bad",
                "is_issue": True,
                "missing_items": [],
                "missing_norms": [],
            }

        inputs = m.get("inputs", {})
        expected = m.get("expected_inputs", [])
        b_working = m.get("b_working", False)
        b_prog_complete = m.get("b_progress_complete", False)
        stall_sec = metric.get("stall_seconds", 0.0)
        cpm = metric.get("cycles_per_min", 0.0)

        missing_items = [k for k in expected if inputs.get(k, 0) <= 0]
        missing_norms = [normalize_item_key(k) for k in missing_items if normalize_item_key(k)]

        if b_prog_complete and not b_working:
            return {
                "state": "BLOCKED",
                "label": "■ OUTPUT BLOCKED",
                "tag": "st_bad",
                "is_issue": True,
                "missing_items": [],
                "missing_norms": [],
            }

        if missing_items:
            names = ", ".join(format_item_name(k) for k in missing_items[:2])
            if len(missing_items) > 2:
                names += f" +{len(missing_items) - 2}"
            return {
                "state": "STARVED",
                "label": f"● STARVED ({names})",
                "tag": "st_bad",
                "is_issue": True,
                "missing_items": missing_items,
                "missing_norms": missing_norms,
            }

        if not b_working:
            if stall_sec >= 15:
                return {
                    "state": "SLOW",
                    "label": f"▲ STALLED ({int(stall_sec)}s)",
                    "tag": "st_warn",
                    "is_issue": True,
                    "missing_items": [],
                    "missing_norms": [],
                }
            return {
                "state": "IDLE",
                "label": "○ PAUSED",
                "tag": "st_idle",
                "is_issue": False,
                "missing_items": [],
                "missing_norms": [],
            }

        if stall_sec >= 15 and metric.get("progress_delta", 0) == 0:
            return {
                "state": "SLOW",
                "label": f"▲ NO PROGRESS ({int(stall_sec)}s)",
                "tag": "st_warn",
                "is_issue": True,
                "missing_items": [],
                "missing_norms": [],
            }

        if m.get("overspeed", 0) < 0:
            return {
                "state": "SLOW",
                "label": f"▲ THROTTLED ({m['overspeed']}%)",
                "tag": "st_warn",
                "is_issue": True,
                "missing_items": [],
                "missing_norms": [],
            }

        rate_txt = f" ({cpm:.1f}/m)" if cpm > 0 else ""
        return {
            "state": "OK",
            "label": f"● RUNNING{rate_txt}",
            "tag": "st_ok",
            "is_issue": False,
            "missing_items": [],
            "missing_norms": [],
        }

    def _analyze_production_topology(self, machines: list[dict], by_station: dict[str, dict[str, int]], station_names: dict[str, str]):
        self.machine_lookup.clear()
        self.line_group_lookup.clear()
        self.station_graphs.clear()
        self.root_cause_lookup.clear()

        grouped: dict[tuple[str, str, str], list[dict]] = {}
        for m in machines:
            mid = m["id"]
            st_id = m.get("station_id", "Unassigned_Station")
            mtype = get_machine_type_label(mid, m.get("name"))
            recipe = m.get("recipe", "None")
            metric = self.machine_metrics.get(mid, {})
            health = self._classify_machine(m, metric)

            enriched = {
                **m,
                "machine_type": mtype,
                "metric": metric,
                "health": health,
            }
            self.machine_lookup[mid] = enriched
            grouped.setdefault((st_id, mtype, recipe), []).append(enriched)

        lines_by_station: dict[str, list[dict]] = {}
        for (st_id, mtype, recipe), m_list in grouped.items():
            line_key = f"{st_id}::{mtype}::{recipe}"
            rec_display = format_item_name(recipe)
            rec_norm = normalize_item_key(recipe)

            total_cnt = len(m_list)
            running_cnt = sum(1 for x in m_list if x["health"]["state"] == "OK")
            starved_cnt = sum(1 for x in m_list if x["health"]["state"] == "STARVED")
            blocked_cnt = sum(1 for x in m_list if x["health"]["state"] == "BLOCKED")
            slow_cnt = sum(1 for x in m_list if x["health"]["state"] in ("SLOW", "ERROR"))

            total_cpm = sum(x["metric"].get("cycles_per_min", 0.0) for x in m_list)
            total_ipm = sum(x["metric"].get("items_per_min", 0.0) for x in m_list)

            agg_inputs: dict[str, int] = {}
            all_input_keys: list[str] = []
            agg_outputs: dict[str, int] = {}
            missing_norms_set: set[str] = set()
            missing_display_map: dict[str, str] = {}

            for x in m_list:
                for k in x.get("expected_inputs", []):
                    if k not in all_input_keys:
                        all_input_keys.append(k)
                    agg_inputs.setdefault(k, 0)
                for k, v in x.get("inputs", {}).items():
                    if k not in all_input_keys:
                        all_input_keys.append(k)
                    agg_inputs[k] = agg_inputs.get(k, 0) + v
                for k, v in x.get("outputs", {}).items():
                    agg_outputs[k] = agg_outputs.get(k, 0) + v
                for mk in x["health"]["missing_items"]:
                    nk = normalize_item_key(mk)
                    if nk:
                        missing_norms_set.add(nk)
                        missing_display_map[nk] = format_item_name(mk)

            if not recipe or recipe == "None":
                line_state = "IDLE"
                line_status = "○ IDLE (No Recipe)"
                line_tag = "st_idle"
                is_issue = False
            elif blocked_cnt > 0:
                line_state = "BLOCKED"
                line_status = f"■ BLOCKED ({blocked_cnt}/{total_cnt})"
                line_tag = "st_bad"
                is_issue = True
            elif starved_cnt > 0:
                miss_names = ", ".join(missing_display_map.values())
                line_state = "STARVED"
                line_status = f"● STARVED: {miss_names}" if starved_cnt == total_cnt else f"● PARTIAL STARVED: {miss_names}"
                line_tag = "st_bad"
                is_issue = True
            elif slow_cnt > 0:
                line_state = "SLOW"
                line_status = f"▲ DELAYED ({slow_cnt}/{total_cnt})"
                line_tag = "st_warn"
                is_issue = True
            elif running_cnt > 0:
                line_state = "OK"
                line_status = "● OPTIMAL"
                line_tag = "st_ok"
                is_issue = False
            else:
                line_state = "IDLE"
                line_status = "○ PAUSED"
                line_tag = "st_idle"
                is_issue = False

            line_obj = {
                "line_key": line_key,
                "station_id": st_id,
                "machine_type": mtype,
                "recipe": recipe,
                "recipe_display": rec_display,
                "recipe_norm": rec_norm,
                "machines": m_list,
                "total_cnt": total_cnt,
                "running_cnt": running_cnt,
                "starved_cnt": starved_cnt,
                "blocked_cnt": blocked_cnt,
                "slow_cnt": slow_cnt,
                "total_cpm": total_cpm,
                "total_ipm": total_ipm,
                "agg_inputs": agg_inputs,
                "all_input_keys": all_input_keys,
                "agg_outputs": agg_outputs,
                "missing_norms": missing_norms_set,
                "missing_display_map": missing_display_map,
                "state": line_state,
                "status_label": line_status,
                "tag": line_tag,
                "is_issue": is_issue,
            }
            self.line_group_lookup[line_key] = line_obj
            lines_by_station.setdefault(st_id, []).append(line_obj)

        root_causes: list[dict] = []
        for st_id, st_lines in lines_by_station.items():
            producers_by_norm: dict[str, list[dict]] = {}
            consumers_by_norm: dict[str, list[dict]] = {}

            for ln in st_lines:
                if ln["recipe_norm"]:
                    producers_by_norm.setdefault(ln["recipe_norm"], []).append(ln)
                for in_k in ln["all_input_keys"]:
                    nk = normalize_item_key(in_k)
                    if nk:
                        consumers_by_norm.setdefault(nk, []).append(ln)

            st_storage_raw = by_station.get(st_id, {})
            storage_by_norm: dict[str, int] = {}
            for k, v in st_storage_raw.items():
                nk = normalize_item_key(k)
                if nk:
                    storage_by_norm[nk] = storage_by_norm.get(nk, 0) + v

            st_limits_raw = self.last_data.get("station_limits", {}).get(st_id, {}) if self.last_data else {}
            limits_by_norm: dict[str, int] = {}
            for k, v in st_limits_raw.items():
                nk = normalize_item_key(k)
                if nk:
                    limits_by_norm[nk] = limits_by_norm.get(nk, 0) + v

            st_lstock_raw = self.last_data.get("station_limit_stock", {}).get(st_id, {}) if self.last_data else {}
            limit_stock_by_norm: dict[str, int] = {}
            for k, v in st_lstock_raw.items():
                nk = normalize_item_key(k)
                if nk:
                    limit_stock_by_norm[nk] = limit_stock_by_norm.get(nk, 0) + v

            self.station_graphs[st_id] = {
                "lines": st_lines,
                "limit_stock": limit_stock_by_norm,
                "producers": producers_by_norm,
                "consumers": consumers_by_norm,
                "storage": storage_by_norm,
                "limits": limits_by_norm,
            }

            def find_root_norms(item_norm: str, visited: set[str]) -> set[str]:
                if item_norm in visited:
                    return {item_norm}
                visited = visited | {item_norm}
                prods = producers_by_norm.get(item_norm, [])
                if not prods:
                    return {item_norm}
                upstream_roots: set[str] = set()
                any_starved_prod = False
                for p in prods:
                    if p["missing_norms"]:
                        any_starved_prod = True
                        for m_norm in p["missing_norms"]:
                            upstream_roots |= find_root_norms(m_norm, visited)
                if any_starved_prod and upstream_roots:
                    return upstream_roots
                return {item_norm}

            def collect_cascade(root_norm: str) -> tuple[list[dict], list[dict], set[str]]:
                direct_lines = [
                    ln for ln in consumers_by_norm.get(root_norm, [])
                    if root_norm in ln["missing_norms"]
                ]
                affected_norms = {root_norm}
                cascade_lines: list[dict] = []
                frontier = [ln["recipe_norm"] for ln in direct_lines if ln["recipe_norm"]]
                seen_ln_keys = {ln["line_key"] for ln in direct_lines}

                while frontier:
                    curr_norm = frontier.pop(0)
                    if curr_norm in affected_norms:
                        continue
                    affected_norms.add(curr_norm)
                    for cons_ln in consumers_by_norm.get(curr_norm, []):
                        if cons_ln["line_key"] not in seen_ln_keys and (
                            cons_ln["is_issue"] or curr_norm in cons_ln["missing_norms"]
                        ):
                            seen_ln_keys.add(cons_ln["line_key"])
                            cascade_lines.append(cons_ln)
                            if cons_ln["recipe_norm"]:
                                frontier.append(cons_ln["recipe_norm"])

                return direct_lines, cascade_lines, affected_norms

            station_root_norms: dict[str, str] = {}
            for ln in st_lines:
                for m_norm in ln["missing_norms"]:
                    for r_norm in find_root_norms(m_norm, set()):
                        if r_norm not in station_root_norms:
                            disp = ln["missing_display_map"].get(r_norm)
                            if not disp:
                                for p in producers_by_norm.get(r_norm, []):
                                    disp = p["recipe_display"]
                                    break
                            if not disp:
                                disp = r_norm.title()
                            station_root_norms[r_norm] = disp

            for r_norm, r_disp in station_root_norms.items():
                direct_lns, cascade_lns, affected_norms = collect_cascade(r_norm)
                if not direct_lns:
                    continue

                direct_machines = sum(ln["starved_cnt"] for ln in direct_lns)
                cascade_machines = sum(ln["total_cnt"] for ln in cascade_lns)
                st_stock = storage_by_norm.get(r_norm, 0)

                prods = producers_by_norm.get(r_norm, [])
                if prods:
                    p_total = sum(p["total_cnt"] for p in prods)
                    p_run = sum(p["running_cnt"] for p in prods)
                    p_cpm = sum(p["total_cpm"] for p in prods)
                    prod_status = f"{p_run}/{p_total} local machines running ({p_cpm:.1f} cyc/m)"
                else:
                    prod_status = f"External supply / Import (Storage: {st_stock:,})"

                direct_desc = f"{direct_machines} machine(s) in " + ", ".join(
                    f"{ln['total_cnt']}× {ln['recipe_display']}" for ln in direct_lns[:3]
                )
                if cascade_lns:
                    cascade_desc = f"{cascade_machines} downstream in " + ", ".join(
                        f"{ln['total_cnt']}× {ln['recipe_display']}" for ln in cascade_lns[:3]
                    )
                else:
                    cascade_desc = "None (End-product line)"

                rc_id = f"RC::{st_id}::{r_norm}"
                rc_obj = {
                    "rc_id": rc_id,
                    "station_id": st_id,
                    "station_label": format_station_label(st_id, station_names.get(st_id)),
                    "root_norm": r_norm,
                    "resource_display": r_disp,
                    "cause_text": f"Buffer Empty (Storage: {st_stock:,})",
                    "direct_desc": direct_desc,
                    "cascade_desc": cascade_desc,
                    "producer_status": prod_status,
                    "affected_norms": affected_norms,
                    "severity": direct_machines + cascade_machines,
                    "tag": "st_bad",
                }
                self.root_cause_lookup[rc_id] = rc_obj
                root_causes.append(rc_obj)

            for ln in st_lines:
                if ln["blocked_cnt"] > 0:
                    rc_id = f"RCBLK::{ln['line_key']}"
                    rc_obj = {
                        "rc_id": rc_id,
                        "station_id": st_id,
                        "station_label": format_station_label(st_id, station_names.get(st_id)),
                        "root_norm": ln["recipe_norm"],
                        "resource_display": ln["recipe_display"],
                        "cause_text": f"Output Full / Blocked ({ln['blocked_cnt']}/{ln['total_cnt']})",
                        "direct_desc": f"{ln['blocked_cnt']}× {ln['machine_type']} halted",
                        "cascade_desc": "Output belt/storage backed up",
                        "producer_status": "Clear output buffer or add storage",
                        "affected_norms": {ln["recipe_norm"]},
                        "severity": ln["blocked_cnt"],
                        "tag": "st_warn",
                    }
                    self.root_cause_lookup[rc_id] = rc_obj
                    root_causes.append(rc_obj)

        root_causes.sort(key=lambda r: (-r["severity"], r["station_label"], r["resource_display"]))
        return root_causes, lines_by_station

    # ==========================================================================
    # 100% EFFICIENCY MATERIAL BALANCE & CAPACITY ENGINE
    # ==========================================================================
    def _global_progress_rate(self) -> float:
        """Progress units/sec at 100% speed: override, else median of calibrated machines."""
        if PROGRESS_UNITS_PER_SEC_OVERRIDE:
            return float(PROGRESS_UNITS_PER_SEC_OVERRIDE)
        vals = sorted(v for v in self.progress_rate_peak.values() if v > 0)
        return vals[len(vals) // 2] if vals else 0.0

    def _estimate_theoretical_cpm(self, m: dict, observed_cpm: float) -> float:
        """100% efficiency cycles/min = (progress units/sec at 100% speed * 60) / MaxProgress."""
        max_prog = m.get("max_progress", 0)
        overspeed_mult = max(0.1, 1.0 + (m.get("overspeed", 0) / 100.0))
        if max_prog > 0:
            if PROGRESS_UNITS_PER_SEC_OVERRIDE:
                rate = float(PROGRESS_UNITS_PER_SEC_OVERRIDE)
            else:
                rate = self.progress_rate_peak.get(m.get("id"), 0.0) or self._global_progress_rate()
            if rate > 0:
                return (rate * 60.0 / max_prog) * overspeed_mult
        # Not calibrated yet (no machine seen working) -> fall back to observed rate
        return observed_cpm

    def _compute_material_balance(self, machines: list[dict], stock_map: dict[str, int], station_filter: str | None = None) -> list[dict]:
        """
        Computes:
          1. Direct 100% demand from every configured machine.
          2. Propagated 100% demand (what upstream machines & raw ores MUST produce if every
             downstream machine ran continuously at 100% without starving).
          3. Max theoretical supply capacity of existing producer machines.
        """
        scoped_machines = [
            m for m in machines
            if (station_filter is None or m.get("station_id") == station_filter)
            and m.get("recipe") and m.get("recipe") != "None"
        ]

        recipes_by_out: dict[str, dict] = {}
        max_supply_rate: dict[str, float] = {}
        producer_counts: dict[str, int] = {}
        producer_types: dict[str, str] = {}
        display_names: dict[str, str] = {}
        direct_demand_rate: dict[str, float] = {}

        for m in scoped_machines:
            out_norm = normalize_item_key(m["recipe"])
            if not out_norm:
                continue
            display_names.setdefault(out_norm, format_item_name(m["recipe"]))

            obs_cpm = self.machine_metrics.get(m["id"], {}).get("cycles_per_min", 0.0)
            theo_cpm = self._estimate_theoretical_cpm(m, obs_cpm)

            rec_in_ratios = m.get("recipe_inputs") or {k: 1 for k in m.get("expected_inputs", [])}
            rec_out_ratios = m.get("recipe_outputs") or {m["recipe"]: 1}
            out_per_cycle = sum(rec_out_ratios.values()) or 1

            max_supply_rate[out_norm] = max_supply_rate.get(out_norm, 0.0) + (theo_cpm * out_per_cycle)
            producer_counts[out_norm] = producer_counts.get(out_norm, 0) + 1
            producer_types[out_norm] = get_machine_type_label(m["id"], m.get("name"))

            norm_inputs: dict[str, int] = {}
            for raw_in, qty_per_cyc in rec_in_ratios.items():
                in_norm = normalize_item_key(raw_in)
                if not in_norm:
                    continue
                display_names.setdefault(in_norm, format_item_name(raw_in))
                norm_inputs[in_norm] = norm_inputs.get(in_norm, 0) + qty_per_cyc
                direct_demand_rate[in_norm] = direct_demand_rate.get(in_norm, 0.0) + (theo_cpm * qty_per_cyc)

            recipes_by_out[out_norm] = {
                "out_per_cycle": out_per_cycle,
                "inputs": norm_inputs,
            }

        # Propagate 100% downstream demand upward so intermediate machine deficits also reveal full raw ore demand
        propagated_demand: dict[str, float] = dict(direct_demand_rate)
        for _ in range(12):
            changed = False
            for item_norm, req_rate in list(propagated_demand.items()):
                rec = recipes_by_out.get(item_norm)
                if not rec:
                    continue
                target_cycles = req_rate / rec["out_per_cycle"]
                for in_norm, in_qty in rec["inputs"].items():
                    implied_in_req = target_cycles * in_qty
                    cur_val = propagated_demand.get(in_norm, 0.0)
                    if implied_in_req > cur_val + 1e-6:
                        propagated_demand[in_norm] = implied_in_req
                        changed = True
            if not changed:
                break

        norm_stock: dict[str, int] = {}
        for k, v in stock_map.items():
            nk = normalize_item_key(k)
            if nk:
                norm_stock[nk] = norm_stock.get(nk, 0) + v
                display_names.setdefault(nk, format_item_name(k))

        all_items = set(propagated_demand.keys()) | set(max_supply_rate.keys())
        rows = []
        for item_norm in all_items:
            demand = propagated_demand.get(item_norm, 0.0)
            supply = max_supply_rate.get(item_norm, 0.0)
            net = supply - demand
            stock = norm_stock.get(item_norm, 0)
            is_raw = item_norm not in recipes_by_out

            if is_raw:
                tier = "Raw / Import"
                if demand <= 0:
                    status_tag = "st_idle"
                    action = "Unused raw stock"
                elif stock <= 0:
                    status_tag = "st_bad"
                    action = f"RAW SOURCE DEFICIT: Need {demand:.1f}/m — add miners / import route"
                else:
                    mins_buffer = stock / demand
                    if mins_buffer < 5.0:
                        status_tag = "st_warn"
                        action = f"LOW RAW BUFFER ({mins_buffer:.1f}m left) — ensure ≥ {demand:.1f}/m mining"
                    else:
                        status_tag = "st_ok"
                        action = f"Raw supply OK ({mins_buffer:.0f}m buffer at 100% draw)"
            else:
                tier = "End Product" if demand == 0 else "Intermediate"
                cnt = producer_counts.get(item_norm, 0)
                mtype = producer_types.get(item_norm, "Machine")
                per_machine_rate = (supply / cnt) if cnt > 0 else 0.0

                if demand > 0 and net < -0.05:
                    deficit = abs(net)
                    extra_needed = int((deficit / per_machine_rate) + 0.999) if per_machine_rate > 0 else 1
                    status_tag = "st_bad"
                    action = f"BUILD +{extra_needed}× {mtype} (short {deficit:.1f}/m at 100% load)"
                elif demand == 0:
                    status_tag = "st_ok"
                    action = f"Final output ({cnt}× {mtype} producing {supply:.1f}/m)"
                else:
                    surplus_pct = ((supply - demand) / demand) * 100.0
                    status_tag = "st_ok"
                    action = f"Sufficient (+{surplus_pct:.0f}% headroom across {cnt}× {mtype})"

            rows.append({
                "item_norm": item_norm,
                "name": display_names.get(item_norm, item_norm.title()),
                "tier": tier,
                "is_raw": is_raw,
                "demand": demand,
                "supply": supply,
                "net": net,
                "stock": stock,
                "action": action,
                "tag": status_tag,
            })

        rows.sort(key=lambda r: (
            0 if r["tag"] == "st_bad" else (1 if r["tag"] == "st_warn" else 2),
            0 if r["is_raw"] else 1,
            r["name"].lower(),
        ))
        return rows

    def _render_balance_tab(self, machines: list[dict], global_stock: dict[str, int], by_station: dict[str, dict[str, int]], station_names: dict[str, str], query: str):
        open_nodes = {
            iid: self.balance_tree.item(iid, "open")
            for iid in self.balance_tree.get_children()
            if iid.startswith("BALST::")
        }
        self.balance_tree.delete(*self.balance_tree.get_children())
        only_issues = self.issues_only.get()
        qty_filter_active = self.qty_filter_mode.get() != "Off"
        scope = self.balance_scope.get()

        if scope == "Per Station":
            total_shortages = 0
            for st_id in sorted(by_station.keys(), key=lambda s: format_station_label(s, station_names.get(s)).lower()):
                st_label = format_station_label(st_id, station_names.get(st_id))
                rows = self._compute_material_balance(machines, by_station.get(st_id, {}), station_filter=st_id)
                visible = []
                for r in rows:
                    if only_issues and r["tag"] not in ("st_bad", "st_warn"):
                        continue
                    if qty_filter_active and not self._matches_qty_filter(r["stock"]):
                        continue
                    if query and query not in r["name"].lower() and query not in st_label.lower() and query not in r["action"].lower() and query not in r["tier"].lower():
                        continue
                    visible.append(r)

                if not visible:
                    continue
                deficits = sum(1 for r in visible if r["tag"] == "st_bad")
                total_shortages += deficits
                st_iid = f"BALST::{st_id}"
                st_node = self.balance_tree.insert(
                    "",
                    tk.END,
                    iid=st_iid,
                    text=f"  {st_label}",
                    values=("Station", "—", "—", "—", "—", f"{deficits} shortage(s)" if deficits else "● Balanced"),
                    open=open_nodes.get(st_iid, True),
                    tags=("station_header",),
                )
                for r in visible:
                    sup_str = "External / Ore" if r["is_raw"] else f"{r['supply']:.1f}"
                    net_str = "—" if r["is_raw"] else f"{r['net']:+.1f}"
                    self.balance_tree.insert(
                        st_node,
                        tk.END,
                        text=f"  {r['name']}",
                        values=(r["tier"], f"{r['demand']:.1f}", sup_str, net_str, f"{r['stock']:,}", r["action"]),
                        tags=(r["tag"],),
                    )
            self.balance_title_label.config(
                text=f"100% EFFICIENCY MATERIAL BALANCE — Per Station ({total_shortages} deficit(s) highlighted)"
            )
        else:
            rows = self._compute_material_balance(machines, global_stock, station_filter=None)
            deficits = 0
            for r in rows:
                if only_issues and r["tag"] not in ("st_bad", "st_warn"):
                    continue
                if qty_filter_active and not self._matches_qty_filter(r["stock"]):
                    continue
                if query and query not in r["name"].lower() and query not in r["action"].lower() and query not in r["tier"].lower():
                    continue
                if r["tag"] == "st_bad":
                    deficits += 1
                sup_str = "External / Ore" if r["is_raw"] else f"{r['supply']:.1f}"
                net_str = "—" if r["is_raw"] else f"{r['net']:+.1f}"
                self.balance_tree.insert(
                    "",
                    tk.END,
                    text=f"  {r['name']}",
                    values=(r["tier"], f"{r['demand']:.1f}", sup_str, net_str, f"{r['stock']:,}", r["action"]),
                    tags=(r["tag"],),
                )
            self.balance_title_label.config(
                text=f"100% EFFICIENCY MATERIAL BALANCE — Global Factory ({deficits} deficit(s) highlighted)"
            )

    # ==========================================================================
    # DATA REFRESH & RENDERING
    # ==========================================================================
    def refresh_data(self, force: bool = False):
        path = self.json_path.get()
        if not os.path.exists(path):
            self.status_label.config(text="Waiting for JSON file... (Press F9 in-game or wait for Lua auto-export)")
            return

        try:
            mtime = os.path.getmtime(path)
            is_new_export = mtime != self.last_file_mtime
            if not force and not is_new_export and self.last_data is not None:
                self._update_relative_timestamps()
                self.status_label.config(
                    text=f"Checked {time.strftime('%H:%M:%S')} — No new export since {self.last_data.get('timestamp', 'unknown')}"
                )
                return

            with open(path, "r", encoding="utf-8") as f:
                data = restrict_to_main_base(json.load(f))

            if self.last_data is not None and is_new_export:
                self.previous_global = dict(self.last_data.get("global_stock", {}))
                self.previous_station = {
                    st: dict(items) for st, items in self.last_data.get("by_station", {}).items()
                }
                self.previous_machines = {
                    m["id"]: m for m in self.last_data.get("machines", [])
                }

            sample_ts = mtime or time.time()
            if is_new_export or not self.history_global:
                global_stock = data.get("global_stock", {})
                by_station = data.get("by_station", {})

                for item_raw, qty in global_stock.items():
                    self._record_sample(self.history_global, item_raw, qty, sample_ts)

                for st_raw, items in by_station.items():
                    st_total = sum(items.values())
                    self._record_sample(self.history_station, f"{st_raw}::__TOTAL__", st_total, sample_ts)
                    for item_raw, qty in items.items():
                        self._record_sample(self.history_station, f"{st_raw}::{item_raw}", qty, sample_ts)

                self._save_history()

            if is_new_export or not self.machine_metrics:
                self._update_machine_metrics(data.get("machines", []), sample_ts)

            self.last_file_mtime = mtime
            self.last_data = data
            self._render_tables()

            export_ts = data.get("timestamp", "Unknown")
            sample_count = max((len(v["samples"]) for v in self.history_global.values()), default=0)
            self.status_label.config(
                text=f"Export: {export_ts}  |  History: {sample_count}/{MAX_HISTORY_SAMPLES} samples  |  UI Updated: {time.strftime('%H:%M:%S')}"
            )
        except (json.JSONDecodeError, PermissionError) as e:
            self.status_label.config(text=f"File busy ({type(e).__name__}), will retry next cycle...")
        except Exception as e:
            self.status_label.config(text=f"Error reading JSON: {e}")

    def _render_tables(self):
        if not self.last_data:
            return

        now_ts = time.time()
        query = self.search_query.get().strip().lower()
        qty_filter_active = self.qty_filter_mode.get() != "Off"
        global_stock: dict[str, int] = self.last_data.get("global_stock", {})
        by_station: dict[str, dict[str, int]] = self.last_data.get("by_station", {})
        station_limits: dict[str, dict[str, int]] = self.last_data.get("station_limits", {})
        station_names: dict[str, str] = self.last_data.get("station_names", {})
        machines: list[dict] = self.last_data.get("machines", [])

        root_causes, lines_by_station = self._analyze_production_topology(machines, by_station, station_names)
        total_lines = sum(len(v) for v in lines_by_station.values())

        self.summary_label.config(
            text=f"Stations: {len(by_station)}  |  Lines: {total_lines} ({len(machines)} machines)  |  Root Causes: {len(root_causes)}"
        )

        self._render_root_causes_tree(root_causes, query)
        self._render_grouped_lines_tree(lines_by_station, station_names, query)
        self._render_balance_tab(machines, global_stock, by_station, station_names, query)
        self._refresh_inspector_panel()

        open_stations = {
            iid.removeprefix("ST::"): self.station_tree.item(iid, "open")
            for iid in self.station_tree.get_children()
        }

        self._active_images.clear()

        # Populate Station / Storage Tree
        self.station_tree.delete(*self.station_tree.get_children())
        sorted_stations = sorted(
            by_station.keys(),
            key=lambda st_id: format_station_label(st_id, station_names.get(st_id)).lower(),
        )

        for station_raw in sorted_stations:
            items = by_station[station_raw]
            custom_name = station_names.get(station_raw)
            station_display = format_station_label(station_raw, custom_name)
            station_matches = (
                query in station_display.lower()
                or query in station_raw.lower()
                or (custom_name and query in custom_name.lower())
            )

            filtered_items = []
            station_total_units = 0
            for item_raw, qty in sorted(items.items(), key=lambda kv: format_item_name(kv[0])):
                station_total_units += qty
                item_display = format_item_name(item_raw)
                text_ok = not query or station_matches or query in item_display.lower() or query in item_raw.lower()
                qty_ok = self._matches_qty_filter(qty)
                if text_ok and qty_ok:
                    filtered_items.append((item_raw, item_display, qty))

            if not filtered_items and (qty_filter_active or (query and not station_matches)):
                continue

            st_hist = self.history_station.get(f"{station_raw}::__TOTAL__", {})
            st_samples = list(st_hist.get("samples", []))
            st_img = self.sparklines.get_image(st_samples, self.bg_header)
            self._active_images.append(st_img)

            st_delta = (st_samples[-1] - st_samples[-2]) if len(st_samples) >= 2 else 0
            st_delta_str = f"{st_delta:+,}" if st_delta != 0 else "—"
            st_updated_str = format_elapsed(st_hist.get("last_changed"), now_ts)

            st_limits_map = station_limits.get(station_raw, {})
            limit_stock_map = self.last_data.get("station_limit_stock", {}).get(station_raw, {})
            limited_qty_sum = sum(limit_stock_map.get(k, items.get(k, 0)) for k in st_limits_map)
            limited_cap_sum = sum(st_limits_map.values())
            st_lim_str, _, _ = self._evaluate_limit_status(limited_qty_sum, limited_cap_sum)

            is_open = open_stations.get(station_raw, True)
            parent_iid = self.station_tree.insert(
                "",
                tk.END,
                iid=f"ST::{station_raw}",
                text=f"  {station_display}",
                image=st_img,
                values=(f"{station_total_units:,} total", st_lim_str, st_delta_str, st_updated_str),
                open=is_open,
                tags=("station_header",),
            )

            prev_st_items = self.previous_station.get(station_raw, {})
            for idx, (item_raw, item_display, qty) in enumerate(filtered_items):
                delta = qty - prev_st_items.get(item_raw, qty)
                delta_str, base_tag = self._format_delta(delta, idx)
                row_bg = self.bg_panel if idx % 2 == 0 else self.bg_row_alt

                item_limit = st_limits_map.get(item_raw)
                lim_str, _, lim_tag = self._evaluate_limit_status(limit_stock_map.get(item_raw, qty), item_limit)
                row_tag = lim_tag if lim_tag else base_tag

                hist_key = f"{station_raw}::{item_raw}"
                item_hist = self.history_station.get(hist_key, {})
                item_samples = list(item_hist.get("samples", []))
                item_img = self.sparklines.get_image(item_samples, row_bg)
                self._active_images.append(item_img)

                updated_str = format_elapsed(item_hist.get("last_changed"), now_ts)

                self.station_tree.insert(
                    parent_iid,
                    tk.END,
                    iid=f"STI::{hist_key}",
                    text=f"  {item_display}",
                    image=item_img,
                    values=(f"{qty:,}", lim_str, delta_str, updated_str),
                    tags=(row_tag,),
                )

        # Populate Global Totals Tree
        self.global_tree.delete(*self.global_tree.get_children())
        row_idx = 0
        for item_raw, qty in sorted(global_stock.items(), key=lambda kv: format_item_name(kv[0])):
            item_display = format_item_name(item_raw)
            if query and query not in item_display.lower() and query not in item_raw.lower():
                continue
            if not self._matches_qty_filter(qty):
                continue

            delta = qty - self.previous_global.get(item_raw, qty)
            delta_str, tag = self._format_delta(delta, row_idx)
            row_bg = self.bg_panel if row_idx % 2 == 0 else self.bg_row_alt

            g_hist = self.history_global.get(item_raw, {})
            g_samples = list(g_hist.get("samples", []))
            g_img = self.sparklines.get_image(g_samples, row_bg)
            self._active_images.append(g_img)

            updated_str = format_elapsed(g_hist.get("last_changed"), now_ts)

            self.global_tree.insert(
                "",
                tk.END,
                iid=f"GLB::{item_raw}",
                text=f"  {item_display}",
                image=g_img,
                values=(f"{qty:,}", delta_str, updated_str),
                tags=(tag,),
            )
            row_idx += 1

    def _render_root_causes_tree(self, root_causes: list[dict], query: str):
        self.rc_tree.delete(*self.rc_tree.get_children())
        if not root_causes:
            self.rc_title_label.config(text="ROOT-CAUSE BOTTLENECKS (None detected — all active lines supplied)")
            self.rc_tree.insert(
                "",
                tk.END,
                text="  ● All production lines have required inputs",
                values=("All Stations", "No bottlenecks", "0 stalled", "0 cascading", "Optimal"),
                tags=("rc_healthy",),
            )
            return

        self.rc_title_label.config(
            text=f"ROOT-CAUSE BOTTLENECKS ({len(root_causes)} primary cause(s) — click a row to trace its downstream impact)"
        )
        for rc in root_causes:
            if query:
                blob = f"{rc['resource_display']} {rc['station_label']} {rc['direct_desc']} {rc['cascade_desc']}".lower()
                if query not in blob:
                    continue
            self.rc_tree.insert(
                "",
                tk.END,
                iid=rc["rc_id"],
                text=f"  ● {rc['resource_display']}",
                values=(
                    rc["station_label"],
                    rc["cause_text"],
                    rc["direct_desc"],
                    rc["cascade_desc"],
                    rc["producer_status"],
                ),
                tags=(rc["tag"],),
            )

    def _render_grouped_lines_tree(self, lines_by_station: dict[str, list[dict]], station_names: dict[str, str], query: str):
        open_nodes = {
            iid: self.line_tree.item(iid, "open")
            for iid in self.line_tree.get_children()
        }
        for st_iid in list(open_nodes.keys()):
            for ln_iid in self.line_tree.get_children(st_iid):
                open_nodes[ln_iid] = self.line_tree.item(ln_iid, "open")

        self.line_tree.delete(*self.line_tree.get_children())

        sorted_stations = sorted(
            lines_by_station.keys(),
            key=lambda st_id: format_station_label(st_id, station_names.get(st_id)).lower(),
        )

        only_issues = self.issues_only.get()
        qty_filter_active = self.qty_filter_mode.get() != "Off"
        trace_st, trace_norm, _ = self.active_trace_filter if self.active_trace_filter else (None, None, None)

        first_selectable_iid = None

        for st_id in sorted_stations:
            if trace_st and st_id != trace_st:
                continue

            st_lines = lines_by_station[st_id]
            st_display = format_station_label(st_id, station_names.get(st_id))
            st_matches = query in st_display.lower() or query in st_id.lower()

            st_lines_sorted = sorted(
                st_lines,
                key=lambda ln: (0 if ln["is_issue"] else 1, ln["recipe_display"].lower()),
            )

            visible_lines = []
            for ln in st_lines_sorted:
                if trace_norm:
                    rc_obj = next((r for r in self.root_cause_lookup.values() if r["station_id"] == st_id and r["root_norm"] == trace_norm), None)
                    affected_norms = rc_obj["affected_norms"] if rc_obj else {trace_norm}
                    in_norms = {normalize_item_key(k) for k in ln["all_input_keys"]}
                    if ln["recipe_norm"] not in affected_norms and not (in_norms & affected_norms):
                        continue

                if only_issues and not ln["is_issue"]:
                    continue

                if qty_filter_active:
                    if not ln["all_input_keys"]:
                        continue
                    if not any(self._matches_qty_filter(ln["agg_inputs"].get(k, 0)) for k in ln["all_input_keys"]):
                        continue

                if query and not st_matches:
                    in_names = " ".join(format_item_name(k).lower() for k in ln["all_input_keys"])
                    haystack = f"{ln['recipe_display'].lower()} {ln['machine_type'].lower()} {ln['status_label'].lower()} {in_names}"
                    if query not in haystack:
                        continue

                visible_lines.append(ln)

            if not visible_lines:
                continue

            total_st_machines = sum(ln["total_cnt"] for ln in st_lines)
            running_st_machines = sum(ln["running_cnt"] for ln in st_lines)
            issue_lines_cnt = sum(1 for ln in st_lines if ln["is_issue"])
            st_cpm = sum(ln["total_cpm"] for ln in st_lines)

            st_iid = f"LST::{st_id}"
            st_health = f"{issue_lines_cnt} Line Issue(s)" if issue_lines_cnt > 0 else "● All Optimal"
            st_node = self.line_tree.insert(
                "",
                tk.END,
                iid=st_iid,
                text=f"  {st_display}",
                values=(
                    f"{running_st_machines}/{total_st_machines}",
                    st_health,
                    f"{st_cpm:.1f} cyc/m",
                    f"{len(st_lines)} recipe line(s)",
                    "—",
                ),
                open=open_nodes.get(st_iid, True),
                tags=("station_header",),
            )

            for ln in visible_lines:
                ln_iid = f"LINE::{ln['line_key']}"
                if first_selectable_iid is None:
                    first_selectable_iid = ln_iid

                line_title = f"  {ln['total_cnt']}× {ln['machine_type']} — {ln['recipe_display']}"
                active_str = f"{ln['running_cnt']}/{ln['total_cnt']}"
                tput_str = f"{ln['total_cpm']:.1f} cyc/m" if ln["total_cpm"] > 0 else "0.0 cyc/m"

                if ln["all_input_keys"]:
                    pills = []
                    for k in ln["all_input_keys"]:
                        val = ln["agg_inputs"].get(k, 0)
                        short_name = format_item_name(k)
                        if val <= 0:
                            pills.append(f"{short_name}: 0 ⚠")
                        else:
                            pills.append(f"{short_name}: {val:,}")
                    in_compact = "  |  ".join(pills)
                else:
                    in_compact = "No Inputs"

                out_total = sum(v for v in ln["agg_outputs"].values() if v > 0)
                out_str = f"{out_total:,} units" if out_total > 0 else "0"

                self.line_tree.insert(
                    st_node,
                    tk.END,
                    iid=ln_iid,
                    text=line_title,
                    values=(active_str, ln["status_label"], tput_str, in_compact, out_str),
                    open=open_nodes.get(ln_iid, False),
                    tags=(ln["tag"],),
                )

                for m in ln["machines"]:
                    mid = m["id"]
                    m_iid = f"MCH::{mid}"
                    mh = m["health"]
                    mcpm = m["metric"].get("cycles_per_min", 0.0)
                    mtput = f"{mcpm:.1f} cyc/m" if mcpm > 0 else "0.0 cyc/m"
                    m_in_str = ", ".join(
                        f"{format_item_name(k)}: {m.get('inputs', {}).get(k, 0):,}"
                        for k in ln["all_input_keys"]
                    ) or "—"
                    m_out = sum(v for v in m.get("outputs", {}).values() if v > 0)
                    sub_tag = "sub_bad" if mh["state"] in ("STARVED", "BLOCKED", "ERROR") else ("sub_warn" if mh["state"] == "SLOW" else "sub_ok")

                    self.line_tree.insert(
                        ln_iid,
                        tk.END,
                        iid=m_iid,
                        text=f"    ↳ {format_machine_label(mid, m.get('name'))}",
                        values=(
                            "1/1" if mh["state"] == "OK" else "0/1",
                            mh["label"],
                            mtput,
                            m_in_str,
                            f"{m_out:,}",
                        ),
                        tags=(sub_tag,),
                    )

        if self.selected_inspector_target is None and first_selectable_iid:
            self.line_tree.selection_set(first_selectable_iid)
            self._on_line_selected()

    # ==========================================================================
    # INTERACTIVE ROOT-CAUSE FILTER & GRAPHICAL FLOW DIAGRAM
    # ==========================================================================
    def _on_root_cause_selected(self, _event=None):
        sel = self.rc_tree.selection()
        if not sel:
            return
        rc_id = sel[0]
        rc = self.root_cause_lookup.get(rc_id)
        if not rc:
            return

        self.active_trace_filter = (rc["station_id"], rc["root_norm"], rc["resource_display"])
        self.trace_chip_label.config(text=f"Tracing Bottleneck: {rc['resource_display']} @ {rc['station_label']}")
        self.trace_chip_frame.pack(side=tk.LEFT, padx=(12, 0))
        self._render_tables()

    def _on_line_selected(self, _event=None):
        sel = self.line_tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("LINE::"):
            self.selected_inspector_target = ("LINE", iid.removeprefix("LINE::"))
        elif iid.startswith("MCH::"):
            self.selected_inspector_target = ("MCH", iid.removeprefix("MCH::"))
        else:
            return
        self._refresh_inspector_panel()

    def _jump_to_line(self, line_key: str):
        """Selects a production line in the left tree and updates the flow diagram."""
        ln_iid = f"LINE::{line_key}"
        if self.line_tree.exists(ln_iid):
            parent_iid = self.line_tree.parent(ln_iid)
            if parent_iid:
                self.line_tree.item(parent_iid, open=True)
            self.line_tree.selection_set(ln_iid)
            self.line_tree.see(ln_iid)
        self.selected_inspector_target = ("LINE", line_key)
        self._refresh_inspector_panel()

    def _resolve_active_inspection_context(self):
        if not self.selected_inspector_target:
            return None, None
        kind, key = self.selected_inspector_target
        if kind == "LINE":
            return self.line_group_lookup.get(key), None
        if kind == "MCH":
            m = self.machine_lookup.get(key)
            if m:
                lkey = f"{m['station_id']}::{m['machine_type']}::{m['recipe']}"
                return self.line_group_lookup.get(lkey), m
        return None, None

    def _draw_flow_node(
        self,
        c: tk.Canvas,
        x: int,
        y: int,
        w: int,
        h: int,
        icon: str,
        title: str,
        subtitle: str,
        metric_line: str,
        border_color: str,
        fill_color: str,
        badge_text: str,
        badge_color: str,
        click_line_key: str | None = None,
    ):
        """Draws a crisp schematic node card on the flow canvas with ports on left/right edges."""
        tag_name = f"node_{x}_{y}"
        # Drop shadow
        c.create_rectangle(x + 3, y + 3, x + w + 3, y + h + 3, fill="#080b10", outline="", tags=tag_name)
        # Main card body
        c.create_rectangle(x, y, x + w, y + h, fill=fill_color, outline=border_color, width=2, tags=tag_name)
        # Left colored status bar
        c.create_rectangle(x, y, x + 6, y + h, fill=border_color, outline="", tags=tag_name)

        # Icon + Title
        c.create_text(
            x + 15,
            y + 15,
            text=f"{icon}  {title}",
            anchor="w",
            fill="#ffffff",
            font=("Segoe UI", 9, "bold"),
            width=w - 24,
            tags=tag_name,
        )
        # Subtitle (machine type / source)
        c.create_text(
            x + 15,
            y + 34,
            text=subtitle,
            anchor="w",
            fill=self.fg_muted,
            font=("Segoe UI", 8),
            width=w - 24,
            tags=tag_name,
        )
        # Metric / Buffer line
        c.create_text(
            x + 15,
            y + 52,
            text=metric_line,
            anchor="w",
            fill=self.fg_primary,
            font=("Segoe UI", 8, "bold"),
            width=w - 24,
            tags=tag_name,
        )
        # Status badge at bottom
        c.create_text(
            x + 15,
            y + 69,
            text=badge_text,
            anchor="w",
            fill=badge_color,
            font=("Segoe UI", 8, "bold"),
            width=w - 24,
            tags=tag_name,
        )

        # Connection ports (left & right midpoints)
        cy = y + h // 2
        c.create_oval(x - 4, cy - 4, x + 4, cy + 4, fill=border_color, outline="#0d1117", width=1, tags=tag_name)
        c.create_oval(x + w - 4, cy - 4, x + w + 4, cy + 4, fill=border_color, outline="#0d1117", width=1, tags=tag_name)

        if click_line_key:
            c.tag_bind(tag_name, "<Button-1>", lambda _e, lk=click_line_key: self._jump_to_line(lk))
            c.tag_bind(tag_name, "<Enter>", lambda _e: c.configure(cursor="hand2"))
            c.tag_bind(tag_name, "<Leave>", lambda _e: c.configure(cursor=""))

    def _draw_flow_edge(
        self,
        c: tk.Canvas,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        rate_label: str,
        color: str,
        is_blocked: bool,
        lane_x: int | None = None,
        label_side: str = "start",
        pill_w: int | None = None,
    ):
        """Draws an orthogonal directional arrow. The throughput pill is queued and drawn later,
        on top of every line, so no arrow can ever strike through label text."""
        if lane_x is None:
            lane_x = (x1 + x2) // 2
        dash_pattern = (5, 4) if is_blocked else ()

        c.create_line(
            x1, y1, lane_x, y1, lane_x, y2, x2 - 4, y2,
            fill=color,
            width=2.5 if not is_blocked else 2.0,
            dash=dash_pattern,
            arrow=tk.LAST,
            arrowshape=(9, 11, 4),
        )

        if pill_w is None:
            pill_w = self._pill_width(rate_label)
        if label_side == "end":
            left = x2 - 14 - pill_w
            cy = y2
        else:
            left = x1 + 8
            cy = y1
        self._edge_labels.append((left, cy, pill_w, rate_label, color))

    def _pill_width(self, text: str) -> int:
        try:
            if getattr(self, "_pill_font", None) is None:
                self._pill_font = tkfont.Font(family="Segoe UI", size=8, weight="bold")
            return self._pill_font.measure(text) + 20
        except Exception:
            return max(72, len(text) * 7 + 20)

    def _flush_edge_labels(self, c: tk.Canvas):
        """Draws all queued throughput pills last so they sit above every line and node."""
        for left, cy, pw, text, color in self._edge_labels:
            c.create_rectangle(left, cy - 10, left + pw, cy + 10, fill="#141922", outline=color, width=1)
            c.create_text(left + pw // 2, cy, text=text, fill=color, font=("Segoe UI", 8, "bold"))
        self._edge_labels = []

    def _draw_flow_diagram(self):
        """Renders left-to-right graphical material flow diagram for the inspected production line."""
        c = self.flow_canvas
        c.delete("all")
        self._edge_labels = []

        line_obj, single_machine = self._resolve_active_inspection_context()
        if not line_obj:
            c.create_text(
                24,
                24,
                text="Select a production line on the left to view its live flow diagram.",
                anchor="nw",
                fill=self.fg_muted,
                font=("Segoe UI", 10),
            )
            return

        st_id = line_obj["station_id"]
        graph = self.station_graphs.get(st_id, {"producers": {}, "consumers": {}, "storage": {}, "limits": {}})
        target_inputs = single_machine.get("inputs", {}) if single_machine else line_obj["agg_inputs"]

        # 1. Build Upstream Nodes (one or more per required input)
        upstream_specs = []
        if not line_obj["all_input_keys"]:
            upstream_specs.append({
                "icon": "⚡",
                "title": "No Material Inputs",
                "subtitle": "Direct / Passive Process",
                "metric": "—",
                "color": self.fg_muted,
                "badge": "READY",
                "rate_text": "—",
                "blocked": False,
                "click_key": None,
            })
        else:
            for in_k in line_obj["all_input_keys"]:
                in_disp = format_item_name(in_k)
                in_norm = normalize_item_key(in_k)
                buf_qty = target_inputs.get(in_k, 0)
                st_stock = graph["storage"].get(in_norm, 0)
                prods = graph["producers"].get(in_norm, [])

                if prods:
                    for p in prods:
                        p_rate = p["total_ipm"] if p["total_ipm"] > 0 else p["total_cpm"]
                        is_starved_edge = (buf_qty <= 0 and p_rate <= 0)
                        is_slow_edge = (buf_qty <= 0 and p_rate > 0) or p["is_issue"]

                        if is_starved_edge:
                            color = self.red
                            badge = f"● BOTTLENECK ({p['status_label']})"
                            rate_txt = "0.0 u/m • STARVED"
                            blocked = True
                        elif is_slow_edge:
                            color = self.amber
                            badge = f"▲ {p['status_label']}"
                            rate_txt = f"{p_rate:.1f} u/m • DELAYED"
                            blocked = True
                        else:
                            color = self.green
                            badge = f"● {p['running_cnt']}/{p['total_cnt']} Running"
                            rate_txt = f"{p_rate:.1f} u/m"
                            blocked = False

                        upstream_specs.append({
                            "icon": "⚙",
                            "title": f"{p['total_cnt']}× {p['machine_type']}",
                            "subtitle": f"Crafts: {in_disp}",
                            "metric": f"Line Buf: {buf_qty:,}  |  Storage: {st_stock:,}",
                            "color": color,
                            "badge": badge,
                            "rate_text": rate_txt,
                            "blocked": blocked,
                            "click_key": p["line_key"],
                        })
                else:
                    # External import / station storage source with limit percentage scaling
                    st_limit = graph.get("limits", {}).get(in_norm, 0)
                    lim_stock = graph.get("limit_stock", {}).get(in_norm, st_stock)
                    _, lim_color, _ = self._evaluate_limit_status(lim_stock, st_limit)

                    if st_limit > 0:
                        pct = (lim_stock / st_limit) * 100.0
                        color = lim_color
                        badge = (
                            f"● LIMIT {pct:.0f}% (AT TARGET)"
                            if pct >= 100
                            else (f"▲ LIMIT {pct:.0f}% (WARNING)" if pct >= 50 else f"● LIMIT {pct:.0f}% (ALERT)")
                        )
                        rate_txt = f"{pct:.0f}% of {st_limit:,}"
                        metric_txt = f"Storage: {lim_stock:,} / {st_limit:,}  |  Buf: {buf_qty:,}"
                    else:
                        is_empty = (buf_qty <= 0 and st_stock <= 0)
                        color = self.red if is_empty else (self.amber if buf_qty <= 0 else self.green)
                        badge = "● EMPTY STORAGE" if is_empty else ("▲ BELT DELAY (0 in buf)" if buf_qty <= 0 else "● SUPPLIED")
                        rate_txt = "0.0 u/m • EMPTY" if is_empty else ("0.0 u/m • WAIT" if buf_qty <= 0 else f"Stock: {st_stock:,}")
                        metric_txt = f"Machine Buf: {buf_qty:,}  |  Storage: {st_stock:,}"

                    upstream_specs.append({
                        "icon": "📦",
                        "title": in_disp,
                        "subtitle": "Station Storage / External Import",
                        "metric": metric_txt,
                        "color": color,
                        "badge": badge,
                        "rate_text": rate_txt,
                        "blocked": (buf_qty <= 0 and st_stock <= 0),
                        "click_key": None,
                    })

        # 2. Build Center Node (Selected Line or Individual Machine)
        if single_machine:
            mh = single_machine["health"]
            c_state = mh["state"]
            c_color = self.red if c_state in ("STARVED", "BLOCKED", "ERROR") else (self.amber if c_state == "SLOW" else (self.green if c_state == "OK" else self.fg_muted))
            c_title = format_machine_label(single_machine["id"], single_machine.get("name"))
            c_sub = f"Recipe: {line_obj['recipe_display']}"
            out_qty = sum(v for v in single_machine.get("outputs", {}).values() if v > 0)
            c_rate = single_machine["metric"].get("items_per_min", 0.0) or single_machine["metric"].get("cycles_per_min", 0.0)
            c_metric = f"Rate: {c_rate:.1f} u/m  |  Out Buf: {out_qty:,}"
            c_badge = mh["label"]
        else:
            c_state = line_obj["state"]
            c_color = self.red if c_state in ("STARVED", "BLOCKED") else (self.amber if c_state == "SLOW" else (self.green if c_state == "OK" else self.fg_muted))
            c_title = f"{line_obj['total_cnt']}× {line_obj['machine_type']}"
            c_sub = f"Recipe: {line_obj['recipe_display']}"
            out_qty = sum(v for v in line_obj["agg_outputs"].values() if v > 0)
            c_rate = line_obj["total_ipm"] if line_obj["total_ipm"] > 0 else line_obj["total_cpm"]
            c_metric = f"Active: {line_obj['running_cnt']}/{line_obj['total_cnt']}  |  Out: {out_qty:,}"
            c_badge = line_obj["status_label"]

        # 3. Build Downstream Consumer Nodes
        rec_norm = line_obj["recipe_norm"]
        consumers = graph["consumers"].get(rec_norm, []) if rec_norm else []
        downstream_specs = []
        if not consumers:
            downstream_specs.append({
                "icon": "🏁",
                "title": "End Product / Storage",
                "subtitle": f"Output: {line_obj['recipe_display']}",
                "metric": f"Output Buffer: {out_qty:,} units",
                "color": self.accent if out_qty > 0 else self.fg_muted,
                "badge": "STORED / EXPORTED",
                "rate_text": f"{c_rate:.1f} u/m",
                "blocked": (c_rate <= 0),
                "click_key": None,
            })
        else:
            for cons in consumers:
                waiting = rec_norm in cons["missing_norms"]
                cons_in_qty = sum(
                    v for k, v in cons["agg_inputs"].items()
                    if normalize_item_key(k) == rec_norm
                )
                if waiting:
                    d_color = self.amber
                    d_badge = "▲ WAITING FOR THIS INPUT"
                    d_rate = f"{c_rate:.1f} u/m • STARVED" if c_rate <= 0 else f"{c_rate:.1f} u/m • UNDERSUPPLIED"
                    d_blocked = True
                elif cons["is_issue"]:
                    d_color = self.red
                    d_badge = cons["status_label"]
                    d_rate = f"{c_rate:.1f} u/m"
                    d_blocked = False
                else:
                    d_color = self.green
                    d_badge = f"● {cons['running_cnt']}/{cons['total_cnt']} Running"
                    d_rate = f"{c_rate:.1f} u/m"
                    d_blocked = (c_rate <= 0)

                downstream_specs.append({
                    "icon": "⚙",
                    "title": f"{cons['total_cnt']}× {cons['machine_type']}",
                    "subtitle": f"Makes: {cons['recipe_display']}",
                    "metric": f"Input Buf ({line_obj['recipe_display']}): {cons_in_qty:,}",
                    "color": d_color,
                    "badge": d_badge,
                    "rate_text": d_rate,
                    "blocked": d_blocked,
                    "click_key": cons["line_key"],
                })

        # Layout geometry calculation
        node_w = 215
        node_h = 84
        gap_y = 18
        min_gap = 125
        lane_step = 8
        margin_x = 18
        margin_y = 32

        up_pills = [self._pill_width(sp["rate_text"]) for sp in upstream_specs]
        down_pills = [self._pill_width(sp["rate_text"]) for sp in downstream_specs]
        up_pill_max = max(up_pills, default=0)
        down_pill_max = max(down_pills, default=0)

        need_left = up_pill_max + 8 + 14 + len(upstream_specs) * lane_step + 30
        need_right = down_pill_max + 14 + 14 + len(downstream_specs) * lane_step + 30
        gap_left = max(min_gap, need_left)
        gap_right = max(min_gap, need_right)

        max_rows = max(len(upstream_specs), 1, len(downstream_specs))
        total_h = margin_y * 2 + max_rows * node_h + max(0, max_rows - 1) * gap_y

        base_w = margin_x * 2 + node_w * 3 + gap_left + gap_right
        spare = max(0, c.winfo_width() - base_w)
        gap_left += min(spare // 2, 160)
        gap_right += min(spare - spare // 2, 160)

        total_w = margin_x * 2 + node_w * 3 + gap_left + gap_right
        canvas_w = max(c.winfo_width(), total_w)
        canvas_h = max(c.winfo_height(), total_h)

        # Column X coordinates
        x_up = margin_x
        x_mid = x_up + node_w + gap_left
        x_down = x_mid + node_w + gap_right

        # Column Headers
        c.create_text(x_up, 12, text="UPSTREAM SOURCES", anchor="w", fill=self.fg_muted, font=("Segoe UI", 8, "bold"))
        c.create_text(x_mid, 12, text="INSPECTED STAGE", anchor="w", fill=self.accent, font=("Segoe UI", 8, "bold"))
        c.create_text(x_down, 12, text="DOWNSTREAM CONSUMERS", anchor="w", fill=self.fg_muted, font=("Segoe UI", 8, "bold"))

        # Center node Y position
        y_mid = (canvas_h - node_h) // 2

        # Draw Upstream Nodes + Edges -> Center
        up_block_h = len(upstream_specs) * node_h + max(0, len(upstream_specs) - 1) * gap_y
        up_start_y = max(margin_y, (canvas_h - up_block_h) // 2)

        for idx, spec in enumerate(upstream_specs):
            ny = up_start_y + idx * (node_h + gap_y)
            self._draw_flow_node(
                c,
                x_up,
                ny,
                node_w,
                node_h,
                spec["icon"],
                spec["title"],
                spec["subtitle"],
                spec["metric"],
                spec["color"],
                "#181e29",
                spec["badge"],
                spec["color"],
                click_line_key=spec["click_key"],
            )
            self._draw_flow_edge(
                c,
                x_up + node_w,
                ny + node_h // 2,
                x_mid,
                y_mid + node_h // 2,
                spec["rate_text"],
                spec["color"],
                spec["blocked"],
                lane_x=x_up + node_w + 8 + up_pill_max + 14 + idx * lane_step,
                label_side="start",
                pill_w=up_pills[idx],
            )

        # Draw Downstream Nodes + Edges <- Center
        down_block_h = len(downstream_specs) * node_h + max(0, len(downstream_specs) - 1) * gap_y
        down_start_y = max(margin_y, (canvas_h - down_block_h) // 2)

        for idx, spec in enumerate(downstream_specs):
            ny = down_start_y + idx * (node_h + gap_y)
            self._draw_flow_node(
                c,
                x_down,
                ny,
                node_w,
                node_h,
                spec["icon"],
                spec["title"],
                spec["subtitle"],
                spec["metric"],
                spec["color"],
                "#181e29",
                spec["badge"],
                spec["color"],
                click_line_key=spec["click_key"],
            )
            self._draw_flow_edge(
                c,
                x_mid + node_w,
                y_mid + node_h // 2,
                x_down,
                ny + node_h // 2,
                spec["rate_text"],
                spec["color"],
                spec["blocked"],
                lane_x=x_down - 14 - down_pill_max - 14 - idx * lane_step,
                label_side="end",
                pill_w=down_pills[idx],
            )

        # Draw Center Inspected Node last so it sits cleanly on top
        c.create_rectangle(
            x_mid - 4,
            y_mid - 4,
            x_mid + node_w + 4,
            y_mid + node_h + 4,
            fill="",
            outline=self.accent,
            width=1,
            dash=(3, 2),
        )
        self._draw_flow_node(
            c,
            x_mid,
            y_mid,
            node_w,
            node_h,
            "🏭",
            c_title,
            c_sub,
            c_metric,
            c_color,
            "#1f2736",
            c_badge,
            c_color,
            click_line_key=None,
        )

        self._flush_edge_labels(c)
        c.configure(scrollregion=(0, 0, total_w, total_h))

    def _refresh_inspector_panel(self):
        self._draw_flow_diagram()

        self.inspector_text.config(state=tk.NORMAL)
        self.inspector_text.delete("1.0", tk.END)

        line_obj, single_machine = self._resolve_active_inspection_context()
        if not line_obj:
            self.inspector_text.insert(tk.END, "Select a production line on the left\n", "h1")
            self.inspector_text.insert(
                tk.END,
                "Click any recipe line or individual machine to inspect its upstream input suppliers, buffer levels, and downstream consumers.",
                "sub",
            )
            self.inspector_text.config(state=tk.DISABLED)
            return

        st_id = line_obj["station_id"]
        station_names = self.last_data.get("station_names", {}) if self.last_data else {}
        st_label = format_station_label(st_id, station_names.get(st_id))
        graph = self.station_graphs.get(st_id, {"producers": {}, "consumers": {}, "storage": {}, "limits": {}})

        if single_machine:
            self.inspector_text.insert(tk.END, f"{format_machine_label(single_machine['id'], single_machine.get('name'))}\n", "h1")
            self.inspector_text.insert(tk.END, f"Recipe: {line_obj['recipe_display']}  •  {st_label}\n\n", "sub")
            mh = single_machine["health"]
            tag = "bad" if mh["state"] in ("STARVED", "BLOCKED", "ERROR") else ("warn" if mh["state"] == "SLOW" else "ok")
            self.inspector_text.insert(tk.END, f"Status: {mh['label']}\n", tag)
            prog = single_machine.get("progress", 0)
            mprog = single_machine.get("max_progress", 0)
            pct = f"{(prog / mprog) * 100:.1f}%" if mprog > 0 else "0%"
            cpm = single_machine["metric"].get("cycles_per_min", 0.0)
            self.inspector_text.insert(tk.END, f"Cycle Progress: {pct}   |   Rate: {cpm:.2f} cyc/m\n\n", "detail")
        else:
            self.inspector_text.insert(tk.END, f"{line_obj['total_cnt']}× {line_obj['machine_type']} — {line_obj['recipe_display']}\n", "h1")
            self.inspector_text.insert(tk.END, f"Station: {st_label}\n\n", "sub")
            tag = "bad" if line_obj["state"] in ("STARVED", "BLOCKED") else ("warn" if line_obj["state"] == "SLOW" else "ok")
            self.inspector_text.insert(tk.END, f"Line Health: {line_obj['status_label']}\n", tag)
            self.inspector_text.insert(
                tk.END,
                f"Active Machines: {line_obj['running_cnt']} / {line_obj['total_cnt']}   |   Total Throughput: {line_obj['total_cpm']:.2f} cyc/m\n\n",
                "detail",
            )

        self.inspector_text.insert(tk.END, "UPSTREAM INPUTS & SUPPLIERS\n", "sec")
        self.inspector_text.insert(tk.END, "────────────────────────────────────────\n", "detail")

        target_inputs = single_machine.get("inputs", {}) if single_machine else line_obj["agg_inputs"]
        if not line_obj["all_input_keys"]:
            self.inspector_text.insert(tk.END, "No recipe inputs required.\n\n", "detail")
        else:
            for in_k in line_obj["all_input_keys"]:
                in_disp = format_item_name(in_k)
                in_norm = normalize_item_key(in_k)
                buf_qty = target_inputs.get(in_k, 0)
                st_stock = graph["storage"].get(in_norm, 0)
                st_limit = graph.get("limits", {}).get(in_norm, 0)

                if buf_qty <= 0:
                    self.inspector_text.insert(tk.END, f"✖ {in_disp}: 0 in machine buffer\n", "bad")
                else:
                    self.inspector_text.insert(tk.END, f"● {in_disp}: {buf_qty:,} in buffer\n", "ok")

                if st_limit > 0:
                    lim_stock = graph.get("limit_stock", {}).get(in_norm, st_stock)
                    lim_pct = (lim_stock / st_limit) * 100.0
                    lim_tag = "ok" if lim_pct >= 100.0 else ("warn" if lim_pct >= 50.0 else "bad")
                    self.inspector_text.insert(
                        tk.END,
                        f"   Station Storage: {lim_stock:,} / {st_limit:,} ({lim_pct:.0f}% of limit)\n",
                        lim_tag,
                    )
                else:
                    self.inspector_text.insert(tk.END, f"   Station Storage: {st_stock:,} units\n", "detail")

                prods = graph["producers"].get(in_norm, [])
                if prods:
                    for p in prods:
                        p_tag = "bad" if p["is_issue"] else "detail"
                        self.inspector_text.insert(
                            tk.END,
                            f"   ↳ Produced by: {p['total_cnt']}× {p['machine_type']} ({p['running_cnt']}/{p['total_cnt']} running, {p['total_cpm']:.1f} cyc/m) — {p['status_label']}\n",
                            p_tag,
                        )
                else:
                    self.inspector_text.insert(tk.END, "   ↳ Source: External import / raw resource (no local producer)\n", "detail")
                self.inspector_text.insert(tk.END, "\n")

        self.inspector_text.insert(tk.END, "DOWNSTREAM CONSUMERS (ON STATION)\n", "sec")
        self.inspector_text.insert(tk.END, "────────────────────────────────────────\n", "detail")
        rec_norm = line_obj["recipe_norm"]
        consumers = graph["consumers"].get(rec_norm, []) if rec_norm else []
        if not consumers:
            self.inspector_text.insert(tk.END, "No downstream machines on this station consume this output (End Product / Export).\n", "detail")
        else:
            for c in consumers:
                waiting = rec_norm in c["missing_norms"]
                c_tag = "warn" if waiting else "item"
                wait_txt = "  [WAITING ON THIS OUTPUT]" if waiting else ""
                self.inspector_text.insert(
                    tk.END,
                    f"→ {c['total_cnt']}× {c['machine_type']} ({c['recipe_display']}){wait_txt}\n",
                    c_tag,
                )
                self.inspector_text.insert(
                    tk.END,
                    f"   Status: {c['status_label']}  |  Rate: {c['total_cpm']:.1f} cyc/m\n",
                    "detail",
                )

        self.inspector_text.config(state=tk.DISABLED)

    def _format_delta(self, delta: int, row_idx: int) -> tuple[str, str]:
        if delta > 0:
            return f"+{delta:,}", "gain"
        elif delta < 0:
            return f"{delta:,}", "loss"
        return "—", ("even" if row_idx % 2 == 0 else "odd")


class SingleInstance:
    """Allow only one running copy of the monitor.

    Holds a localhost TCP port for the lifetime of the process (the OS frees it automatically if the
    app crashes, so there are no stale lock files). A second launch connects to that port, asks the
    running copy to come to the front, and exits.
    """

    PORT = 47653
    HELLO = b"MSMON-SHOW"
    REPLY = b"MSMON-OK"

    def __init__(self):
        self._server: socket.socket | None = None
        self._raise_requested = threading.Event()

    def acquire(self) -> bool:
        """True if this process should run; False if another copy is already running."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", self.PORT))      # no SO_REUSEADDR on purpose: bind must fail if taken
            srv.listen(2)
        except OSError:
            srv.close()
            if self._ping_existing():
                print("Stock monitor is already running - brought the existing window to the front.")
                return False
            # Port is held by something that isn't us: don't block startup over it.
            print(f"[single-instance] port {self.PORT} is in use by another program; starting anyway.",
                  file=sys.stderr)
            return True
        self._server = srv
        threading.Thread(target=self._serve, daemon=True).start()
        return True

    def _ping_existing(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.PORT), timeout=1.0) as conn:
                conn.settimeout(1.0)
                conn.sendall(self.HELLO)
                return conn.recv(32) == self.REPLY
        except OSError:
            return False

    def _serve(self):
        srv = self._server
        while srv is not None:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return                                # socket closed on release()
            try:
                with conn:
                    conn.settimeout(1.0)
                    if conn.recv(32) == self.HELLO:
                        self._raise_requested.set()
                        conn.sendall(self.REPLY)
            except OSError:
                pass

    def watch(self, root: tk.Tk):
        """Poll (on the Tk thread) for a second launch asking us to come to the front."""
        def poll():
            if self._raise_requested.is_set():
                self._raise_requested.clear()
                try:
                    if root.state() == "iconic":
                        root.deiconify()
                    was_topmost = bool(root.attributes("-topmost"))
                    root.lift()
                    root.attributes("-topmost", True)
                    root.focus_force()
                    if not was_topmost:
                        root.after(300, lambda: root.attributes("-topmost", False))
                except tk.TclError:
                    pass
            try:
                root.after(400, poll)
            except tk.TclError:
                pass
        root.after(400, poll)

    def release(self):
        srv, self._server = self._server, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass


if __name__ == "__main__":
    guard = SingleInstance()
    if not guard.acquire():
        sys.exit(0)
    root = tk.Tk()
    app = StockMonitorApp(root)
    guard.watch(root)
    try:
        root.mainloop()
    finally:
        guard.release()
