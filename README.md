# Earthquakes

Pull the USGS M≥4.0 earthquake catalog into a local SQLite database, then explore it in a Jupyter notebook.

## What it does

`fetch_quakes.py` queries the [USGS FDSN event service](https://earthquake.usgs.gov/fdsnws/event/1/) in yearly chunks, auto-splitting any year that exceeds the API's 20,000-result cap into months. Results land in `quakes.sqlite` (~110 MB for 1965–today, ~530k events). The fetcher is idempotent on event id and resumable: re-running only processes missing chunks, and the current year is always re-fetched.

`earthquakes.ipynb` reads the database and produces the four plots below. Each is also written to `figures/` so you can browse them on GitHub without running the notebook.

## Sample output

### Magnitude vs. time

![Magnitude vs time](figures/01_magnitude_vs_time.png)

### Yearly counts by magnitude band

The trend line is fit on the post-2000 era only — once digital regional networks were fully online and M4 detection had largely stabilized. A full-span fit would mostly track network-coverage gains rather than seismicity, so it's omitted.

![Yearly counts by magnitude band](figures/02_yearly_by_band.png)

### M≥7.0 yearly counts (the detection-bias control)

Global instrumentation has been complete for M7+ for ~100 years, so this band isn't affected by the same detection-improvement bias. If the apparent trend at M4 were real seismicity, this line would rise too. It's essentially flat — the M4 trend is detection, not actual quakes.

![M7+ yearly counts](figures/03_m7_yearly.png)

### Magnitude distribution

![Magnitude distribution](figures/04_magnitude_distribution.png)

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

Re-executing the notebook refreshes the PNGs in `figures/` as a side effect.
