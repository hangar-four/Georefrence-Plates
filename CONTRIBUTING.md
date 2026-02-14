# Contributing

Thanks for helping! This project is early and intentionally small.

## Quick start

1. Fork and clone the repo.
2. Create a branch for your change.
3. Create a venv and install deps:
   - `python -m venv .venv`
   - `.\.venv\Scripts\Activate.ps1`
   - `pip install -r requirements.txt`
4. Run the app:
   - `python src\app.py`

## Guidelines

- Keep the UI simple and Windows-first.
- Avoid adding heavy dependencies unless necessary.
- Prefer small, focused PRs.
- If you change behavior, update the README steps.

## Reporting issues

Please include:
- What you expected vs what happened
- Your Windows version
- Python version (`python --version`)
- Whether GDAL tools are installed and on PATH
