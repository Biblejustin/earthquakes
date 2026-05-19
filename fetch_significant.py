#!/usr/bin/env python3
"""Fetch significant earthquake fatality data into quakes.sqlite.

Combines two sources because NOAA NCEI's direct API isn't always reachable:

1. Historical (1900–2017): GitHub mirror of NOAA NCEI Significant Earthquake
   Database (benjiao/significant-earthquakes). Well-curated, but stops in 2017.

2. Recent (2018–today): A local hand-curated TSV at recent_significant.tsv,
   sourced from Wikipedia and USGS. Should be replaced with a fresh NOAA pull
   when their API is reachable.

Writes into a `significant_quakes` table in quakes.sqlite alongside the
existing M≥4 catalog.

IMPORTANT: earthquake fatalities are not a measure of seismicity. They're
a measure of where people happened to be living. A M6.0 under a megacity
kills thousands; a M8.5 in the open ocean kills nobody. Read these numbers
as a human-exposure story, not a seismic one.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MIRROR_CSV_URL = (
    'https://raw.githubusercontent.com/benjiao/significant-earthquakes/'
    'master/earthquakes.csv'
)
LOCAL_RECENT_TSV = Path(__file__).parent / 'recent_significant.tsv'
DEFAULT_DB = Path(__file__).parent / 'quakes.sqlite'

SCHEMA = """
CREATE TABLE IF NOT EXISTS significant_quakes (
    id            TEXT PRIMARY KEY,
    time_ms       INTEGER,
    year          INTEGER,
    month         INTEGER,
    day           INTEGER,
    mag           REAL,
    lat           REAL,
    lon           REAL,
    location      TEXT,
    deaths        INTEGER,
    damage_musd   REAL,
    source        TEXT
);

