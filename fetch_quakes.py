#!/usr/bin/env python3
"""Fetch USGS earthquake catalog into a local SQLite database.

Pulls from the USGS FDSN event service in yearly chunks. Any year that exceeds
the API's 20,000-result cap is auto-split into months. Resumable: completed
chunks are recorded, and re-running only processes what's missing (the current
year is always re-fetched). Idempotent on event id, so re-fetches just refresh
existing rows.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://earthquake.usgs.gov/fdsnws/event/1/query"

SCHEMA = """
CREATE TABLE IF NOT EXISTS quakes (
    id        TEXT PRIMARY KEY,
    time_ms   INTEGER NOT NULL,
    mag       REAL    NOT NULL,
    mag_type  TEXT,
    lat       REAL,
    lon       REAL,
    depth_km  REAL,
    place     TEXT,
    url       TEXT
);
CREATE INDEX IF NOT EXISTS idx_quakes_time ON quakes(time_ms);
CREATE INDEX IF NOT EXISTS idx_quakes_mag  ON quakes(mag);

CREATE TABLE IF NOT EXISTS chunks (
    start_iso   TEXT NOT NULL,
    end_iso     TEXT NOT NULL,
    granularity TEXT NOT NULL,
    fetched_at  INTEGER NOT NULL,
    count       INTEGER NOT NULL,
    PRIMARY KEY (start_iso, end_iso)
);
"""


def fetch_chunk(start_iso: str, end_iso: str, min_mag: float) -> dict | None:
    """Return GeoJSON dict, or None if the API rejected the query as too large."""
    params = {
        "format": "geojson",
        "starttime": start_iso,
        "endtime": end_iso,
        "minmagnitude": min_mag,
        "orderby": "time-asc",
    }
    r = requests.get(API, params=params, timeout=180)
    if r.status_code == 400:
        # USGS returns 400 with a "result count exceeds maximum" message
        # when the 20K cap is hit. Caller handles by splitting.
        return None
    r.raise_for_status()
    return r.json()


def upsert(conn: sqlite3.Connection, features: list[dict]) -> int:
    rows = []
    for feat in features:
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        lon = coords[0] if len(coords) > 0 else None
        lat = coords[1] if len(coords) > 1 else None
        depth = coords[2] if len(coords) > 2 else None
        rows.append((
            feat.get("id"),
            props.get("time"),
            props.get("mag"),
            props.get("magType"),
            lat,
            lon,
            depth,
            props.get("place"),
            props.get("url"),
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO quakes "
        "(id, time_ms, mag, mag_type, lat, lon, depth_km, place, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def chunk_done(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> int | None:
    row = conn.execute(
        "SELECT count FROM chunks WHERE start_iso=? AND end_iso=?",
        (start_iso, end_iso),
    ).fetchone()
    return row[0] if row else None


def record_chunk(conn, start_iso, end_iso, granularity, count) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO chunks "
        "(start_iso, end_iso, granularity, fetched_at, count) "
        "VALUES (?, ?, ?, ?, ?)",
        (start_iso, end_iso, granularity, int(time.time()), count),
    )


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = f"{year}-{month:02d}-01T00:00:00"
    if month == 12:
        end = f"{year + 1}-01-01T00:00:00"
    else:
        end = f"{year}-{month + 1:02d}-01T00:00:00"
    return start, end


def fetch_year(conn, year: int, min_mag: float, sleep: float, force: bool) -> int:
    start = f"{year}-01-01T00:00:00"
    end = f"{year + 1}-01-01T00:00:00"
    if not force and chunk_done(conn, start, end) is not None:
        return 0

    print(f"  {year}: ", end="", flush=True)
    data = fetch_chunk(start, end, min_mag)
    if data is None:
        # 20K cap hit — split by month
        print("over cap, splitting by month: ", end="", flush=True)
        total = 0
        for m in range(1, 13):
            mstart, mend = month_bounds(year, m)
            mdata = fetch_chunk(mstart, mend, min_mag)
            if mdata is None:
                raise RuntimeError(
                    f"Month {year}-{m:02d} also exceeds the 20K cap; "
                    f"need finer-grained splitting (not implemented)."
                )
            n = upsert(conn, mdata.get("features", []))
            record_chunk(conn, mstart, mend, "month", n)
            conn.commit()
            total += n
            print(f"{m:02d}={n} ", end="", flush=True)
            time.sleep(sleep)
        record_chunk(conn, start, end, "year", total)
        conn.commit()
        print(f"→ {total} total")
        return total

    n = upsert(conn, data.get("features", []))
    record_chunk(conn, start, end, "year", n)
    conn.commit()
    print(f"{n}", flush=True)
    time.sleep(sleep)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-year", type=int, default=1965)
    ap.add_argument("--end-year", type=int, default=datetime.now(timezone.utc).year)
    ap.add_argument("--min-mag", type=float, default=4.0)
    ap.add_argument("--db", default=str(Path(__file__).parent / "quakes.sqlite"))
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.4,
        help="Seconds to wait between requests (rate-limit courtesy)",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.commit()

    this_year = datetime.now(timezone.utc).year
    print(
        f"Fetching M>={args.min_mag} from {args.start_year} through "
        f"{args.end_year} → {args.db}"
    )
    total = 0
    for year in range(args.start_year, args.end_year + 1):
        # Always re-fetch the current year — it keeps growing
        force = year >= this_year
        total += fetch_year(conn, year, args.min_mag, args.sleep, force=force)

    final = conn.execute("SELECT COUNT(*) FROM quakes").fetchone()[0]
    earliest, latest = conn.execute(
        "SELECT MIN(time_ms), MAX(time_ms) FROM quakes"
    ).fetchone()
    if earliest:
        e = datetime.fromtimestamp(earliest / 1000, tz=timezone.utc)
        l = datetime.fromtimestamp(latest / 1000, tz=timezone.utc)
        print(f"\nDatabase span: {e:%Y-%m-%d} → {l:%Y-%m-%d}")
    print(f"Events processed this run: {total:,}")
    print(f"Total events in database:  {final:,}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
