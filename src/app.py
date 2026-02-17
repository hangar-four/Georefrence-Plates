
import csv
import io
import json
import math
import os
import pickle
import re
import shutil
import subprocess
import tempfile
import time
import threading
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from queue import Queue
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import pypdfium2 as pdfium
from pyproj import Transformer
import numpy as np


NASR_INDEX_URL = "https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/"
NASR_ZIP_TEMPLATE = "https://nfdc.faa.gov/webContent/28DaySub/28DaySubscription_Effective_{date}.zip"
DEFAULT_APRA_BASE = "https://external-api.faa.gov/apra"
DATA_DIR = Path("data")
NASR_DIR = DATA_DIR / "nasr"
CACHE_DIR = DATA_DIR / "cache"
TPP_DIR = DATA_DIR / "tpp"
CHARTS_DIR = DATA_DIR / "charts"
TPP_INDEX_FILE = DATA_DIR / "tpp_index.json"
LAST_NASR_DATE_FILE = DATA_DIR / "last_nasr_date.txt"
APRA_CONFIG_FILE = DATA_DIR / "apra_config.json"

DPI = 400
DEFAULT_OUTPUT_DIR = Path("output")

STATE_NAME_BY_CODE = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}


@dataclass
class RunwayEnd:
    ident: str
    lat: float
    lon: float


@dataclass
class Runway:
    ident: str
    length_ft: Optional[float]
    end1: RunwayEnd
    end2: RunwayEnd


@dataclass
class Airport:
    ident: str
    name: str
    lat: float
    lon: float
    state: str
    runways: List[Runway]


@dataclass
class FixPoint:
    ident: str
    lat: float
    lon: float
    kind: str


