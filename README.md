# FAA Plate Georef (Runway-Click)

This app georeferences approach plates and airport diagrams with a simple two-click workflow using runway endpoints from FAA NASR data.

## Installer steps (baby steps)

1. **Create the Python sandbox (one-time)**
   - Run `python -m venv .venv`
   - Activate it with `.\.venv\Scripts\Activate.ps1`
2. **Install the Python pieces**
   - Inside the activated shell, run `pip install -r requirements.txt`
3. **Launch the app**
   - Run `python src\app.py`

## Workflow (two-click georef)

1. Click **Update NASR** (downloads runway endpoints + fixes).
2. Click **Load PDF** and select your chart.
3. Search for the airport and select the runway.
4. Click **Runway end A** and **Runway end B** on the chart.
5. (Optional) Pick a nearby fix/VOR and click **Refine** for a 3-point fit.
6. Click **Generate KMZ** and open it in Google Earth.

## Notes

- Default rendering is **400 DPI** for stable geometry.
- If runway length error > 5%, the app asks you to re-click.
- Output is a KMZ GroundOverlay using `gx:LatLonQuad` for rotation support.

## Output and cache

- KMZ files go to the folder you select in the UI.
- NASR downloads are cached under `data/nasr/`.
