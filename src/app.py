
import csv
import io
import json
import math
import os
import re
import shutil
import tempfile
import time
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime
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
APRA_BASE = os.environ.get("APRA_BASE_URL", "https://external-api.faa.gov/apra")
DATA_DIR = Path("data")
NASR_DIR = DATA_DIR / "nasr"
CACHE_DIR = DATA_DIR / "cache"
LAST_NASR_DATE_FILE = DATA_DIR / "last_nasr_date.txt"

DPI = 400
DEFAULT_OUTPUT_DIR = Path("output")


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
    runways: List[Runway]


@dataclass
class FixPoint:
    ident: str
    lat: float
    lon: float
    kind: str


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("FAA Plate Georef (Runway-Click)")
        self.geometry("1200x820")

        self.airports: Dict[str, Airport] = {}
        self.filtered_airports: List[Airport] = []
        self.selected_airport: Optional[Airport] = None
        self.selected_runway: Optional[Runway] = None
        self.fixes: List[FixPoint] = []
        self.nearby_fixes: List[FixPoint] = []

        self.pdf_path: Optional[Path] = None
        self.page_image: Optional[Image.Image] = None
        self.crop_box: Optional[Tuple[int, int, int, int]] = None
        self.display_scale: float = 1.0
        self.zoom: float = 1.0
        self.tk_image: Optional[ImageTk.PhotoImage] = None

        self.click_points: List[Tuple[float, float]] = []
        self.refine_point: Optional[Tuple[float, float]] = None
        self._apra_token: Optional[str] = None
        self._apra_token_expiry: float = 0.0

        self._build_ui()
        self._load_last_nasr_date()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=8)
        top.columnconfigure(7, weight=1)

        ttk.Button(top, text="Update NASR", command=self.on_update_nasr).grid(
            row=0, column=0, padx=(0, 10)
        )
        ttk.Label(top, text="NASR date:").grid(row=0, column=1, sticky="w")
        self.nasr_date_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.nasr_date_var, width=12).grid(
            row=0, column=2, padx=(4, 12)
        )

        ttk.Button(top, text="Load PDF", command=self.on_load_pdf).grid(
            row=0, column=3, padx=(0, 10)
        )

        ttk.Label(top, text="Search Airport:").grid(row=0, column=4, sticky="w")
        self.search_var = tk.StringVar(value="")
        search_entry = ttk.Entry(top, textvariable=self.search_var)
        search_entry.grid(row=0, column=5, sticky="ew", padx=(4, 10))
        search_entry.bind("<KeyRelease>", lambda _e: self.on_search())

        ttk.Label(top, text="Runway:").grid(row=0, column=6, sticky="w")
        self.runway_var = tk.StringVar(value="")
        self.runway_combo = ttk.Combobox(
            top, textvariable=self.runway_var, state="readonly", width=12
        )
        self.runway_combo.grid(row=0, column=7, sticky="w")
        self.runway_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_runway_select())

        mid = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        mid.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))

        left = ttk.Frame(mid)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        mid.add(left, weight=1)

        ttk.Label(left, text="Airports").grid(row=0, column=0, sticky="w")
        self.airport_list = tk.Listbox(left, height=18)
        self.airport_list.grid(row=1, column=0, sticky="nsew")
        self.airport_list.bind("<<ListboxSelect>>", lambda _e: self.on_airport_select())

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
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Configure>", lambda _e: self._draw_image())

        controls = ttk.Frame(self)
        controls.grid(row=2, column=0, sticky="ew", padx=10)
        controls.columnconfigure(6, weight=1)

        self.instruction_var = tk.StringVar(value="Load a PDF to start.")
        ttk.Label(controls, textvariable=self.instruction_var).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )

        ttk.Label(controls, text="Refine fix:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.fix_var = tk.StringVar(value="None")
        self.fix_combo = ttk.Combobox(
            controls, textvariable=self.fix_var, state="readonly", width=24
        )
        self.fix_combo.grid(row=1, column=1, sticky="w", pady=(6, 0))
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

        log_frame = ttk.Frame(self)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=(6, 10))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=10, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self._dragging = False
        self._drag_start = None
        self._crop_rect_id = None

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _load_last_nasr_date(self) -> None:
        if LAST_NASR_DATE_FILE.exists():
            self.nasr_date_var.set(LAST_NASR_DATE_FILE.read_text(encoding="utf-8").strip())

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

            self._log("Parsing NASR CSVs...")
            self.airports, self.fixes = self._parse_nasr(zip_path)
            self._log(f"Loaded {len(self.airports)} airports, {len(self.fixes)} fixes/navs.")
            self.on_search()
        except Exception as exc:
            if "not a zip" in str(exc).lower():
                zip_path = self._prompt_for_nasr_zip()
                if zip_path:
                    try:
                        self._log("Parsing NASR CSVs...")
                        self.airports, self.fixes = self._parse_nasr(zip_path)
                        self._log(
                            f"Loaded {len(self.airports)} airports, {len(self.fixes)} fixes/navs."
                        )
                        self.on_search()
                        return
                    except Exception as exc2:
                        self._log(f"Error: {exc2}")
                        messagebox.showerror("NASR Update Failed", str(exc2))
                        return
            self._log(f"Error: {exc}")
            messagebox.showerror("NASR Update Failed", str(exc))

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
        url = f"{APRA_BASE}{path}"
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp

    def _apra_headers(self) -> Optional[Dict[str, str]]:
        api_key = os.environ.get("APRA_API_KEY")
        api_key_header = os.environ.get("APRA_API_KEY_HEADER", "x-api-key")
        bearer = os.environ.get("APRA_BEARER_TOKEN")
        token_url = os.environ.get("APRA_TOKEN_URL")
        client_id = os.environ.get("APRA_CLIENT_ID")
        client_secret = os.environ.get("APRA_CLIENT_SECRET")
        scope = os.environ.get("APRA_SCOPE")

        if api_key:
            return {api_key_header: api_key}
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
            airports[apt_id] = Airport(ident=apt_id, name=name_val, lat=lat, lon=lon, runways=[])
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
        scale = min(canvas_w / self.page_image.width, canvas_h / self.page_image.height, 1.0)
        self.display_scale = max(0.1, min(scale * self.zoom, 6.0))
        display = self.page_image.resize(
            (
                int(self.page_image.width * self.display_scale),
                int(self.page_image.height * self.display_scale),
            )
        )
        self.tk_image = ImageTk.PhotoImage(display)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image)
        self._draw_crop_box()
        self._draw_clicks()

    def _draw_crop_box(self) -> None:
        if not self.crop_box:
            return
        x1, y1, x2, y2 = self.crop_box
        x1 *= self.display_scale
        y1 *= self.display_scale
        x2 *= self.display_scale
        y2 *= self.display_scale
        if self._crop_rect_id:
            self.canvas.delete(self._crop_rect_id)
        self._crop_rect_id = self.canvas.create_rectangle(
            x1, y1, x2, y2, outline="#00ff99", width=2
        )

    def _draw_clicks(self) -> None:
        for idx, pt in enumerate(self.click_points):
            x, y = pt
            x *= self.display_scale
            y *= self.display_scale
            color = "#ffcc00" if idx == 0 else "#00ccff"
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="black")
        if self.refine_point:
            x, y = self.refine_point
            x *= self.display_scale
            y *= self.display_scale
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#ff66cc", outline="black")

    def on_canvas_click(self, event: tk.Event) -> None:
        if not self.page_image:
            return
        self._dragging = True
        self._drag_start = (event.x / self.display_scale, event.y / self.display_scale)

    def on_canvas_drag(self, event: tk.Event) -> None:
        if not self._dragging or not self._drag_start:
            return
        x0, y0 = self._drag_start
        x1, y1 = event.x / self.display_scale, event.y / self.display_scale
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
        x1, y1 = event.x / self.display_scale, event.y / self.display_scale
        if abs(x1 - x0) < 3 and abs(y1 - y0) < 3:
            self._add_control_point((x1, y1))

    def on_zoom_in(self) -> None:
        self.zoom = min(self.zoom * 1.25, 6.0)
        self._draw_image()

    def on_zoom_out(self) -> None:
        self.zoom = max(self.zoom / 1.25, 0.2)
        self._draw_image()

    def on_zoom_fit(self) -> None:
        self.zoom = 1.0
        self._draw_image()

    def on_mouse_wheel(self, event: tk.Event) -> None:
        if event.delta > 0:
            self.on_zoom_in()
        else:
            self.on_zoom_out()

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
        self.refine_point = None
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
        if self.fix_var.get() == "None":
            messagebox.showwarning("Missing fix", "Select a fix/VOR to refine.")
            return
        messagebox.showinfo("Refine", "Click the selected fix on the chart.")
        self.refine_point = None
        self.canvas.bind("<Button-1>", self._capture_refine_click, add="+")

    def _capture_refine_click(self, event: tk.Event) -> None:
        self.refine_point = (event.x / self.display_scale, event.y / self.display_scale)
        self.canvas.unbind("<Button-1>", self._capture_refine_click)
        self._draw_image()

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
        except Exception as exc:
            self._log(f"Error: {exc}")
            messagebox.showerror("Generate Failed", str(exc))

    def _generate_kmz(self, output_dir: Path) -> Tuple[Path, Optional[float]]:
        crop_img = self.page_image.crop(self.crop_box)
        width, height = crop_img.size

        p1 = self._to_crop_coords(self.click_points[0])
        p2 = self._to_crop_coords(self.click_points[1])

        rwy = self.selected_runway
        end_a, end_b = rwy.end1, rwy.end2

        enu = ENUTransform(rwy.end1.lat, rwy.end1.lon)
        q1 = enu.geodetic_to_enu(end_a.lat, end_a.lon)
        q2 = enu.geodetic_to_enu(end_b.lat, end_b.lon)

        transform = SimilarityTransform.from_two_points(p1, p2, q1, q2)

        err_m = None
        if self.refine_point and self.fix_var.get() != "None":
            fix = self._selected_fix()
            if fix:
                p3 = self._to_crop_coords(self.refine_point)
                q3 = enu.geodetic_to_enu(fix.lat, fix.lon)
                transform, err_m = SimilarityTransform.refine([p1, p2, p3], [q1, q2, q3])

        if rwy.length_ft:
            implied_len = transform.distance(p1, p2)
            expected_m = rwy.length_ft * 0.3048
            if abs(implied_len - expected_m) / expected_m > 0.05:
                raise RuntimeError("Clicks don't match runway length; re-click thresholds.")

        corners_px = [(0, 0), (width, 0), (width, height), (0, height)]
        corners_ll = [enu.enu_to_geodetic(*transform.apply(p)) for p in corners_px]

        name = f"{self.selected_airport.ident}_{self.selected_runway.ident}.kmz"
        out_path = output_dir / name

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            img_path = tmp_dir / "overlay.png"
            crop_img.save(img_path)
            kml = self._build_kml(corners_ll)
            kmz_path = self._write_kmz(out_path, kml, img_path)

        return kmz_path, err_m

    def _build_kml(self, corners_ll: List[Tuple[float, float]]) -> str:
        coords = " ".join([f"{lon},{lat},0" for lat, lon in corners_ll])
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
      <Point><coordinates>0,0,0</coordinates></Point>
    </Placemark>
    <GroundOverlay>
      <name>Overlay</name>
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
        cx1, cy1, _, _ = self.crop_box
        return (x - cx1, y - cy1)

    def _selected_fix(self) -> Optional[FixPoint]:
        label = self.fix_var.get()
        if label == "None":
            return None
        ident = label.split(" ")[0]
        for f in self.nearby_fixes:
            if f.ident == ident:
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
    def refine(points_px: List[Tuple[float, float]], points_enu: List[Tuple[float, float]]):
        P = np.array([[p[0], -p[1]] for p in points_px], dtype=float)
        Q = np.array(points_enu, dtype=float)
        Pc = P.mean(axis=0)
        Qc = Q.mean(axis=0)
        P0 = P - Pc
        Q0 = Q - Qc
        H = P0.T @ Q0
        U, S, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T
        if np.linalg.det(R) < 0:
            Vt[1, :] *= -1
            R = Vt.T @ U.T
        scale = np.trace(R.T @ H) / np.trace(P0.T @ P0)
        t = Qc - scale * (R @ Pc)

        Qp = (scale * (R @ P.T)).T + t
        err = np.sqrt(np.mean(np.sum((Qp - Q) ** 2, axis=1)))

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