@dataclass
class TppChart:
    airport_id: str
    chart_name: str
    url: str


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("FAA Plate Georef (Runway-Click)")
        self.geometry("1200x820")
        try:
            self.state("zoomed")
        except Exception:
            pass

        self.airports: Dict[str, Airport] = {}
        self.filtered_airports: List[Airport] = []
        self.selected_airport: Optional[Airport] = None
        self.selected_runway: Optional[Runway] = None
        self.fixes: List[FixPoint] = []
        self.nearby_fixes: List[FixPoint] = []
        self.tpp_charts: List[TppChart] = []
        self.current_chart_name: str = ""

        self.pdf_path: Optional[Path] = None
        self.page_image: Optional[Image.Image] = None
        self.crop_box: Optional[Tuple[int, int, int, int]] = None
        self.display_scale: float = 1.0
        self.zoom: float = 1.0
        self.pan_x: float = 0.0
        self.pan_y: float = 0.0
        self.tk_image: Optional[ImageTk.PhotoImage] = None

        self.click_points: List[Tuple[float, float]] = []
        self.refine_points: List[Tuple[Tuple[float, float], FixPoint]] = []
        self._pending_refine_fix: Optional[FixPoint] = None
        self._target_circle_id: Optional[int] = None
        self._apra_token: Optional[str] = None
        self._apra_token_expiry: float = 0.0
        self.geo_results: List[Dict[str, str]] = []

        self._build_ui()
        self._load_last_nasr_date()
        self._load_apra_config()
        self._refresh_status()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=8)
        top.columnconfigure(7, weight=1)

        ttk.Button(top, text="Update NASR", command=self.on_update_nasr).grid(
            row=0, column=0, padx=(0, 10)
        )
        ttk.Button(top, text="APRA Keys", command=self.on_apra_keys).grid(
            row=0, column=1, padx=(0, 10)
        )
        ttk.Label(top, text="NASR date:").grid(row=0, column=2, sticky="w")
        self.nasr_date_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.nasr_date_var, width=12).grid(
            row=0, column=3, padx=(4, 12)
        )

        ttk.Button(top, text="Load PDF", command=self.on_load_pdf).grid(
            row=0, column=4, padx=(0, 10)
        )

        ttk.Label(top, text="Search Airport:").grid(row=0, column=5, sticky="w")
        self.search_var = tk.StringVar(value="")
        search_entry = ttk.Entry(top, textvariable=self.search_var)
        search_entry.grid(row=0, column=6, sticky="ew", padx=(4, 10))
        search_entry.bind("<KeyRelease>", lambda _e: self.on_search())

        ttk.Label(top, text="Runway:").grid(row=0, column=7, sticky="w")
        self.runway_var = tk.StringVar(value="")
        self.runway_combo = ttk.Combobox(
            top, textvariable=self.runway_var, state="readonly", width=12
        )
        self.runway_combo.grid(row=0, column=8, sticky="w")
        self.runway_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_runway_select())

        plate_tab = ttk.Frame(self)
        plate_tab.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        plate_tab.columnconfigure(0, weight=1)
        plate_tab.rowconfigure(0, weight=1)
        plate_tab.rowconfigure(2, weight=1)

        mid = ttk.Panedwindow(plate_tab, orient=tk.HORIZONTAL)
        mid.grid(row=0, column=0, sticky="nsew", pady=(0, 6))

        left = ttk.Frame(mid)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        left.rowconfigure(3, weight=1)
        mid.add(left, weight=1)

        ttk.Label(left, text="Airports").grid(row=0, column=0, sticky="w")
        self.airport_list = tk.Listbox(left)
        self.airport_list.grid(row=1, column=0, sticky="nsew")
        self.airport_list.bind("<<ListboxSelect>>", lambda _e: self.on_airport_select())

        ttk.Label(left, text="Approaches").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.tpp_list = tk.Listbox(left)
        self.tpp_list.grid(row=3, column=0, sticky="nsew")
        ttk.Button(left, text="Fetch Approaches", command=self.on_fetch_tpp).grid(
            row=4, column=0, sticky="ew", pady=(6, 0)
        )
        ttk.Button(left, text="Load Selected PDF", command=self.on_load_selected_tpp).grid(
            row=5, column=0, sticky="ew", pady=(4, 0)
        )

        right = ttk.Frame(mid)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        mid.add(right, weight=3)

        self.canvas = tk.Canvas(
            right, background="#111", highlightthickness=1, highlightbackground="#444"
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Button-3>", self.on_pan_start)
        self.canvas.bind("<B3-Motion>", self.on_pan_drag)
        self.canvas.bind("<ButtonRelease-3>", self.on_pan_release)
        self.canvas.bind("<Button-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_drag)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_release)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Leave>", lambda _e: self._hide_target_cursor())
        self.canvas.bind("<Configure>", lambda _e: self._draw_image())

        controls = ttk.Frame(plate_tab)
        controls.grid(row=1, column=0, sticky="ew")
        controls.columnconfigure(9, weight=1)

        self.instruction_var = tk.StringVar(value="Load a PDF to start.")
        ttk.Label(controls, textvariable=self.instruction_var).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )

        ttk.Label(controls, text="Refine fix:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.fix_var = tk.StringVar(value="None")
        self.fix_combo = ttk.Combobox(
            controls, textvariable=self.fix_var, state="normal", width=24
        )
        self.fix_combo.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.fix_combo.bind("<KeyRelease>", lambda _e: self.on_fix_search())
        ttk.Button(controls, text="Refine", command=self.on_refine).grid(
            row=1, column=2, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR.resolve()))
        ttk.Label(controls, text="Output folder:").grid(
            row=1, column=3, sticky="w", padx=(16, 4), pady=(6, 0)
        )
        ttk.Entry(controls, textvariable=self.output_var, width=40).grid(
            row=1, column=4, sticky="w", pady=(6, 0)
        )
        ttk.Button(controls, text="Browse", command=self.on_browse).grid(
            row=1, column=5, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        self.show_target_var = tk.BooleanVar(value=True)
        self.target_radius_var = tk.IntVar(value=16)
        ttk.Checkbutton(
            controls, text="Waypoint target circle", variable=self.show_target_var
        ).grid(row=1, column=6, sticky="w", padx=(12, 0), pady=(6, 0))
        ttk.Label(controls, text="px:").grid(row=1, column=7, sticky="e", pady=(6, 0))
        ttk.Spinbox(
            controls, from_=6, to=60, width=5, textvariable=self.target_radius_var
        ).grid(row=1, column=8, sticky="w", pady=(6, 0))

        ttk.Button(controls, text="Generate KMZ", command=self.on_generate).grid(
            row=0, column=5, sticky="e"
        )
        ttk.Button(controls, text="Zoom +", command=self.on_zoom_in).grid(
            row=0, column=6, sticky="e", padx=(8, 0)
        )
        ttk.Button(controls, text="Zoom -", command=self.on_zoom_out).grid(
            row=0, column=7, sticky="e", padx=(4, 0)
        )
        ttk.Button(controls, text="Fit", command=self.on_zoom_fit).grid(
            row=0, column=8, sticky="e", padx=(4, 0)
        )

        status_frame = ttk.Frame(plate_tab)
        status_frame.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(status_frame, text="Session status:").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="")
        ttk.Label(status_frame, textvariable=self.status_var).grid(row=0, column=1, sticky="w", padx=(8, 0))

        log_frame = ttk.Frame(plate_tab)
        log_frame.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=10, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self._dragging = False
        self._drag_start = None
        self._crop_rect_id = None
        self._pan_anchor = None
        self._refine_bind_id: Optional[str] = None

        # GeoTIFF tab intentionally disabled for now.

    def _build_geo_tab(self, parent: ttk.Frame) -> None:
        frm = ttk.Frame(parent)
        frm.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        for c in range(8):
            frm.columnconfigure(c, weight=0)
        frm.columnconfigure(7, weight=1)

        ttk.Label(frm, text="Product").grid(row=0, column=0, sticky="w")
        self.geo_product_var = tk.StringVar(value="IFR Enroute")
        self.geo_product_combo = ttk.Combobox(
            frm,
            textvariable=self.geo_product_var,
            state="readonly",
            values=["IFR Enroute", "IFR Planning", "VFR Sectional", "VFR TAC"],
            width=16,
        )
        self.geo_product_combo.grid(row=0, column=1, sticky="w", padx=(4, 8))
        self.geo_product_combo.bind("<<ComboboxSelected>>", lambda _e: self._geo_product_changed())

        ttk.Label(frm, text="Edition").grid(row=0, column=2, sticky="w")
        self.geo_edition_var = tk.StringVar(value="current")
        ttk.Combobox(
            frm, textvariable=self.geo_edition_var, state="readonly", values=["current", "next"], width=10
        ).grid(row=0, column=3, sticky="w", padx=(4, 8))

        ttk.Label(frm, text="Geoname").grid(row=0, column=4, sticky="w")
        self.geo_geoname_var = tk.StringVar(value="US")
        self.geo_geoname_combo = ttk.Combobox(frm, textvariable=self.geo_geoname_var, width=24)
        self.geo_geoname_combo.grid(row=0, column=5, sticky="w", padx=(4, 8))

        ttk.Label(frm, text="Series").grid(row=0, column=6, sticky="w")
        self.geo_series_var = tk.StringVar(value="low")
        self.geo_series_combo = ttk.Combobox(
            frm, textvariable=self.geo_series_var, state="readonly", values=["low", "high", "area"], width=10
        )
        self.geo_series_combo.grid(row=0, column=7, sticky="w", padx=(4, 8))

        btns = ttk.Frame(parent)
        btns.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        ttk.Button(btns, text="Fetch GeoTIFF Links", command=self.on_geo_fetch).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text="Download Selected", command=self.on_geo_download_selected).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(btns, text="Download + Export KMZ", command=self.on_geo_download_export_kmz).grid(row=0, column=2)

        self.geo_list = tk.Listbox(parent)
        self.geo_list.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self._geo_product_changed()

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _load_last_nasr_date(self) -> None:
        if LAST_NASR_DATE_FILE.exists():
            self.nasr_date_var.set(LAST_NASR_DATE_FILE.read_text(encoding="utf-8").strip())

    def _load_apra_config(self) -> None:
        if not APRA_CONFIG_FILE.exists():
            return
        try:
            data = json.loads(APRA_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        # Do not log secrets.
        for key in (
            "APRA_CLIENT_ID",
            "APRA_CLIENT_SECRET",
            "APRA_CLIENT_ID_HEADER",
            "APRA_CLIENT_SECRET_HEADER",
            "APRA_API_KEY",
            "APRA_API_KEY_HEADER",
            "APRA_BASE_URL",
            "APRA_TOKEN_URL",
            "APRA_SCOPE",
        ):
            val = data.get(key)
            if isinstance(val, str) and val:
                os.environ[key] = val

    def _save_apra_config(self, data: Dict[str, str]) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        APRA_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def on_apra_keys(self) -> None:
        win = tk.Toplevel(self)
        win.title("APRA Credentials")
        win.geometry("520x360")
        win.grab_set()

        def row(label: str, var: tk.StringVar, r: int, show: Optional[str] = None) -> None:
            ttk.Label(win, text=label).grid(row=r, column=0, sticky="w", padx=10, pady=6)
            ttk.Entry(win, textvariable=var, width=48, show=show).grid(
                row=r, column=1, sticky="ew", padx=10, pady=6
            )

        win.columnconfigure(1, weight=1)
        v_client_id = tk.StringVar(value=os.environ.get("APRA_CLIENT_ID", ""))
        v_client_secret = tk.StringVar(value=os.environ.get("APRA_CLIENT_SECRET", ""))
        v_id_header = tk.StringVar(value=os.environ.get("APRA_CLIENT_ID_HEADER", "client_id"))
        v_secret_header = tk.StringVar(value=os.environ.get("APRA_CLIENT_SECRET_HEADER", "client_secret"))
        v_api_key = tk.StringVar(value=os.environ.get("APRA_API_KEY", ""))
        v_api_key_header = tk.StringVar(value=os.environ.get("APRA_API_KEY_HEADER", "x-api-key"))
        v_base_url = tk.StringVar(value=os.environ.get("APRA_BASE_URL", DEFAULT_APRA_BASE))
        v_token_url = tk.StringVar(value=os.environ.get("APRA_TOKEN_URL", ""))
        v_scope = tk.StringVar(value=os.environ.get("APRA_SCOPE", ""))

        row("Client ID", v_client_id, 0)
        row("Client Secret", v_client_secret, 1, show="*")
        row("Client ID Header", v_id_header, 2)
        row("Client Secret Header", v_secret_header, 3)
        row("API Key", v_api_key, 4, show="*")
        row("API Key Header", v_api_key_header, 5)
        row("APRA Base URL", v_base_url, 6)
        row("Token URL (optional)", v_token_url, 7)
        row("Scope (optional)", v_scope, 8)

        def save() -> None:
            data = {
                "APRA_CLIENT_ID": v_client_id.get().strip(),
                "APRA_CLIENT_SECRET": v_client_secret.get().strip(),
                "APRA_CLIENT_ID_HEADER": v_id_header.get().strip(),
                "APRA_CLIENT_SECRET_HEADER": v_secret_header.get().strip(),
                "APRA_API_KEY": v_api_key.get().strip(),
                "APRA_API_KEY_HEADER": v_api_key_header.get().strip(),
                "APRA_BASE_URL": v_base_url.get().strip(),
                "APRA_TOKEN_URL": v_token_url.get().strip(),
                "APRA_SCOPE": v_scope.get().strip(),
            }
            # Apply immediately
            for k, v in data.items():
                if v:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]
            self._save_apra_config(data)
            self._refresh_status()
            win.destroy()

        ttk.Button(win, text="Save", command=save).grid(row=9, column=0, pady=12, padx=10, sticky="w")
        ttk.Button(win, text="Cancel", command=win.destroy).grid(row=9, column=1, pady=12, padx=10, sticky="e")

    def on_browse(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)

    def on_update_nasr(self) -> None:
        threading.Thread(target=self._update_nasr_worker, daemon=True).start()

    def _update_nasr_worker(self) -> None:
        try:
            date = self.nasr_date_var.get().strip()
            if not date:
                date = self._fetch_current_nasr_date()
                self.nasr_date_var.set(date)
            LAST_NASR_DATE_FILE.write_text(date, encoding="utf-8")

            self._log(f"Downloading NASR {date}...")
            zip_path = self._download_nasr_zip(date)
            self._log(f"Saved: {zip_path}")

            cache_path = self._nasr_cache_path(date)
            if cache_path.exists():
                self._log(f"Loading NASR cache: {cache_path.name}...")
                self.airports, self.fixes = self._load_nasr_cache(cache_path)
            else:
                self._log("Parsing NASR CSVs...")
                self.airports, self.fixes = self._parse_nasr(zip_path)
                self._save_nasr_cache(cache_path, self.airports, self.fixes)
                self._log(f"Saved NASR cache: {cache_path.name}")
            self._log(f"Loaded {len(self.airports)} airports, {len(self.fixes)} fixes/navs.")
            self._refresh_status()
            self.on_search()
        except Exception as exc:
            if "not a zip" in str(exc).lower():
                zip_path = self._prompt_for_nasr_zip()
                if zip_path:
                    try:
                        date = self._date_from_filename(zip_path.name) or self.nasr_date_var.get().strip()
                        cache_path = self._nasr_cache_path(date) if date else None
                        if cache_path and cache_path.exists():
                            self._log(f"Loading NASR cache: {cache_path.name}...")
                            self.airports, self.fixes = self._load_nasr_cache(cache_path)
                        else:
                            self._log("Parsing NASR CSVs...")
                            self.airports, self.fixes = self._parse_nasr(zip_path)
                            if cache_path:
                                self._save_nasr_cache(cache_path, self.airports, self.fixes)
                                self._log(f"Saved NASR cache: {cache_path.name}")
                        self._log(
                            f"Loaded {len(self.airports)} airports, {len(self.fixes)} fixes/navs."
                        )
                        self._refresh_status()
                        self.on_search()
                        return
                    except Exception as exc2:
                        self._log(f"Error: {exc2}")
                        messagebox.showerror("NASR Update Failed", str(exc2))
                        return
            self._log(f"Error: {exc}")
            messagebox.showerror("NASR Update Failed", str(exc))

    def _nasr_cache_path(self, date: str) -> Path:
        safe = date.strip().replace("/", "-")
        return NASR_DIR / f"NASR_{safe}.pkl"

    def _date_from_filename(self, name: str) -> Optional[str]:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", name)
        if match:
            return match.group(1)
        return None

    def _save_nasr_cache(self, path: Path, airports: Dict[str, Airport], fixes: List[FixPoint]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({"airports": airports, "fixes": fixes}, f, protocol=pickle.HIGHEST_PROTOCOL)

    def _load_nasr_cache(self, path: Path) -> Tuple[Dict[str, Airport], List[FixPoint]]:
        with open(path, "rb") as f:
            data = pickle.load(f)
        airports = data.get("airports")
        fixes = data.get("fixes")
        if not isinstance(airports, dict) or not isinstance(fixes, list):
            raise RuntimeError("Invalid NASR cache format.")
        return airports, fixes

    def _prompt_for_nasr_zip(self) -> Optional[Path]:
        answer = messagebox.askyesno(
            "NASR Download Failed",
            "Download failed. Do you want to select a NASR ZIP file manually?",
        )
        if not answer:
            return None
        path = filedialog.askopenfilename(filetypes=[("NASR ZIP", "*.zip")])
        if not path:
            return None
        zip_path = Path(path)
        if not self._is_zip_file(zip_path):
            messagebox.showwarning("Invalid File", "Selected file is not a ZIP.")
            return None
        return zip_path

    def _fetch_current_nasr_date(self) -> str:
        resp = requests.get(NASR_INDEX_URL, timeout=30)
        resp.raise_for_status()
        text = resp.text
        matches = re.findall(r"Subscription effective ([A-Za-z]+ \d{1,2}, \d{4})", text)
        if not matches:
            raise RuntimeError("Could not find NASR effective date.")
        date_str = matches[1] if len(matches) > 1 else matches[0]
        dt = datetime.strptime(date_str, "%B %d, %Y")
        return dt.strftime("%Y-%m-%d")

    def _download_nasr_zip(self, date: str) -> Path:
        NASR_DIR.mkdir(parents=True, exist_ok=True)
        dest = NASR_DIR / f"NASR_{date}.zip"
        if dest.exists():
            return dest
        try:
            resp = self._apra_get("/nfdc/nasr/chart", params={"edition": "current"})
            if resp is not None:
                url = self._extract_first_url(resp.text)
                if url:
                    self._http_download(url, dest)
                    if self._is_zip_file(dest):
                        return dest
                    dest.unlink(missing_ok=True)
        except Exception:
            if dest.exists():
                dest.unlink(missing_ok=True)
        urls = self._resolve_nasr_zip_urls(date)
        last_error: Optional[Exception] = None
        for url in urls:
            try:
                self._http_download(url, dest, referer=f"{NASR_INDEX_URL}{date}/")
                if self._is_zip_file(dest):
                    return dest
                dest.unlink(missing_ok=True)
            except requests.RequestException as exc:
                last_error = exc
                if dest.exists():
                    dest.unlink(missing_ok=True)
                continue

        raise RuntimeError("Downloaded NASR file is not a ZIP.") from last_error

    def _resolve_nasr_zip_urls(self, date: str) -> List[str]:
        # Prefer links from the cycle page, then fallback to legacy template.
        page_urls = [
            f"{NASR_INDEX_URL}{date}/",
            f"https://www.faa.gov/air_traffic/flight_info/aeronav/Aero_Data/NASR_Subscription/{date}/",
        ]
        urls: List[str] = []
        for page_url in page_urls:
            try:
                resp = requests.get(page_url, timeout=30)
                resp.raise_for_status()
                links = re.findall(r'href="([^"]+)"', resp.text)
                for link in links:
                    if not link.lower().endswith(".zip"):
                        continue
                    urls.append(self._abs_url(page_url, link))
            except requests.RequestException:
                continue

        def rank(u: str) -> int:
            lu = u.lower()
            if "csv.zip" in lu or "_csv.zip" in lu:
                return 0
            if "subscription_effective" in lu:
                return 1
            if "nfdc.faa.gov" in lu:
                return 2
            return 3

        urls = sorted(dict.fromkeys(urls), key=rank)
        urls.append(NASR_ZIP_TEMPLATE.format(date=date))
        urls.append(f"https://nfdc.faa.gov/webContent/28DaySub/28DaySubscription_Effective_{date}_CSV.zip")
        urls.append(f"https://nfdc.faa.gov/webContent/28DaySub/28DaySubscription_Effective_{date}.zip")
        return urls

    def _abs_url(self, base: str, href: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return "https://www.faa.gov" + href
        if base.endswith("/"):
            return base + href
        return base + "/" + href

    def _is_zip_file(self, path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                sig = f.read(4)
            return sig == b"PK\x03\x04"
        except OSError:
            return False

    def _apra_get(self, path: str, params: Optional[Dict[str, str]] = None) -> Optional[requests.Response]:
        headers = self._apra_headers()
        if headers is None:
            return None
        base = self._apra_base().rstrip("/")
        url = f"{base}{path}"
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 404 and not url.endswith("/"):
            url = url + "/"
            resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp

    def _apra_base(self) -> str:
        return (os.environ.get("APRA_BASE_URL") or DEFAULT_APRA_BASE).strip()

    def _apra_headers(self) -> Optional[Dict[str, str]]:
        api_key = os.environ.get("APRA_API_KEY")
        api_key_header = os.environ.get("APRA_API_KEY_HEADER", "x-api-key")
        bearer = os.environ.get("APRA_BEARER_TOKEN")
        client_id_header = os.environ.get("APRA_CLIENT_ID_HEADER", "client_id")
        client_secret_header = os.environ.get("APRA_CLIENT_SECRET_HEADER", "client_secret")
        client_id = os.environ.get("APRA_CLIENT_ID")
        client_secret = os.environ.get("APRA_CLIENT_SECRET")
        token_url = os.environ.get("APRA_TOKEN_URL")
        scope = os.environ.get("APRA_SCOPE")

        if api_key:
            return {api_key_header: api_key}
        if client_id and client_secret:
            return {client_id_header: client_id, client_secret_header: client_secret}
        if bearer:
            return {"Authorization": f"Bearer {bearer}"}
        if token_url and client_id and client_secret:
            token = self._get_apra_token(token_url, client_id, client_secret, scope)
            return {"Authorization": f"Bearer {token}"}
        return None

    def _get_apra_token(self, token_url: str, client_id: str, client_secret: str, scope: Optional[str]) -> str:
        now = time.time()
        if self._apra_token and now < self._apra_token_expiry:
            return self._apra_token

        data = {"grant_type": "client_credentials"}
        if scope:
            data["scope"] = scope
        resp = requests.post(
            token_url,
            data=data,
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 3600))
        if not token:
            raise RuntimeError("APRA token response missing access_token.")
        self._apra_token = token
        self._apra_token_expiry = now + max(60, expires_in - 60)
        return token

    def _extract_first_url(self, text: str) -> Optional[str]:
        # JSON first
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k.lower() == "url" and isinstance(v, str):
                        return v
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "url" in item:
                        return item["url"]
        except Exception:
            pass
        # Fallback: any URL ending in .zip
        match = re.search(r"(https?://[^\\s\"']+\\.zip)", text, re.IGNORECASE)
        return match.group(1) if match else None

    def _parse_nasr(self, zip_path: Path) -> Tuple[Dict[str, Airport], List[FixPoint]]:
        airports: Dict[str, Airport] = {}
        runway_ends: Dict[Tuple[str, str], List[RunwayEnd]] = {}
        runway_len: Dict[Tuple[str, str], float] = {}
        fixes: List[FixPoint] = []

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            apt_base = self._find_csv(names, "APT_BASE")
            apt_rwy = self._find_csv(names, "APT_RWY")
            apt_rwy_end = self._find_csv(names, "APT_RWY_END")
            nav_base = self._find_csv(names, "NAV_BASE")
            fix_base = self._find_csv(names, "FIX_BASE")

            if not (apt_base and apt_rwy and apt_rwy_end):
                raise RuntimeError("Required APT CSV files not found in NASR zip.")

            airports = self._load_apt_base(zf, apt_base)
            runway_len = self._load_apt_rwy(zf, apt_rwy)
            runway_ends = self._load_apt_rwy_end(zf, apt_rwy_end)

            for (apt_id, rwy_id), ends in runway_ends.items():
                if apt_id not in airports or len(ends) != 2:
                    continue
                length_ft = runway_len.get((apt_id, rwy_id))
                run = Runway(
                    ident=rwy_id,
                    length_ft=length_ft,
                    end1=ends[0],
                    end2=ends[1],
                )
                airports[apt_id].runways.append(run)

            if nav_base:
                fixes.extend(self._load_nav_base(zf, nav_base))
            if fix_base:
                fixes.extend(self._load_fix_base(zf, fix_base))

        return airports, fixes

    def _find_csv(self, names: List[str], prefix: str) -> Optional[str]:
        for name in names:
            upper = name.upper()
            if upper.endswith(".CSV") and prefix in upper:
                return name
        return None

    def _load_apt_base(self, zf: zipfile.ZipFile, name: str) -> Dict[str, Airport]:
        rows = self._read_csv(zf, name)
        airports: Dict[str, Airport] = {}
        for row in rows:
            apt_id = self._get_any(row, ["ARPT_ID", "ARPT_IDENT", "LOCATION_IDENTIFIER", "APT_ID"])
            if not apt_id:
                continue
            name_val = self._get_any(row, ["ARPT_NAME", "APT_NAME", "NAME"]) or ""
            lat = self._parse_latlon(self._get_any(row, ["ARPT_LAT_DECIMAL", "LAT_DECIMAL", "LATITUDE"]))
            lon = self._parse_latlon(self._get_any(row, ["ARPT_LON_DECIMAL", "LONG_DECIMAL", "LONGITUDE"]))
            if lat is None or lon is None:
                continue
            state = (self._get_any(row, ["STATE_CODE", "STATE", "ARPT_STATE"]) or "").strip()
            airports[apt_id] = Airport(
                ident=apt_id,
                name=name_val,
                lat=lat,
                lon=lon,
                state=state,
                runways=[],
            )
        return airports

    def _load_apt_rwy(self, zf: zipfile.ZipFile, name: str) -> Dict[Tuple[str, str], float]:
        rows = self._read_csv(zf, name)
        out: Dict[Tuple[str, str], float] = {}
        for row in rows:
            apt_id = self._get_any(row, ["ARPT_ID", "ARPT_IDENT", "APT_ID"])
            rwy_id = self._get_any(row, ["RWY_ID", "RWY_IDENT", "RUNWAY_ID"])
            length = self._get_any(row, ["RWY_LEN", "RWY_LENGTH", "RUNWAY_LENGTH"])
            if not (apt_id and rwy_id and length):
                continue
            try:
                out[(apt_id, rwy_id)] = float(length)
            except ValueError:
                continue
        return out

    def _load_apt_rwy_end(self, zf: zipfile.ZipFile, name: str) -> Dict[Tuple[str, str], List[RunwayEnd]]:
        rows = self._read_csv(zf, name)
        out: Dict[Tuple[str, str], List[RunwayEnd]] = {}
        for row in rows:
            apt_id = self._get_any(row, ["ARPT_ID", "ARPT_IDENT", "APT_ID"])
            rwy_id = self._get_any(row, ["RWY_ID", "RWY_IDENT", "RUNWAY_ID"])
            end_id = self._get_any(row, ["RWY_END_ID", "RWY_END_IDENT", "RWY_END", "RUNWAY_END_ID"])
            lat = self._parse_latlon(self._get_any(row, ["RWY_END_LAT_DECIMAL", "LAT_DECIMAL", "LATITUDE"]))
            lon = self._parse_latlon(self._get_any(row, ["RWY_END_LON_DECIMAL", "LONG_DECIMAL", "LONGITUDE"]))
            if not (apt_id and rwy_id and end_id and lat is not None and lon is not None):
                continue
            out.setdefault((apt_id, rwy_id), []).append(RunwayEnd(end_id, lat, lon))
        return out

    def _load_nav_base(self, zf: zipfile.ZipFile, name: str) -> List[FixPoint]:
        rows = self._read_csv(zf, name)
        out: List[FixPoint] = []
        for row in rows:
            ident = self._get_any(row, ["NAV_ID", "NAV_IDENT", "IDENT"])
            lat = self._parse_latlon(self._get_any(row, ["LAT_DECIMAL", "LATITUDE", "NAV_LAT_DECIMAL"]))
            lon = self._parse_latlon(self._get_any(row, ["LONG_DECIMAL", "LONGITUDE", "NAV_LON_DECIMAL"]))
            if ident and lat is not None and lon is not None:
                out.append(FixPoint(ident=ident, lat=lat, lon=lon, kind="NAV"))
        return out

    def _load_fix_base(self, zf: zipfile.ZipFile, name: str) -> List[FixPoint]:
        rows = self._read_csv(zf, name)
        out: List[FixPoint] = []
        for row in rows:
            ident = self._get_any(row, ["FIX_ID", "IDENT", "FIX_IDENT"])
            lat = self._parse_latlon(self._get_any(row, ["LAT_DECIMAL", "LATITUDE", "FIX_LAT_DECIMAL"]))
            lon = self._parse_latlon(self._get_any(row, ["LONG_DECIMAL", "LONGITUDE", "FIX_LON_DECIMAL"]))
            if ident and lat is not None and lon is not None:
                out.append(FixPoint(ident=ident, lat=lat, lon=lon, kind="FIX"))
        return out

    def _read_csv(self, zf: zipfile.ZipFile, name: str) -> List[Dict[str, str]]:
        with zf.open(name) as f:
            data = f.read()
        text = data.decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(text))
        return [row for row in reader]

    def _get_any(self, row: Dict[str, str], keys: List[str]) -> Optional[str]:
        for k in keys:
            if k in row and row[k].strip():
                return row[k].strip()
        for k in row.keys():
            for key in keys:
                if key.lower() == k.lower() and row[k].strip():
                    return row[k].strip()
        return None

    def _parse_latlon(self, value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        v = value.strip()
        try:
            return float(v)
        except ValueError:
            pass
        match = re.match(r"(\d+)([NSEW])", v)
        if not match:
            return None
        digits = match.group(1)
        hemi = match.group(2)
        if len(digits) < 6:
            return None
        deg = int(digits[:-4])
        minutes = int(digits[-4:-2])
        seconds = float(digits[-2:])
        dec = deg + minutes / 60.0 + seconds / 3600.0
        if hemi in ("S", "W"):
            dec = -dec
        return dec

    def on_search(self) -> None:
        query = self.search_var.get().strip().lower()
        self.airport_list.delete(0, "end")
        self.filtered_airports.clear()

        if not self.airports:
            return

        for apt in self.airports.values():
            hay = f"{apt.ident} {apt.name}".lower()
            if query in hay:
                self.filtered_airports.append(apt)
                self.airport_list.insert("end", f"{apt.ident} | {apt.name}")

    def on_airport_select(self) -> None:
        if not self.airport_list.curselection():
            return
        idx = self.airport_list.curselection()[0]
        self.selected_airport = self.filtered_airports[idx]
        runways = [r.ident for r in self.selected_airport.runways]
        self.runway_combo["values"] = runways
        self.runway_var.set(runways[0] if runways else "")
        self.on_runway_select()

    def on_runway_select(self) -> None:
        if not self.selected_airport:
            return
        rwy_id = self.runway_var.get()
        self.selected_runway = next(
            (r for r in self.selected_airport.runways if r.ident == rwy_id), None
        )
        self._update_fix_list()
        self._reset_clicks()

    def _update_fix_list(self) -> None:
        if not self.selected_airport:
            return
        nearby = self._find_nearby_fixes(
            self.selected_airport.lat, self.selected_airport.lon, radius_nm=80
        )
        labels = ["None"] + [f"{f.ident} ({f.kind})" for f in nearby]
        self.fix_combo["values"] = labels
        self.fix_var.set("None")
        self.nearby_fixes = nearby

    def on_fix_search(self) -> None:
        query = self.fix_var.get().strip().upper()
        if not self.nearby_fixes:
            return
        if not query or query == "NONE":
            labels = ["None"] + [f"{f.ident} ({f.kind})" for f in self.nearby_fixes]
        else:
            labels = ["None"] + [
                f"{f.ident} ({f.kind})"
                for f in self.nearby_fixes
                if query in f.ident.upper() or query in f.kind.upper()
            ]
            if len(labels) == 1:
                labels = ["None"] + [f"{f.ident} ({f.kind})" for f in self.nearby_fixes]
        self.fix_combo["values"] = labels

    def on_fetch_tpp(self) -> None:
        if not self.selected_airport:
            messagebox.showwarning("Missing airport", "Select an airport first.")
            return
        threading.Thread(target=self._fetch_tpp_worker, daemon=True).start()

    def _fetch_tpp_worker(self) -> None:
        try:
            apt = self.selected_airport
            self._log("Building/loading TPP index...")
            charts = self._fetch_tpp_charts(apt)
            self.tpp_charts = charts
            self.tpp_list.delete(0, "end")
            for c in charts:
                self.tpp_list.insert("end", c.chart_name)
            self._log(f"Loaded {len(charts)} approach charts.")
            self._refresh_status()
        except Exception as exc:
            self._log(f"Error: {exc}")
            messagebox.showerror("TPP Fetch Failed", str(exc))

    def _fetch_tpp_charts(self, apt: Airport) -> List[TppChart]:
        index = self._ensure_tpp_index()
        charts: List[TppChart] = []
        ident = apt.ident.upper()
        for item in index.get("charts", []):
            apt_ident = str(item.get("airport_id", "")).upper()
            icao_ident = str(item.get("icao_id", "")).upper()
            if ident and ident not in (apt_ident, icao_ident):
                continue
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            charts.append(
                TppChart(
                    airport_id=apt_ident or icao_ident,
                    chart_name=str(item.get("chart_name", "")),
                    url=url,
                )
            )
        return charts

    def _parse_tpp_response(self, text: str) -> List[TppChart]:
        charts: List[TppChart] = []
        try:
            data = json.loads(text)
            items = []
            if isinstance(data, dict):
                for k in ("product", "products", "edition", "data"):
                    if k in data:
                        items = data[k]
                        break
            elif isinstance(data, list):
                items = data
            if isinstance(items, dict):
                items = [items]
            for item in items:
                if not isinstance(item, dict):
                    continue
                airport_id = str(item.get("airportId") or item.get("airport_id") or "")
                chart_name = str(item.get("chartName") or item.get("chart_name") or "")
                url = str(item.get("url") or "")
                if url and chart_name:
                    charts.append(TppChart(airport_id=airport_id, chart_name=chart_name, url=url))
        except Exception:
            pass

        if charts:
            return charts

        # XML fallback
        urls = re.findall(r"<url>([^<]+)</url>", text)
        names = re.findall(r"<chartName>([^<]+)</chartName>", text)
        airports = re.findall(r"<airportId>([^<]+)</airportId>", text)
        for i, url in enumerate(urls):
            chart_name = names[i] if i < len(names) else url.split("/")[-1]
            airport_id = airports[i] if i < len(airports) else ""
            charts.append(TppChart(airport_id=airport_id, chart_name=chart_name, url=url))
        return charts

    def _ensure_tpp_index(self) -> Dict[str, object]:
        cached = self._load_tpp_index()
        try:
            current_info = self._fetch_dtpp_info()
        except Exception as exc:
            if cached:
                self._log(f"Using cached TPP index (APRA unavailable): {exc}")
                return cached
            raise
        if cached and current_info.get("edition_date") == cached.get("edition_date"):
            return cached
        if cached:
            if not self._prompt_tpp_update(cached, current_info):
                self._log("Keeping existing TPP cache.")
                return cached
            self._clear_tpp_cache()
        index = self._build_tpp_index(current_info)
        self._save_tpp_index(index)
        return index

    def _prompt_tpp_update(self, cached: Dict[str, object], current_info: Dict[str, object]) -> bool:
        prev_date = str(cached.get("edition_date") or "unknown")
        prev_generated = str(cached.get("generated") or "unknown")
        new_date = str(current_info.get("edition_date") or "unknown")
        msg = (
            "A newer TPP edition is available.\n\n"
            f"Cached edition date: {prev_date}\n"
            f"Cached download time: {prev_generated}\n"
            f"Newest edition date: {new_date}\n\n"
            "Download newest now and delete old cached TPP zip files?"
        )
        return self._ask_yes_no("Update TPP Cache", msg)

    def _ask_yes_no(self, title: str, message: str) -> bool:
        # Ensure Tk dialog runs on the main thread even when called from worker thread.
        if threading.current_thread() is threading.main_thread():
            return messagebox.askyesno(title, message)
        q: Queue[bool] = Queue()

        def ask() -> None:
            q.put(messagebox.askyesno(title, message))

        self.after(0, ask)
        return q.get()

    def _clear_tpp_cache(self) -> None:
        if TPP_INDEX_FILE.exists():
            TPP_INDEX_FILE.unlink(missing_ok=True)
        if TPP_DIR.exists():
            for p in TPP_DIR.glob("DDTPP*.zip"):
                p.unlink(missing_ok=True)
        self._refresh_status()

    def _fetch_dtpp_info(self) -> Dict[str, object]:
        if self._apra_headers() is None:
            raise RuntimeError("APRA credentials not configured.")
        params_list = [
            {"edition": "current", "geoname": "US"},
            {"edition": "current"},
            {"edition": "CURRENT", "geoname": "US"},
            {"edition": "CURRENT"},
        ]
        last_error: Optional[Exception] = None
        for params in params_list:
            try:
                resp = self._apra_get("/dtpp/info", params=params)
                if resp is None:
                    continue
                info = self._parse_dtpp_info(resp.text)
                if info.get("urls"):
                    return info
                # Some APRA tenants return edition metadata only on /dtpp/info.
                chart_resp = self._apra_get("/dtpp/chart", params=params)
                if chart_resp is not None:
                    chart_info = self._parse_dtpp_info(chart_resp.text)
                    if chart_info.get("urls"):
                        if not chart_info.get("edition_date"):
                            chart_info["edition_date"] = info.get("edition_date", "")
                        return chart_info
                sample = " ".join(resp.text.split())[:220]
                self._log(f"TPP info parse had no URLs for params={params}; sample={sample}")
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(exc, "response", None).status_code if getattr(exc, "response", None) else None
                if status == 404:
                    self._log(f"TPP info 404 for params: {params}")
                    continue
                raise
        if last_error:
            raise RuntimeError("APRA dtpp/info returned 404. Check APRA access.") from last_error
        raise RuntimeError("Failed to retrieve TPP info from APRA.")

    def _parse_dtpp_info(self, text: str) -> Dict[str, object]:
        urls: List[str] = []
        edition_date = ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                editions = data.get("edition") or data.get("editions") or []
                if isinstance(editions, dict):
                    editions = [editions]
                for ed in editions:
                    if isinstance(ed, dict):
                        ed_date = str(ed.get("editionDate") or ed.get("edition_date") or "")
                        if ed_date and not edition_date:
                            edition_date = ed_date
                        prod = ed.get("product") or {}
                        if isinstance(prod, dict):
                            url = str(prod.get("url") or "")
                            if url:
                                urls.append(url)
        except Exception:
            pass

        # XML fallback for APRA's default productSet payload.
        if (not urls) or (not edition_date):
            try:
                root = ET.fromstring(text)
                for elem in root.iter():
                    tag = self._strip_xml_ns(elem.tag)
                    if tag == "editionDate" and not edition_date:
                        edition_date = (elem.text or "").strip()
                    if tag == "product":
                        url = (elem.attrib.get("url") or "").strip()
                        if url:
                            urls.append(url)
            except Exception:
                pass
        if not urls:
            urls = re.findall(r'url="([^"]+)"', text)
        if not edition_date:
            match = re.search(r"<editionDate>([^<]+)</editionDate>", text)
            if match:
                edition_date = match.group(1).strip()
        return {"edition_date": edition_date, "urls": urls}

    def _load_tpp_index(self) -> Optional[Dict[str, object]]:
        if not TPP_INDEX_FILE.exists():
            return None
        try:
            return json.loads(TPP_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_tpp_index(self, data: Dict[str, object]) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TPP_INDEX_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._refresh_status()

    def _build_tpp_index(self, info: Dict[str, object]) -> Dict[str, object]:
        urls = [u for u in info.get("urls", []) if isinstance(u, str)]
        if not urls:
            raise RuntimeError("TPP info response contained no URLs.")
        # Prefer the E zip which contains the metafile XML.
        e_urls = [u for u in urls if "DDTPPE" in u.upper()]
        use_urls = e_urls or urls
        TPP_DIR.mkdir(parents=True, exist_ok=True)
        charts: List[Dict[str, str]] = []
        for url in use_urls:
            zip_path = TPP_DIR / Path(url).name
            if not zip_path.exists():
                self._log(f"Downloading TPP bundle: {zip_path.name} (may be large)...")
                self._http_download(url, zip_path)
            self._log(f"Parsing metafile from {zip_path.name}...")
            charts.extend(self._parse_tpp_zip(zip_path))
        return {
            "edition_date": info.get("edition_date", ""),
            "generated": datetime.utcnow().isoformat() + "Z",
            "bundle_urls": urls,
            "charts": charts,
        }

    def _parse_tpp_zip(self, zip_path: Path) -> List[Dict[str, str]]:
        with zipfile.ZipFile(zip_path, "r") as zf:
            xml_name = None
            for name in zf.namelist():
                if name.lower().endswith("d-tpp_metafile.xml"):
                    xml_name = name
                    break
            if not xml_name:
                raise RuntimeError("d-TPP_Metafile.xml not found in TPP zip.")
            with zf.open(xml_name) as f:
                return self._parse_tpp_metafile(f)

    def _parse_tpp_metafile(self, fileobj: io.BufferedReader) -> List[Dict[str, str]]:
        charts: List[Dict[str, str]] = []
        current_volume = ""
        current_apt = ""
        current_icao = ""
        cycle = ""
        for event, elem in ET.iterparse(fileobj, events=("start", "end")):
            tag = self._strip_xml_ns(elem.tag)
            if event == "start":
                if tag == "digital_tpp":
                    cycle = elem.attrib.get("cycle", "").strip()
                elif tag == "city_name":
                    current_volume = elem.attrib.get("volume", "").strip()
                elif tag == "airport_name":
                    current_apt = elem.attrib.get("apt_ident", "").strip()
                    current_icao = elem.attrib.get("icao_ident", "").strip()
            elif event == "end":
                if tag == "record":
                    chart_name = self._find_xml_child_text(elem, "chart_name")
                    chart_code = self._find_xml_child_text(elem, "chart_code")
                    pdf_name = self._find_xml_child_text(elem, "pdf_name")
                    if not pdf_name or pdf_name.upper() in ("DELETED_JOB.PDF", "DEL_APT_SERVED.PDF"):
                        elem.clear()
                        continue
                    if not chart_name:
                        chart_name = pdf_name
                    if current_volume and cycle:
                        url = f"https://aeronav.faa.gov/d-tpp/{cycle}/{current_volume}/{pdf_name}"
                    else:
                        url = ""
                    label = chart_name
                    if chart_code:
                        label = f"{chart_code} - {chart_name}"
                    charts.append(
                        {
                            "airport_id": current_apt,
                            "icao_id": current_icao,
                            "chart_name": label.strip(),
                            "pdf_name": pdf_name,
                            "volume": current_volume,
                            "cycle": cycle,
                            "url": url,
                        }
                    )
                    elem.clear()
                elif tag == "airport_name":
                    current_apt = ""
                    current_icao = ""
                elif tag == "city_name":
                    current_volume = ""
        return charts

    def _strip_xml_ns(self, tag: str) -> str:
        if "}" in tag:
            return tag.split("}", 1)[1]
        return tag

    def _find_xml_child_text(self, elem: ET.Element, name: str) -> str:
        for child in list(elem):
            if self._strip_xml_ns(child.tag) == name:
                return (child.text or "").strip()
        return ""

    def on_load_selected_tpp(self) -> None:
        if not self.tpp_list.curselection():
            messagebox.showwarning("Missing selection", "Select an approach first.")
            return
        idx = self.tpp_list.curselection()[0]
        chart = self.tpp_charts[idx]
        self.current_chart_name = chart.chart_name
        threading.Thread(target=self._download_and_load_tpp, args=(chart,), daemon=True).start()

    def _download_and_load_tpp(self, chart: TppChart) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            dest = CACHE_DIR / Path(chart.url).name
            if not dest.exists():
                self._log(f"Downloading {chart.chart_name}...")
                try:
                    self._http_download(chart.url, dest)
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status == 404:
                        if self._extract_tpp_pdf_from_local_zip(dest.name, dest):
                            self._log("FAA URL returned 404. Loaded PDF from local TPP zip cache.")
                        else:
                            self._ensure_all_tpp_bundles_cached()
                            if self._extract_tpp_pdf_from_local_zip(dest.name, dest):
                                self._log("FAA URL returned 404. Loaded PDF from downloaded TPP bundle cache.")
                            else:
                                raise
                    else:
                        raise
            self.pdf_path = dest
            self._render_pdf()
        except Exception as exc:
            self._log(f"Error: {exc}")
            messagebox.showerror("Download Failed", str(exc))

    def _extract_tpp_pdf_from_local_zip(self, pdf_name: str, dest: Path) -> bool:
        if not TPP_DIR.exists():
            return False
        target = pdf_name.lower()
        for zip_path in sorted(TPP_DIR.glob("DDTPP*.zip")):
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for member in zf.namelist():
                        if Path(member).name.lower() != target:
                            continue
                        with zf.open(member) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
                        return True
            except Exception:
                continue
        return False

    def _ensure_all_tpp_bundles_cached(self) -> None:
        index = self._load_tpp_index()
        urls: List[str] = []
        if index:
            urls = [u for u in index.get("bundle_urls", []) if isinstance(u, str)]
        if not urls:
            info = self._fetch_dtpp_info()
            urls = [u for u in info.get("urls", []) if isinstance(u, str)]
            if index:
                index["bundle_urls"] = urls
                self._save_tpp_index(index)
        if not urls:
            return
        TPP_DIR.mkdir(parents=True, exist_ok=True)
        for url in urls:
            name = Path(url).name
            if not name.upper().startswith("DDTPP") or not name.lower().endswith(".zip"):
                continue
            dest = TPP_DIR / name
            if dest.exists():
                continue
            self._log(f"Caching missing TPP bundle: {name}...")
            self._http_download(url, dest)

    def _find_nearby_fixes(self, lat: float, lon: float, radius_nm: float) -> List[FixPoint]:
        out: List[FixPoint] = []
        for f in self.fixes:
            if self._haversine_nm(lat, lon, f.lat, f.lon) <= radius_nm:
                out.append(f)
        out.sort(key=lambda f: self._haversine_nm(lat, lon, f.lat, f.lon))
        return out[:50]

    def on_load_pdf(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
        if not path:
            return
        self.pdf_path = Path(path)
        self.current_chart_name = self.pdf_path.stem
        self._render_pdf()

    def _render_pdf(self) -> None:
        if not self.pdf_path:
            return
        self._log(f"Rendering {self.pdf_path.name} at {DPI} DPI...")
        pdf = pdfium.PdfDocument(str(self.pdf_path))
        page = pdf[0]
        scale = DPI / 72.0
        bitmap = page.render(scale=scale)
        self.page_image = bitmap.to_pil()
        self.crop_box = self._auto_crop(self.page_image)
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._draw_image()
        self._reset_clicks()
    def _auto_crop(self, image: Image.Image) -> Tuple[int, int, int, int]:
        gray = image.convert("L")
        small = gray.resize((max(1, image.width // 4), max(1, image.height // 4)))
        pixels = small.load()
        w, h = small.size
        min_x, min_y, max_x, max_y = w, h, 0, 0
        for y in range(h):
            for x in range(w):
                if pixels[x, y] < 245:
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
        if min_x >= max_x or min_y >= max_y:
            return (0, 0, image.width, image.height)
        scale_x = image.width / w
        scale_y = image.height / h
        return (
            int(min_x * scale_x),
            int(min_y * scale_y),
            int((max_x + 1) * scale_x),
            int((max_y + 1) * scale_y),
        )

    def _draw_image(self) -> None:
        if not self.page_image:
            return
        canvas_w = self.canvas.winfo_width() or 800
        canvas_h = self.canvas.winfo_height() or 600
        scale = min(canvas_w / self.page_image.width, canvas_h / self.page_image.height)
        self.display_scale = max(0.02, min(scale * self.zoom, 24.0))
        display = self.page_image.resize(
            (
                int(self.page_image.width * self.display_scale),
                int(self.page_image.height * self.display_scale),
            )
        )
        self.tk_image = ImageTk.PhotoImage(display)
        self.canvas.delete("all")
        self.canvas.create_image(self.pan_x, self.pan_y, anchor="nw", image=self.tk_image)
        self._draw_crop_box()
        self._draw_clicks()

    def _draw_crop_box(self) -> None:
        if not self.crop_box or not self.page_image:
            return
        norm = self._normalized_crop_box(self.crop_box, self.page_image.width, self.page_image.height)
        if not norm:
            return
        x1, y1, x2, y2 = norm
        x1 = x1 * self.display_scale + self.pan_x
        y1 = y1 * self.display_scale + self.pan_y
        x2 = x2 * self.display_scale + self.pan_x
        y2 = y2 * self.display_scale + self.pan_y
        if self._crop_rect_id:
            self.canvas.delete(self._crop_rect_id)
        self._crop_rect_id = self.canvas.create_rectangle(
            x1, y1, x2, y2, outline="#00ff99", width=2
        )

    def _draw_clicks(self) -> None:
        for idx, pt in enumerate(self.click_points):
            x, y = pt
            x = x * self.display_scale + self.pan_x
            y = y * self.display_scale + self.pan_y
            color = "#ffcc00" if idx == 0 else "#00ccff"
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="black")
        for idx, (pt, _) in enumerate(self.refine_points):
            x, y = pt
            x = x * self.display_scale + self.pan_x
            y = y * self.display_scale + self.pan_y
            label = str(idx + 1)
            self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#ff66cc", outline="black")
            self.canvas.create_text(x + 9, y - 9, text=label, fill="#ff66cc", anchor="nw")

    def on_canvas_click(self, event: tk.Event) -> None:
        if not self.page_image:
            return
        self._dragging = True
        self._drag_start = self._screen_to_image(event.x, event.y)

    def on_canvas_drag(self, event: tk.Event) -> None:
        if not self._dragging or not self._drag_start:
            return
        x0, y0 = self._drag_start
        x1, y1 = self._screen_to_image(event.x, event.y)
        self.crop_box = (
            int(min(x0, x1)),
            int(min(y0, y1)),
            int(max(x0, x1)),
            int(max(y0, y1)),
        )
        self._draw_image()

    def on_canvas_release(self, event: tk.Event) -> None:
        self._dragging = False
        if not self.page_image or not self.crop_box:
            return
        x0, y0 = self._drag_start or (0, 0)
        x1, y1 = self._screen_to_image(event.x, event.y)
        if abs(x1 - x0) < 3 and abs(y1 - y0) < 3:
            self._add_control_point((x1, y1))

    def on_pan_start(self, event: tk.Event) -> None:
        self._pan_anchor = (event.x, event.y, self.pan_x, self.pan_y)

    def on_pan_drag(self, event: tk.Event) -> None:
        if not getattr(self, "_pan_anchor", None):
            return
        ax, ay, px, py = self._pan_anchor
        self.pan_x = px + (event.x - ax)
        self.pan_y = py + (event.y - ay)
        self._draw_image()

    def on_pan_release(self, _event: tk.Event) -> None:
        self._pan_anchor = None

    def on_zoom_in(self) -> None:
        self.zoom = min(self.zoom * 1.25, 20.0)
        self._draw_image()

    def on_zoom_out(self) -> None:
        self.zoom = max(self.zoom / 1.25, 0.05)
        self._draw_image()

    def on_zoom_fit(self) -> None:
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._draw_image()

    def on_mouse_wheel(self, event: tk.Event) -> None:
        if event.delta > 0:
            self.on_zoom_in()
        else:
            self.on_zoom_out()

    def on_canvas_motion(self, event: tk.Event) -> None:
        if not self.show_target_var.get() or self._pending_refine_fix is None:
            self._hide_target_cursor()
            return
        radius = max(2, int(self.target_radius_var.get()))
        x1, y1 = event.x - radius, event.y - radius
        x2, y2 = event.x + radius, event.y + radius
        if self._target_circle_id is None:
            self._target_circle_id = self.canvas.create_oval(
                x1, y1, x2, y2, outline="#ff66cc", width=2, dash=(2, 2)
            )
        else:
            self.canvas.coords(self._target_circle_id, x1, y1, x2, y2)

    def _hide_target_cursor(self) -> None:
        if self._target_circle_id is not None:
            self.canvas.delete(self._target_circle_id)
            self._target_circle_id = None

    def _screen_to_image(self, x: float, y: float) -> Tuple[float, float]:
        return ((x - self.pan_x) / self.display_scale, (y - self.pan_y) / self.display_scale)

    def _add_control_point(self, pt: Tuple[float, float]) -> None:
        if len(self.click_points) < 2:
            self.click_points.append(pt)
            if len(self.click_points) == 1:
                if self.selected_runway:
                    self.instruction_var.set(
                        f"Click runway end {self.selected_runway.end2.ident} on the chart."
                    )
                else:
                    self.instruction_var.set("Click runway end B on the chart.")
            else:
                self.instruction_var.set("Ready. Optional: pick a fix and click Refine.")
            self._draw_image()

    def _reset_clicks(self) -> None:
        self.click_points = []
        self.refine_points = []
        self._pending_refine_fix = None
        if self.selected_runway:
            self.instruction_var.set(
                f"Click runway end {self.selected_runway.end1.ident} on the chart."
            )
        else:
            self.instruction_var.set("Click runway end A on the chart.")
        self._draw_image()

    def on_refine(self) -> None:
        if not self.page_image or not self.selected_runway:
            return
        if len(self.click_points) < 2:
            messagebox.showwarning("Missing clicks", "Click both runway ends first.")
            return
        fix = self._selected_fix()
        if fix is None:
            messagebox.showwarning("Missing fix", "Select a fix/VOR to refine.")
            return
        self._pending_refine_fix = fix
        messagebox.showinfo("Refine", f"Click {fix.ident} on the chart.")
        self._hide_target_cursor()
        if self._refine_bind_id:
            self.canvas.unbind("<Button-1>", self._refine_bind_id)
            self._refine_bind_id = None
        self._refine_bind_id = self.canvas.bind("<Button-1>", self._capture_refine_click, add="+")

    def _capture_refine_click(self, event: tk.Event) -> None:
        try:
            if self._pending_refine_fix is None:
                return
            self.refine_points.append((self._screen_to_image(event.x, event.y), self._pending_refine_fix))
            self.instruction_var.set(
                f"Refine points: {len(self.refine_points)}. Add more fixes or Generate KMZ."
            )
            self._draw_image()
        finally:
            self._pending_refine_fix = None
            self._hide_target_cursor()
            if self._refine_bind_id:
                self.canvas.unbind("<Button-1>", self._refine_bind_id)
                self._refine_bind_id = None

    def on_generate(self) -> None:
        if not self.selected_airport or not self.selected_runway:
            messagebox.showwarning("Missing data", "Select an airport and runway.")
            return
        if not self.page_image or not self.crop_box:
            messagebox.showwarning("Missing PDF", "Load a PDF first.")
            return
        if len(self.click_points) < 2:
            messagebox.showwarning("Missing clicks", "Click both runway ends.")
            return

        output_dir = Path(self.output_var.get())
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            kmz_path, err_m = self._generate_kmz(output_dir)
            self._log(f"Saved KMZ: {kmz_path}")
            if err_m is not None:
                self._log(f"Estimated error: {err_m:.1f} m")
            try:
                os.startfile(str(output_dir))  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception as exc:
            self._log(f"Error: {exc}")
            messagebox.showerror("Generate Failed", str(exc))

    def _generate_kmz(self, output_dir: Path) -> Tuple[Path, Optional[float]]:
        norm_crop = self._normalized_crop_box(self.crop_box, self.page_image.width, self.page_image.height)
        if not norm_crop:
            raise RuntimeError("Invalid crop area. Drag a valid crop box on the chart.")
        crop_img = self.page_image.crop(norm_crop)
        width, height = crop_img.size

        p1 = self._to_crop_coords(self.click_points[0])
        p2 = self._to_crop_coords(self.click_points[1])

        rwy = self.selected_runway
        end_a, end_b = rwy.end1, rwy.end2

        enu = ENUTransform(rwy.end1.lat, rwy.end1.lon)
        q1 = enu.geodetic_to_enu(end_a.lat, end_a.lon)
        q2 = enu.geodetic_to_enu(end_b.lat, end_b.lon)

        points_px = [p1, p2]
        points_enu = [q1, q2]
        for px_pt, fix in self.refine_points:
            points_px.append(self._to_crop_coords(px_pt))
            points_enu.append(enu.geodetic_to_enu(fix.lat, fix.lon))

        transform = SimilarityTransform.from_two_points(p1, p2, q1, q2)
        err_m = None
        if len(points_px) >= 3:
            # Keep runway axis dominant; refine points improve local fit without introducing
            # noticeable rotational drift from small waypoint click errors.
            weights = [8.0, 8.0] + [1.0] * (len(points_px) - 2)
            transform, err_m = SimilarityTransform.refine(points_px, points_enu, weights=weights)

        if rwy.length_ft:
            implied_len = transform.distance(p1, p2)
            expected_m = rwy.length_ft * 0.3048
            rel_err = abs(implied_len - expected_m) / expected_m
            hard_limit = 0.35 if len(self.refine_points) >= 1 else 0.20
            if rel_err > hard_limit:
                raise RuntimeError("Runway length mismatch is too large. Re-click runway thresholds.")
            if rel_err > 0.10:
                self._log(
                    f"Warning: runway length mismatch {rel_err * 100:.1f}% "
                    "(continuing because tolerance is relaxed)."
                )

        corners_px = [(0, 0), (width, 0), (width, height), (0, height)]
        corners_ll = [enu.enu_to_geodetic(*transform.apply(p)) for p in corners_px]

        airport_name = self._sanitize_filename_part(self.selected_airport.ident)
        proc_name = self._sanitize_filename_part(self._display_procedure_name(self._current_procedure_name()))
        name = f"{airport_name} {proc_name}.kmz"
        out_path = output_dir / name

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            img_path = tmp_dir / "overlay.png"
            crop_img.save(img_path)
            kml = self._build_kml(corners_ll)
            kmz_path = self._write_kmz(out_path, kml, img_path)

        return kmz_path, err_m

    def _build_kml(self, corners_ll: List[Tuple[float, float]]) -> str:
        # Google Earth expects gx:LatLonQuad corners in this order:
        # lower-left, lower-right, upper-right, upper-left.
        if len(corners_ll) == 4:
            ordered = [corners_ll[3], corners_ll[2], corners_ll[1], corners_ll[0]]
        else:
            ordered = corners_ll
        coords = " ".join([f"{lon},{lat},0" for lat, lon in ordered])
        point_lat = self.selected_airport.lat
        point_lon = self.selected_airport.lon
        meta = (
            f"Airport: {self.selected_airport.ident}\\n"
            f"Runway: {self.selected_runway.ident}\\n"
            f"DPI: {DPI}"
        )
        return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<kml xmlns=\"http://www.opengis.net/kml/2.2\" xmlns:gx=\"http://www.google.com/kml/ext/2.2\">
  <Document>
    <name>Plate Overlay</name>
    <Placemark>
      <name>Georef Metadata</name>
      <description>{meta}</description>
      <Point>
        <altitudeMode>clampToGround</altitudeMode>
        <coordinates>{point_lon},{point_lat},0</coordinates>
      </Point>
    </Placemark>
    <GroundOverlay>
      <name>Overlay</name>
      <altitude>0</altitude>
      <altitudeMode>clampToGround</altitudeMode>
      <Icon>
        <href>overlay.png</href>
      </Icon>
      <gx:LatLonQuad>
        <coordinates>{coords}</coordinates>
      </gx:LatLonQuad>
    </GroundOverlay>
  </Document>
</kml>"""

    def _write_kmz(self, out_path: Path, kml: str, img_path: Path) -> Path:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doc.kml", kml)
            zf.write(img_path, "overlay.png")
        return out_path

    def _to_crop_coords(self, pt: Tuple[float, float]) -> Tuple[float, float]:
        x, y = pt
        if not self.page_image:
            return (x, y)
        norm = self._normalized_crop_box(self.crop_box, self.page_image.width, self.page_image.height)
        if not norm:
            return (x, y)
        cx1, cy1, _, _ = norm
        return (x - cx1, y - cy1)

    def _normalized_crop_box(
        self, crop_box: Optional[Tuple[int, int, int, int]], width: int, height: int
    ) -> Optional[Tuple[int, int, int, int]]:
        if not crop_box:
            return None
        x1, y1, x2, y2 = crop_box
        x1 = max(0, min(width, int(x1)))
        x2 = max(0, min(width, int(x2)))
        y1 = max(0, min(height, int(y1)))
        y2 = max(0, min(height, int(y2)))
        if x2 <= x1 or y2 <= y1:
            return None
        return (x1, y1, x2, y2)

    def _selected_fix(self) -> Optional[FixPoint]:
        label = self.fix_var.get()
        if not label or label.strip().lower() == "none":
            return None
        ident = label.split(" ")[0].strip().upper()
        if not ident:
            return None
        for f in self.nearby_fixes:
            if f.ident.upper() == ident:
                return f
        for f in self.nearby_fixes:
            if f.ident.upper().startswith(ident):
                return f
        return None

    def _haversine_nm(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        r = 6371000.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return (r * c) / 1852.0

    def _sanitize_filename_part(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\\\|?*]+', " ", value.strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned or "unnamed"

    def _display_procedure_name(self, name: str) -> str:
        # Keep user-facing chart naming while removing APRA category prefixes.
        return re.sub(r"^(IAP|ODP|DP|STAR)\s*-\s*", "", name.strip(), flags=re.IGNORECASE)

    def _refresh_status(self) -> None:
        if not hasattr(self, "status_var"):
            return
        nasr = self._status_nasr_cycle()
        tpp = self._status_tpp_cycle()
        apra = "configured" if self._apra_headers() is not None else "not configured"
        self.status_var.set(f"NASR: {nasr} | TPP: {tpp} | APRA: {apra}")

    def _status_nasr_cycle(self) -> str:
        if self.nasr_date_var.get().strip():
            return self.nasr_date_var.get().strip()
        if not NASR_DIR.exists():
            return "none"
        dates: List[str] = []
        for p in NASR_DIR.glob("NASR_*.pkl"):
            m = re.search(r"NASR_(\d{4}-\d{2}-\d{2})\.pkl$", p.name)
            if m:
                dates.append(m.group(1))
        return max(dates) if dates else "none"

    def _status_tpp_cycle(self) -> str:
        if not TPP_INDEX_FILE.exists():
            return "none"
        try:
            data = json.loads(TPP_INDEX_FILE.read_text(encoding="utf-8"))
            return str(data.get("edition_date") or "cached")
        except Exception:
            return "cached"

    def _current_procedure_name(self) -> str:
        if self.current_chart_name:
            return self.current_chart_name
        if self.tpp_list.curselection():
            idx = self.tpp_list.curselection()[0]
            if 0 <= idx < len(self.tpp_charts):
                return self.tpp_charts[idx].chart_name
        if self.selected_runway:
            return self.selected_runway.ident
        return "procedure"

    def _geo_product_changed(self) -> None:
        product = self.geo_product_var.get()
        if product == "IFR Enroute":
            self.geo_geoname_combo["values"] = ["US", "Alaska", "Pacific", "Caribbean"]
            self.geo_geoname_var.set("US")
            self.geo_series_combo.configure(state="readonly")
            self.geo_series_var.set("low")
        elif product == "IFR Planning":
            self.geo_geoname_combo["values"] = ["US", "NA", "PO", "WAT"]
            self.geo_geoname_var.set("US")
            self.geo_series_combo.configure(state="disabled")
            self.geo_series_var.set("low")
        elif product == "VFR Sectional":
            self.geo_geoname_combo["values"] = [
                "Denver", "Seattle", "Los Angeles", "Dallas-Ft Worth", "Phoenix", "Chicago", "New York"
            ]
            self.geo_geoname_var.set("Denver")
            self.geo_series_combo.configure(state="disabled")
            self.geo_series_var.set("low")
        else:  # VFR TAC
            self.geo_geoname_combo["values"] = [
                "Denver-Colorado Springs", "Los Angeles", "Seattle", "Dallas-Ft Worth", "New York", "Chicago"
            ]
            self.geo_geoname_var.set("Denver-Colorado Springs")
            self.geo_series_combo.configure(state="disabled")
            self.geo_series_var.set("low")

    def on_geo_fetch(self) -> None:
        threading.Thread(target=self._geo_fetch_worker, daemon=True).start()

    def _geo_fetch_worker(self) -> None:
        try:
            resp = self._apra_get_geotiff_release()
            if resp is None:
                raise RuntimeError("APRA credentials not configured.")
            items = self._parse_geo_release(resp.text)
            self.geo_results = items
            self.geo_list.delete(0, "end")
            for item in items:
                self.geo_list.insert("end", item.get("label", ""))
            self._log(f"Loaded {len(items)} GeoTIFF link(s).")
        except Exception as exc:
            self._log(f"Error: {exc}")
            messagebox.showerror("GeoTIFF Fetch Failed", str(exc))

    def _apra_get_geotiff_release(self) -> Optional[requests.Response]:
        product = self.geo_product_var.get()
        edition = self.geo_edition_var.get().strip() or "current"
        geoname = self.geo_geoname_var.get().strip()
        if product == "IFR Enroute":
            params = {"edition": edition, "format": "tiff", "geoname": geoname, "seriesType": self.geo_series_var.get()}
            return self._apra_get("/enroute/chart", params=params)
        if product == "IFR Planning":
            params = {"edition": edition, "format": "tiff", "geoname": geoname}
            return self._apra_get("/ifr/planning/chart", params=params)
        if product == "VFR Sectional":
            params = {"edition": edition, "format": "tiff", "geoname": geoname}
            return self._apra_get("/vfr/sectional/chart", params=params)
        params = {"edition": edition, "format": "tiff", "geoname": geoname}
        return self._apra_get("/vfr/tac/chart", params=params)

    def _parse_geo_release(self, text: str) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        # XML APRA format: product url attr.
        try:
            root = ET.fromstring(text)
            idx = 1
            for elem in root.iter():
                if self._strip_xml_ns(elem.tag) != "product":
                    continue
                url = (elem.attrib.get("url") or "").strip()
                if not url:
                    continue
                name = (
                    elem.attrib.get("chartName")
                    or elem.attrib.get("productName")
                    or Path(url).name
                )
                out.append({"url": url, "label": f"{idx}. {name}", "name": str(name)})
                idx += 1
        except Exception:
            pass
        if out:
            return out
        # Fallback regex parse.
        urls = re.findall(r'url="([^"]+)"', text)
        for idx, url in enumerate(urls, start=1):
            out.append({"url": url, "label": f"{idx}. {Path(url).name}", "name": Path(url).name})
        return out

    def on_geo_download_selected(self) -> None:
        threading.Thread(target=self._geo_download_worker, args=(False,), daemon=True).start()

    def on_geo_download_export_kmz(self) -> None:
        threading.Thread(target=self._geo_download_worker, args=(True,), daemon=True).start()

    def _geo_download_worker(self, export_kmz: bool) -> None:
        try:
            if not self.geo_list.curselection():
                raise RuntimeError("Select a GeoTIFF item first.")
            idx = self.geo_list.curselection()[0]
            item = self.geo_results[idx]
            url = item.get("url", "")
            if not url:
                raise RuntimeError("Selected item has no URL.")
            CHARTS_DIR.mkdir(parents=True, exist_ok=True)
            dest = CHARTS_DIR / Path(url).name
            if not dest.exists():
                self._log(f"Downloading {dest.name}...")
                self._http_download(url, dest)
            self._log(f"Saved: {dest}")
            if export_kmz:
                kmz = self._export_geotiff_to_kmz(dest)
                self._log(f"Exported KMZ: {kmz}")
                try:
                    os.startfile(str(kmz.parent))  # type: ignore[attr-defined]
                except Exception:
                    pass
        except Exception as exc:
            self._log(f"Error: {exc}")
            messagebox.showerror("GeoTIFF Download Failed", str(exc))

    def _export_geotiff_to_kmz(self, source: Path) -> Path:
        tif = source
        if tif.suffix.lower() == ".zip":
            tif = self._extract_first_tif_from_zip(tif)
        if tif.suffix.lower() not in (".tif", ".tiff"):
            raise RuntimeError("Downloaded file is not a GeoTIFF.")
        out = tif.with_suffix(".kmz")
        gdal = self._find_gdal_translate()
        if not gdal:
            gdal = self._prompt_for_gdal_translate()
        if not gdal:
            raise RuntimeError("gdal_translate not found. Install GDAL or select gdal_translate.exe.")
        cmd = [gdal, "-of", "KMLSUPEROVERLAY", str(tif), str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(f"GDAL export failed: {stderr[:300]}")
        return out

    def _find_gdal_translate(self) -> Optional[str]:
        # 1) explicit env override
        env_path = os.environ.get("GDAL_TRANSLATE", "").strip()
        if env_path and Path(env_path).exists():
            return env_path

        # 2) regular PATH lookup
        path_hit = shutil.which("gdal_translate")
        if path_hit:
            return path_hit
        path_hit_exe = shutil.which("gdal_translate.exe")
        if path_hit_exe:
            return path_hit_exe

        # 3) common Windows install locations
        candidates = [
            Path(r"C:\Program Files\GDAL\gdal_translate.exe"),
            Path(r"C:\Program Files\GDAL\bin\gdal_translate.exe"),
            Path(r"C:\OSGeo4W\bin\gdal_translate.exe"),
            Path(r"C:\OSGeo4W64\bin\gdal_translate.exe"),
        ]
        for p in candidates:
            if p.exists():
                return str(p)

        root = Path(r"C:\Program Files\GDAL")
        if root.exists():
            hits = list(root.rglob("gdal_translate.exe"))
            if hits:
                return str(hits[0])
        return None

    def _prompt_for_gdal_translate(self) -> Optional[str]:
        msg = (
            "Could not auto-find gdal_translate.exe.\n\n"
            "Select gdal_translate.exe to continue GeoTIFF -> KMZ export."
        )
        if not messagebox.askyesno("Locate GDAL", msg):
            return None
        path = filedialog.askopenfilename(
            title="Select gdal_translate.exe",
            filetypes=[("GDAL Translate", "gdal_translate.exe"), ("Executable", "*.exe")],
        )
        if not path:
            return None
        if Path(path).name.lower() != "gdal_translate.exe":
            messagebox.showwarning("Invalid File", "Please select gdal_translate.exe.")
            return None
        os.environ["GDAL_TRANSLATE"] = path
        return path

    def _extract_first_tif_from_zip(self, zip_path: Path) -> Path:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith((".tif", ".tiff")):
                    out = CHARTS_DIR / Path(name).name
                    if not out.exists():
                        with zf.open(name) as src, open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    return out
        raise RuntimeError("No GeoTIFF found in ZIP.")

    def _http_download(self, url: str, dest: Path, referer: Optional[str] = None) -> None:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        resp = requests.get(url, stream=True, timeout=120, headers=headers)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

class ENUTransform:
    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0 = math.radians(lat0)
        self.lon0 = math.radians(lon0)
        self.to_ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
        self.to_geo = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
        self.x0, self.y0, self.z0 = self.to_ecef.transform(lon0, lat0, 0.0)

    def geodetic_to_enu(self, lat: float, lon: float) -> Tuple[float, float]:
        x, y, z = self.to_ecef.transform(lon, lat, 0.0)
        dx, dy, dz = x - self.x0, y - self.y0, z - self.z0
        sin_lat = math.sin(self.lat0)
        cos_lat = math.cos(self.lat0)
        sin_lon = math.sin(self.lon0)
        cos_lon = math.cos(self.lon0)
        e = -sin_lon * dx + cos_lon * dy
        n = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
        return (e, n)

    def enu_to_geodetic(self, e: float, n: float) -> Tuple[float, float]:
        sin_lat = math.sin(self.lat0)
        cos_lat = math.cos(self.lat0)
        sin_lon = math.sin(self.lon0)
        cos_lon = math.cos(self.lon0)
        dx = -sin_lon * e - sin_lat * cos_lon * n
        dy = cos_lon * e - sin_lat * sin_lon * n
        dz = cos_lat * n
        x = self.x0 + dx
        y = self.y0 + dy
        z = self.z0 + dz
        lon, lat, _ = self.to_geo.transform(x, y, z)
        return (lat, lon)


class SimilarityTransform:
    def __init__(self, scale: float, rotation: float, t: Tuple[float, float], p0: Tuple[float, float]):
        self.scale = scale
        self.rotation = rotation
        self.t = t
        self.p0 = p0

    @staticmethod
    def from_two_points(
        p1: Tuple[float, float], p2: Tuple[float, float], q1: Tuple[float, float], q2: Tuple[float, float]
    ):
        v = (p2[0] - p1[0], -(p2[1] - p1[1]))
        w = (q2[0] - q1[0], q2[1] - q1[1])
        lv = math.hypot(v[0], v[1])
        lw = math.hypot(w[0], w[1])
        if lv == 0:
            raise RuntimeError("Runway click points are identical.")
        scale = lw / lv
        ang_v = math.atan2(v[1], v[0])
        ang_w = math.atan2(w[1], w[0])
        rotation = ang_w - ang_v
        return SimilarityTransform(scale, rotation, q1, p1)

    @staticmethod
    def refine(
        points_px: List[Tuple[float, float]],
        points_enu: List[Tuple[float, float]],
        weights: Optional[List[float]] = None,
    ):
        P = np.array([[p[0], -p[1]] for p in points_px], dtype=float)
        Q = np.array(points_enu, dtype=float)
        if weights is None:
            w = np.ones(len(points_px), dtype=float)
        else:
            w = np.array(weights, dtype=float)
            if w.shape[0] != len(points_px):
                raise RuntimeError("Refine weights length mismatch.")
            w = np.clip(w, 1e-6, None)
        w_sum = np.sum(w)
        if w_sum <= 0:
            raise RuntimeError("Invalid refine weights.")
        wn = w / w_sum
        Pc = np.sum(P * wn[:, None], axis=0)
        Qc = np.sum(Q * wn[:, None], axis=0)
        P0 = P - Pc
        Q0 = Q - Qc
        H = P0.T @ (Q0 * wn[:, None])
        U, S, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T
        if np.linalg.det(R) < 0:
            Vt[1, :] *= -1
            R = Vt.T @ U.T
        denom = np.sum(wn * np.sum(P0 * P0, axis=1))
        if denom <= 0:
            raise RuntimeError("Degenerate refine geometry.")
        scale = np.trace(R.T @ H) / denom
        t = Qc - scale * (R @ Pc)

        Qp = (scale * (R @ P.T)).T + t
        err = np.sqrt(np.sum(wn * np.sum((Qp - Q) ** 2, axis=1)))

        def apply_fn(p: Tuple[float, float]) -> Tuple[float, float]:
            v = np.array([p[0], -p[1]])
            q = scale * (R @ v) + t
            return (float(q[0]), float(q[1]))

        st = SimilarityTransform(scale, math.atan2(R[1, 0], R[0, 0]), (float(t[0]), float(t[1])), (0, 0))
        st.apply = apply_fn  # type: ignore
        st.distance = lambda a, b: math.hypot(*(np.array(st.apply(a)) - np.array(st.apply(b))))  # type: ignore
        return st, err

    def apply(self, p: Tuple[float, float]) -> Tuple[float, float]:
        x, y = p
        y = -y
        cos_r = math.cos(self.rotation)
        sin_r = math.sin(self.rotation)
        rx = cos_r * (x - self.p0[0]) - sin_r * (y - (-self.p0[1]))
        ry = sin_r * (x - self.p0[0]) + cos_r * (y - (-self.p0[1]))
        return (self.t[0] + self.scale * rx, self.t[1] + self.scale * ry)

    def distance(self, a: Tuple[float, float], b: Tuple[float, float]) -> float:
        qa = self.apply(a)
        qb = self.apply(b)
        return math.hypot(qb[0] - qa[0], qb[1] - qa[1])


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    NASR_DIR.mkdir(parents=True, exist_ok=True)
    App().mainloop()


if __name__ == "__main__":
    main()
