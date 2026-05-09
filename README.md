# Earthquakes

Pull the USGS M≥4.0 earthquake catalog into a local SQLite database, then explore it in a Jupyter notebook.

## What it does

`fetch_quakes.py` queries the [USGS FDSN event service](https://earthquake.usgs.gov/fdsnws/event/1/) in yearly chunks, auto-splitting any year that exceeds the API's 20,000-result cap into months. Results land in `quakes.sqlite` (~110 MB for 1965–today, ~530k events). The fetcher is idempotent on event id and resumable: re-running only processes missing chunks, and the current year is always re-fetched.

`earthquakes.ipynb` reads the database and produces:

- Magnitude vs. time scatter (M≥7.0 events highlighted)
- Yearly counts stacked by magnitude band
- Magnitude frequency distribution (log scale)
- Summary stats and top events

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Fetch the data

```bash
python fetch_quakes.py
```

Defaults to M≥4.0, 1965 → today. Override with `--start-year`, `--end-year`, `--min-mag`, `--db`. Pre-1965 data is sparse globally; treat earlier years as undercounting reality.

## Open the notebook

```bash
jupyter notebook earthquakes.ipynb
```
