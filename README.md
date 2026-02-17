# FAA Plate Georef (Runway-Click)

Windows Python app to download FAA approach plates, georeference them with runway/fix clicks, and export KMZ overlays for Google Earth.

## Install (baby steps)

1. Open PowerShell in the project folder.
2. Create virtual environment:
   - `python -m venv .venv`
3. Activate it:
   - `.\.venv\Scripts\Activate.ps1`
4. Install packages:
   - `python -m pip install -r requirements.txt`
5. Start app:
   - `python src\app.py`

## What it does now

- Starts maximized/full screen.
- Loads airport/runway/fix data from FAA NASR.
- Fetches approach list from APRA/TPP metadata.
- Downloads selected approach PDF (with local TPP ZIP fallback if FAA URL is 404).
- Lets you:
  - click runway threshold A and B
  - optionally add multiple fix/VOR refine clicks
  - export KMZ (`gx:LatLonQuad`) for Google Earth.
- Opens output folder automatically after KMZ export.

## Main workflow

1. Click `Update NASR`.
2. Search/select airport and runway.
3. Click `Fetch Approaches`.
4. Select approach and click `Load Selected PDF`.
5. Click runway end A then runway end B on chart.
6. Optional refine:
   - select fix (you can type to search)
   - click `Refine`
   - click that fix on the chart
   - repeat for more fixes if needed.
7. Click `Generate KMZ`.

## Output naming

KMZ file name format:
- `<AIRPORT_ID> <PROCEDURE_NAME>.kmz`

Example:
- `GEG ILS RWY 03 (CAT II - III).kmz`

## Session status

Bottom status line shows:
- NASR cycle in use
- TPP cycle in use
- APRA credential status

## APRA keys

Use `APRA Keys` button in app and save credentials.
Saved locally in:
- `data/apra_config.json`

Supported auth styles:
- API key: `APRA_API_KEY`, `APRA_API_KEY_HEADER`
- Client headers: `APRA_CLIENT_ID`, `APRA_CLIENT_SECRET` (+ header names)
- OAuth client credentials: `APRA_TOKEN_URL`, `APRA_CLIENT_ID`, `APRA_CLIENT_SECRET`, optional `APRA_SCOPE`

Default base URL:
- `https://external-api.faa.gov/apra`

## Caching

- NASR ZIP + parsed cache:
  - `data/nasr/`
- TPP bundles + index:
  - `data/tpp/`
  - `data/tpp_index.json`
- Downloaded approach PDFs:
  - `data/cache/`
