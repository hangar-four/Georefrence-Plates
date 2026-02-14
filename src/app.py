import json
import re
import shutil
import subprocess
import threading
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


FAA_BASE = "https://aeronav.faa.gov/d-tpp"
DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "cache"
CYCLES_DIR = DATA_DIR / "cycles"
LAST_CYCLE_FILE = DATA_DIR / "last_cycle.txt"

APPROACH_TYPES = [
    "Any",
    "ILS",
    "LOC",
    "RNAV/GPS",
    "VOR",
    "NDB",
    "TACAN",
    "LDA",
    "SDF",
    "GLS",
    "RNP",
    "LPV",
    "LNAV",
    "Other",
]


@dataclass
class PlateRecord:
    chart_code: str
    chart_name: str
    pdf_name: str
    volume: str


@dataclass
class Airport:
    name: str
    city: str
    state: str
    apt_ident: str
    icao_ident: str
    volume: str
    plates: List[PlateRecord]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("FAA Plate KMZ Builder")
        self.geometry("980x720")

        self.airports: List[Airport] = []
        self.filtered_airports: List[Airport] = []
        self.selected_airport: Optional[Airport] = None
        self.plate_pool: List[PlateRecord] = []
        self.plate_index: List[PlateRecord] = []

        self._build_ui()
        self._load_last_cycle()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=8)
        top.columnconfigure(4, weight=1)

        ttk.Button(top, text="Update Data", command=self.on_update_data).grid(
            row=0, column=0, padx=(0, 10)
        )

        ttk.Label(top, text="Cycle:").grid(row=0, column=1, sticky="w")
        self.cycle_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.cycle_var, width=8).grid(
            row=0, column=2, padx=(4, 12)
        )

        ttk.Label(top, text="Search Airport:").grid(row=0, column=3, sticky="w")
        self.search_var = tk.StringVar(value="")
        search_entry = ttk.Entry(top, textvariable=self.search_var)
        search_entry.grid(row=0, column=4, sticky="ew")
        search_entry.bind("<KeyRelease>", lambda _e: self.on_search())

        mid = ttk.Frame(self)
        mid.grid(row=1, column=0, sticky="ew", padx=10)
        mid.columnconfigure(1, weight=1)

        ttk.Label(mid, text="Airports").grid(row=0, column=0, sticky="w")
        ttk.Label(mid, text="Plates").grid(row=0, column=1, sticky="w")

        self.airport_list = tk.Listbox(mid, height=12)
        self.airport_list.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        self.airport_list.bind("<<ListboxSelect>>", lambda _e: self.on_airport_select())

        self.plate_list = tk.Listbox(mid, height=12, selectmode="extended")
        self.plate_list.grid(row=1, column=1, sticky="nsew")

        opts = ttk.Frame(self)
        opts.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 2))
        opts.columnconfigure(4, weight=1)

        self.all_plates_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="All plates", variable=self.all_plates_var).grid(
            row=0, column=0, padx=(0, 12)
        )

        self.allow_non_geo_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts,
            text="Allow non-georeferenced (download PDF only)",
            variable=self.allow_non_geo_var,
        ).grid(row=0, column=1, padx=(0, 12))

        ttk.Label(opts, text="Output folder:").grid(row=0, column=2, sticky="w")
        self.output_var = tk.StringVar(value=str(Path("output").resolve()))
        ttk.Entry(opts, textvariable=self.output_var, width=50).grid(
            row=0, column=3, sticky="ew", padx=(4, 6)
        )
        ttk.Button(opts, text="Browse", command=self.on_browse).grid(
            row=0, column=4, sticky="e"
        )

        filter_row = ttk.Frame(self)
        filter_row.grid(row=3, column=0, sticky="ew", padx=10)
        filter_row.columnconfigure(5, weight=1)

        ttk.Label(filter_row, text="Chart type:").grid(row=0, column=0, sticky="w")
        self.chart_code_var = tk.StringVar(value="Any")
        self.chart_code_combo = ttk.Combobox(
            filter_row, textvariable=self.chart_code_var, state="readonly", values=["Any"]
        )
        self.chart_code_combo.grid(row=0, column=1, padx=(4, 18))
        self.chart_code_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_filter_change())

        ttk.Label(filter_row, text="Approach:").grid(row=0, column=2, sticky="w")
        self.approach_var = tk.StringVar(value="Any")
        self.approach_combo = ttk.Combobox(
            filter_row, textvariable=self.approach_var, state="readonly", values=APPROACH_TYPES
        )
        self.approach_combo.grid(row=0, column=3, padx=(4, 18))
        self.approach_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_filter_change())

        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, sticky="ew", padx=10, pady=(6, 0))
        ttk.Button(actions, text="Generate", command=self.on_generate).pack(side="left")

        log_frame = ttk.Frame(self)
        log_frame.grid(row=5, column=0, sticky="nsew", padx=10, pady=(6, 10))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=16, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _load_last_cycle(self) -> None:
        if LAST_CYCLE_FILE.exists():
            self.cycle_var.set(LAST_CYCLE_FILE.read_text(encoding="utf-8").strip())

    def on_browse(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)

    def on_update_data(self) -> None:
        threading.Thread(target=self._update_data_worker, daemon=True).start()

    def _update_data_worker(self) -> None:
        try:
            self._log("Checking latest FAA d-TPP cycle...")
            cycle = self._fetch_latest_cycle()
            self.cycle_var.set(cycle)
            LAST_CYCLE_FILE.write_text(cycle, encoding="utf-8")

            self._log(f"Downloading d-TPP Metafile for cycle {cycle}...")
            xml_path = self._download_metafile(cycle)
            self._log(f"Saved: {xml_path}")

            self._log("Parsing airports and plates...")
            self.airports = self._parse_metafile(xml_path)
            self._log(f"Loaded {len(self.airports)} airports.")
            self.on_search()
        except Exception as exc:
            self._log(f"Error: {exc}")
            messagebox.showerror("Update Failed", str(exc))

    def on_search(self) -> None:
        query = self.search_var.get().strip().lower()
        self.airport_list.delete(0, "end")
        self.filtered_airports.clear()

        if not self.airports:
            return

        for apt in self.airports:
            hay = " ".join(
                [apt.name, apt.city, apt.state, apt.apt_ident, apt.icao_ident]
            ).lower()
            if query in hay:
                self.filtered_airports.append(apt)
                label = (
                    f"{apt.apt_ident} / {apt.icao_ident} | "
                    f"{apt.name} ({apt.city}, {apt.state})"
                )
                self.airport_list.insert("end", label)

    def on_airport_select(self) -> None:
        if not self.airport_list.curselection():
            return
        idx = self.airport_list.curselection()[0]
        self.selected_airport = self.filtered_airports[idx]
        self.plate_pool = list(self.selected_airport.plates)
        self._populate_chart_codes()
        self._refresh_plate_list()

    def on_filter_change(self) -> None:
        if not self.selected_airport:
            return
        self._refresh_plate_list()

    def on_generate(self) -> None:
        if not self.selected_airport:
            messagebox.showwarning("Missing Airport", "Select an airport first.")
            return

        output_dir = Path(self.output_var.get())
        output_dir.mkdir(parents=True, exist_ok=True)

        plates = self._get_selected_plates()
        if not plates:
            messagebox.showwarning("Missing Plates", "Select plate(s) or All plates.")
            return

        threading.Thread(
            target=self._generate_worker, args=(plates, output_dir), daemon=True
        ).start()

    def _get_selected_plates(self) -> List[PlateRecord]:
        if self.all_plates_var.get():
            return list(self.plate_index)

        selections = self.plate_list.curselection()
        return [self.plate_index[i] for i in selections]

    def _populate_chart_codes(self) -> None:
        codes = sorted({rec.chart_code for rec in self.plate_pool if rec.chart_code})
        values = ["Any"] + codes
        self.chart_code_combo["values"] = values
        if self.chart_code_var.get() not in values:
            self.chart_code_var.set("Any")

    def _refresh_plate_list(self) -> None:
        self.plate_list.delete(0, "end")
        self.plate_index = self._filter_plate_records(self.plate_pool)
        for rec in self.plate_index:
            self.plate_list.insert(
                "end", f"{rec.chart_code} | {rec.chart_name} | {rec.pdf_name}"
            )

    def _filter_plate_records(self, plates: List[PlateRecord]) -> List[PlateRecord]:
        chart_filter = self.chart_code_var.get()
        approach_filter = self.approach_var.get()
        results: List[PlateRecord] = []
        for rec in plates:
            if chart_filter != "Any" and rec.chart_code != chart_filter:
                continue
            approach = self._approach_type(rec)
            if approach_filter != "Any":
                if approach_filter == "Other" and approach != "Other":
                    continue
                if approach_filter != "Other" and approach != approach_filter:
                    continue
            results.append(rec)
        return results

    def _approach_type(self, rec: PlateRecord) -> str:
        name = f"{rec.chart_name} {rec.chart_code}".upper()
        if "ILS" in name:
            return "ILS"
        if "LOCALIZER" in name or re.search(r"\bLOC\b", name):
            return "LOC"
        if "RNAV" in name or "GPS" in name:
            return "RNAV/GPS"
        if "VOR" in name:
            return "VOR"
        if "NDB" in name:
            return "NDB"
        if "TACAN" in name:
            return "TACAN"
        if "LDA" in name:
            return "LDA"
        if "SDF" in name:
            return "SDF"
        if "GLS" in name:
            return "GLS"
        if "RNP" in name:
            return "RNP"
        if "LPV" in name:
            return "LPV"
        if "LNAV" in name:
            return "LNAV"
        return "Other"

    def _generate_worker(self, plates: List[PlateRecord], output_dir: Path) -> None:
        try:
            if not self._gdal_available():
                raise RuntimeError(
                    "GDAL tools not found in PATH (gdal_translate, gdalinfo)."
                )

            cycle = self.cycle_var.get().strip()
            if not cycle:
                raise RuntimeError("No cycle set. Click Update Data first.")

            self._log(f"Generating for {len(plates)} plate(s)...")

            geo_plates: List[PlateRecord] = []
            non_geo: List[PlateRecord] = []

            for rec in plates:
                pdf_path = self._download_plate_pdf(cycle, rec)
                if self._is_georeferenced(pdf_path):
                    geo_plates.append(rec)
                else:
                    non_geo.append(rec)

            if non_geo:
                if self.allow_non_geo_var.get():
                    for rec in non_geo:
                        pdf_path = self._download_plate_pdf(cycle, rec)
                        dest = output_dir / rec.pdf_name
                        shutil.copy2(pdf_path, dest)
                        self._log(f"Saved PDF (non-georef): {dest}")
                else:
                    self._log(
                        f"Skipped {len(non_geo)} non-georeferenced plate(s)."
                    )

            if not geo_plates:
                self._log("No georeferenced plates to export.")
                return

            if len(geo_plates) == 1 and not self.all_plates_var.get():
                rec = geo_plates[0]
                pdf_path = self._download_plate_pdf(cycle, rec)
                out_name = self._safe_name(
                    f"{self.selected_airport.apt_ident}_{rec.chart_name}.kmz"
                )
                out_path = output_dir / out_name
                self._log(f"Creating KMZ: {out_path}")
                self._gdal_kmz_single(pdf_path, out_path)
                self._log("Done.")
                return

            out_name = self._safe_name(
                f"{self.selected_airport.apt_ident}_ALL_PLATES.kmz"
            )
            out_path = output_dir / out_name
            self._log(f"Creating combined KMZ: {out_path}")
            self._gdal_kmz_multi(cycle, geo_plates, out_path)
            self._log("Done.")
        except Exception as exc:
            self._log(f"Error: {exc}")
            messagebox.showerror("Generate Failed", str(exc))

    def _gdal_available(self) -> bool:
        return bool(shutil.which("gdal_translate") and shutil.which("gdalinfo"))

    def _fetch_latest_cycle(self) -> str:
        resp = requests.get(f"{FAA_BASE}/", timeout=30)
        resp.raise_for_status()
        cycles = re.findall(r">(\d{4})<", resp.text)
        if not cycles:
            raise RuntimeError("No cycles found on FAA d-TPP index.")
        # Prefer the newest cycle that actually has the metafile.
        for cycle in sorted(set(cycles), reverse=True):
            meta_url = f"{FAA_BASE}/{cycle}/xml_data/d-TPP_Metafile.xml"
            try:
                head = requests.head(meta_url, timeout=15, allow_redirects=True)
                if head.status_code == 200:
                    return cycle
                if head.status_code in (403, 405):
                    get = requests.get(meta_url, stream=True, timeout=15)
                    if get.status_code == 200:
                        return cycle
            except requests.RequestException:
                continue
        raise RuntimeError("No cycles with d-TPP_Metafile.xml found.")

    def _download_metafile(self, cycle: str) -> Path:
        CYCLES_DIR.mkdir(parents=True, exist_ok=True)
        url = f"{FAA_BASE}/{cycle}/xml_data/d-TPP_Metafile.xml"
        out_path = CYCLES_DIR / f"d-TPP_Metafile_{cycle}.xml"
        self._http_download(url, out_path)
        return out_path

    def _parse_metafile(self, xml_path: Path) -> List[Airport]:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        airports: List[Airport] = []
        for state in root.findall("state_code"):
            state_id = state.attrib.get("ID", "")
            for city in state.findall("city_name"):
                city_id = city.attrib.get("ID", "")
                volume = city.attrib.get("volume", "")
                for apt in city.findall("airport_name"):
                    apt_name = apt.attrib.get("ID", "")
                    apt_ident = apt.attrib.get("apt_ident", "").strip()
                    icao_ident = apt.attrib.get("icao_ident", "").strip()
                    plates: List[PlateRecord] = []
                    for rec in apt.findall("record"):
                        chart_code = (rec.findtext("chart_code") or "").strip()
                        chart_name = (rec.findtext("chart_name") or "").strip()
                        pdf_name = (rec.findtext("pdf_name") or "").strip()
                        if not pdf_name:
                            continue
                        plates.append(
                            PlateRecord(
                                chart_code=chart_code,
                                chart_name=chart_name,
                                pdf_name=pdf_name,
                                volume=volume,
                            )
                        )
                    airports.append(
                        Airport(
                            name=apt_name,
                            city=city_id,
                            state=state_id,
                            apt_ident=apt_ident,
                            icao_ident=icao_ident,
                            volume=volume,
                            plates=plates,
                        )
                    )
        return airports

    def _download_plate_pdf(self, cycle: str, rec: PlateRecord) -> Path:
        dest_dir = CACHE_DIR / cycle
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / rec.pdf_name
        if dest.exists():
            return dest

        url = f"{FAA_BASE}/{cycle}/{rec.volume}/{rec.pdf_name}"
        self._log(f"Downloading {rec.pdf_name}...")
        self._http_download(url, dest)
        return dest

    def _is_georeferenced(self, pdf_path: Path) -> bool:
        try:
            result = subprocess.run(
                ["gdalinfo", "-json", str(pdf_path)],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            if data.get("geoTransform"):
                return True
            cc = data.get("cornerCoordinates")
            return bool(cc and cc.get("upperLeft"))
        except Exception:
            return False

    def _gdal_kmz_single(self, pdf_path: Path, out_path: Path) -> None:
        subprocess.run(
            [
                "gdal_translate",
                "-of",
                "KMLSUPEROVERLAY",
                str(pdf_path),
                str(out_path),
            ],
            check=True,
        )

    def _gdal_kmz_multi(self, cycle: str, plates: List[PlateRecord], out_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plate_dirs: List[Path] = []
            for rec in plates:
                pdf_path = self._download_plate_pdf(cycle, rec)
                slug = self._safe_name(f"{rec.chart_code}_{rec.chart_name}")
                plate_dir = tmp_dir / slug
                plate_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [
                        "gdal_translate",
                        "-of",
                        "KMLSUPEROVERLAY",
                        str(pdf_path),
                        str(plate_dir),
                    ],
                    check=True,
                )
                plate_dirs.append(plate_dir)

            master_kml = self._build_master_kml(plate_dirs)
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("doc.kml", master_kml)
                for plate_dir in plate_dirs:
                    for p in plate_dir.rglob("*"):
                        if p.is_file():
                            rel = p.relative_to(tmp_dir)
                            zf.write(p, rel.as_posix())

    def _build_master_kml(self, plate_dirs: List[Path]) -> str:
        lines = [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<kml xmlns=\"http://www.opengis.net/kml/2.2\">",
            "  <Document>",
            "    <name>FAA Plates</name>",
        ]
        for p in plate_dirs:
            name = p.name
            lines.extend(
                [
                    "    <NetworkLink>",
                    f"      <name>{name}</name>",
                    "      <Link>",
                    f"        <href>{name}/doc.kml</href>",
                    "      </Link>",
                    "    </NetworkLink>",
                ]
            )
        lines.extend(["  </Document>", "</kml>"])
        return "\n".join(lines)

    def _safe_name(self, name: str) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
        return name.strip("_")

    def _http_download(self, url: str, dest: Path) -> None:
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


if __name__ == "__main__":
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CYCLES_DIR.mkdir(parents=True, exist_ok=True)
    App().mainloop()
