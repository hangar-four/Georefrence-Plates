# FAA Plate KMZ Builder

This GUI helps non-technical users download FAA d-TPP GeoPDF approach plates and drop them straight into Google Earth.

## Installer steps (baby steps)

1. **Create the Python sandbox (one-time)**
   - Run `python -m venv .venv`
   - Activate it with `.\.venv\Scripts\Activate.ps1`
2. **Install the Python pieces**
   - Inside the activated shell, run `pip install -r requirements.txt`
3. **Launch the helper**
   - Run `python src\app.py` from the same activated shell
4. **First run GDAL setup (automatic)**
   - If GDAL is missing, the app will ask to download and install it automatically.

## Using the app (step-by-step)

1. Click **Update Data** to download the latest FAA `d-TPP_Metafile.xml` (stored under `data/cycles/`).
2. Type an airport name, FAA ident, or ICAO ident into “Search Airport.” Matching airports appear in the list.
3. Select an airport, then use the **Chart type** and **Approach** filters if you want to narrow the plate list.
4. Leave **All plates** checked to export everything, or uncheck it and select the plates you need from the list.
5. If you want non-georeferenced plates saved as PDF, turn on **Allow non-georeferenced (download PDF only)**.
6. Set the output folder (click **Browse** if needed) and press **Generate** to create the KMZ/PDF files.

## Output and cache

- KMZ overlays and optional PDFs go to the folder you chose in the UI.
- Downloaded PDFs and cycle XML stay under `data/cache/` and `data/cycles/` for reuse.
- GDAL downloads are stored under `%LOCALAPPDATA%\Georef-Plates\gdal`.

## Manual GDAL (optional)

If you do not want the auto-download, you can install GDAL yourself and make sure `gdal_translate` and `gdalinfo` are on PATH.