CREATE INDEX IF NOT EXISTS sig_year ON significant_quakes(year);
CREATE INDEX IF NOT EXISTS sig_deaths ON significant_quakes(deaths);
"""

POINT_RE = re.compile(r'POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)')


def _try_float(s):
    if s is None or s == '':
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _try_int(s):
    f = _try_float(s)
    return int(f) if f is not None else None


def _parse_point(s):
    if not s:
        return None, None
    m = POINT_RE.search(s)
    if not m:
        return None, None
    lon, lat = float(m.group(1)), float(m.group(2))
    return lat, lon


def _to_time_ms(year, month, day, hour, minute, second):
    """Compose a UTC timestamp in milliseconds. Tolerate missing sub-day fields."""
    if year is None or year < 1 or year > 9999:
        # Negative or huge years (BC, fragmentary) — skip for our 1900+ scope
        return None
    try:
        dt = datetime(
            int(year),
            int(month) if month else 1,
            int(day) if day else 1,
            int(hour) if hour else 0,
            int(minute) if minute else 0,
            int(second) if second else 0,
            tzinfo=timezone.utc,
        )
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def _row_id(year, month, day, lat, lon, mag, src):
    """Deterministic primary key. Tolerates missing fields."""
    parts = [
        src,
        str(int(year)) if year is not None else '?',
        str(int(month)) if month is not None else '?',
        str(int(day)) if day is not None else '?',
        f'{lat:.2f}' if lat is not None else '?',
        f'{lon:.2f}' if lon is not None else '?',
        f'{mag:.1f}' if mag is not None else '?',
    ]
    return '_'.join(parts)


def fetch_mirror_rows(start_year, end_year):
    """Pull NOAA mirror CSV, yield dicts ready for insertion."""
    print(f'Fetching mirror: {MIRROR_CSV_URL}')
    with urllib.request.urlopen(MIRROR_CSV_URL, timeout=30) as resp:
        text = resp.read().decode('utf-8')
    reader = csv.DictReader(io.StringIO(text))
    count = 0
    for row in reader:
        year = _try_int(row.get('year'))
        if year is None or year < start_year or year > end_year:
            continue
        month = _try_int(row.get('month'))
        day = _try_int(row.get('day'))
        hour = _try_int(row.get('hour'))
        minute = _try_int(row.get('minute'))
        second = _try_int(row.get('second'))
        mag = _try_float(row.get('magnitude'))
        deaths = _try_int(row.get('deaths'))
        damage = _try_float(row.get('damage'))
        lat, lon = _parse_point(row.get('location'))
        loc_name = (row.get('name') or '').strip()
        tm = _to_time_ms(year, month, day, hour, minute, second)
        rid = _row_id(year, month, day, lat, lon, mag, 'noaa_mirror')
        yield (rid, tm, year, month, day, mag, lat, lon, loc_name,
               deaths, damage, 'noaa_mirror_2017')
        count += 1
    print(f'  → {count} mirror rows in {start_year}–{end_year}')


def fetch_local_recent():
    """Read hand-curated recent significant events from local TSV."""
    if not LOCAL_RECENT_TSV.exists():
        print(f'No local recent file at {LOCAL_RECENT_TSV} — skipping')
        return
    print(f'Reading local recent file: {LOCAL_RECENT_TSV}')
    count = 0
    with open(LOCAL_RECENT_TSV, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for row in reader:
            year = _try_int(row.get('year'))
            month = _try_int(row.get('month'))
            day = _try_int(row.get('day'))
            mag = _try_float(row.get('magnitude'))
            deaths = _try_int(row.get('deaths'))
            damage = _try_float(row.get('damage_musd'))
            lat = _try_float(row.get('lat'))
            lon = _try_float(row.get('lon'))
            loc_name = (row.get('location') or '').strip()
            tm = _to_time_ms(year, month, day, 0, 0, 0)
            rid = _row_id(year, month, day, lat, lon, mag, 'local_recent')
            yield (rid, tm, year, month, day, mag, lat, lon, loc_name,
                   deaths, damage, 'local_recent')
            count += 1
    print(f'  → {count} local recent rows')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB,
                        help=f'SQLite database path (default: {DEFAULT_DB})')
    parser.add_argument('--start-year', type=int, default=1900)
    parser.add_argument('--end-year', type=int, default=2099)
    parser.add_argument('--mirror-only', action='store_true',
                        help='Skip local recent TSV')
    parser.add_argument('--local-only', action='store_true',
                        help='Skip mirror fetch')
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    n_before = conn.execute('SELECT COUNT(*) FROM significant_quakes').fetchone()[0]

    rows = []
    if not args.local_only:
        try:
            rows.extend(fetch_mirror_rows(args.start_year, args.end_year))
        except Exception as e:
            print(f'  ! mirror fetch failed: {e}', file=sys.stderr)
            if not args.mirror_only:
                print('  → continuing with local recent only')
            else:
                raise

    if not args.mirror_only:
        rows.extend(fetch_local_recent())

    conn.executemany(
        'INSERT OR REPLACE INTO significant_quakes '
        '(id, time_ms, year, month, day, mag, lat, lon, location, '
        'deaths, damage_musd, source) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        rows,
    )
    conn.commit()

    n_after = conn.execute('SELECT COUNT(*) FROM significant_quakes').fetchone()[0]
    n_with_deaths = conn.execute(
        'SELECT COUNT(*) FROM significant_quakes WHERE deaths IS NOT NULL AND deaths > 0'
    ).fetchone()[0]
    total_deaths = conn.execute(
        'SELECT SUM(deaths) FROM significant_quakes WHERE deaths IS NOT NULL'
    ).fetchone()[0]
    span = conn.execute(
        'SELECT MIN(year), MAX(year) FROM significant_quakes'
    ).fetchone()
    conn.close()

    print()
    print(f'significant_quakes table: {n_before:,} → {n_after:,} '
          f'rows ({n_after - n_before:+,})')
    print(f'  Events with recorded deaths: {n_with_deaths:,}')
    print(f'  Total deaths (sum of recorded): {total_deaths:,}')
    print(f'  Year span: {span[0]} → {span[1]}')


if __name__ == '__main__':
    main()
