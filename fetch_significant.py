#!/usr/bin/env python3
"""Fetch significant earthquake fatality data into quakes.sqlite.

Primary source (since 2026-07): NOAA NCEI/NGDC Significant Earthquake
Database, pulled directly from the hazel hazard-service API (paginated).
This is the same upstream the old GitHub mirror snapshotted in 2017, but
live — it currently runs through the present.

On a failed NGDC refresh, retain any existing snapshot unchanged. Fallbacks are
only used to bootstrap an empty database: first the 2017 mirror, or local TSV
if the mirror is unavailable. These sources are never appended to an existing
NGDC table or mixed with one another. Degraded/stale runs return nonzero status.

Writes into a `significant_quakes` table in quakes.sqlite alongside the
existing M≥4 catalog. When the NGDC pull succeeds, the table is rebuilt
from NGDC alone (single source of truth); a guard refuses the rebuild if
the fresh pull is >5% smaller than what the table already holds.

IMPORTANT: earthquake fatalities are not a measure of seismicity. They're
a measure of where people happened to be living. A M6.0 under a megacity
kills thousands; a M8.5 in the open ocean kills nobody. Read these numbers
as a human-exposure story, not a seismic one.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NGDC_URL = 'https://www.ngdc.noaa.gov/hazel/hazard-service/api/v1/earthquakes'
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
CREATE TABLE IF NOT EXISTS significant_refresh_status (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    detail TEXT NOT NULL,
    start_year INTEGER NOT NULL,
    end_year INTEGER NOT NULL,
    row_count INTEGER NOT NULL
);
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
    if year is None or year < 1 or year > 9999 or month is None or day is None:
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


def fetch_ngdc_rows(start_year, end_year, page_size=200, sleep=0.4):
    """Pull the live NGDC significant-earthquake catalog, yield insert tuples."""
    print(f'Fetching NGDC live: {NGDC_URL}')
    page = 1
    count = 0
    total = None
    while True:
        url = (f'{NGDC_URL}?minYear={start_year}&maxYear={end_year}'
               f'&page={page}&itemsPerPage={page_size}')
        req = urllib.request.Request(
            url, headers={'User-Agent': 'earthquakes-repo-fetch/2.0'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        items = data.get('items') or []
        total = data.get('totalItems', total)
        for row in items:
            year = _try_int(row.get('year'))
            if year is None:
                continue
            month = _try_int(row.get('month'))
            day = _try_int(row.get('day'))
            hour = _try_int(row.get('hour'))
            minute = _try_int(row.get('minute'))
            second = _try_int(row.get('second'))
            mag = _try_float(row.get('eqMagnitude'))
            deaths = _try_int(row.get('deathsTotal'))
            damage = _try_float(row.get('damageMillionsDollarsTotal'))
            lat = _try_float(row.get('latitude'))
            lon = _try_float(row.get('longitude'))
            loc_name = (row.get('locationName') or '').strip()
            tm = _to_time_ms(year, month, day, hour, minute, second)
            rid = ('ngdc_' + str(row['id'])) if row.get('id') is not None else _row_id(year, month, day, lat, lon, mag, 'ngdc')
            yield (rid, tm, year, month, day, mag, lat, lon, loc_name,
                   deaths, damage, 'ngdc')
            count += 1
        if not items or (total is not None and count >= total):
            break
        page += 1
        time.sleep(sleep)
    print(f'  → {count} NGDC rows in {start_year}–{end_year}')


def fetch_mirror_rows(start_year, end_year):
    """FALLBACK: 2017 GitHub snapshot of the NGDC catalog."""
    print(f'Fetching mirror (2017 snapshot): {MIRROR_CSV_URL}')
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
    """FALLBACK: hand-curated recent significant events from local TSV."""
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


def _save_refresh_status(conn, status, source, detail, start_year, end_year):
    count = conn.execute('SELECT COUNT(*) FROM significant_quakes').fetchone()[0]
    result = dict(checked_at=datetime.now(timezone.utc).isoformat(), status=status,
                  source=source, detail=detail, start_year=start_year,
                  end_year=end_year, row_count=count)
    conn.execute(
        'INSERT OR REPLACE INTO significant_refresh_status '
        '(singleton,checked_at,status,source,detail,start_year,end_year,row_count) '
        'VALUES (1,?,?,?,?,?,?,?)', tuple(result.values()))
    conn.commit()
    return result


def refresh_significant(conn, start_year=1900, end_year=2099, *,
                        mirror_only=False, local_only=False):
    """Stage a complete single-source replacement or retain the old snapshot.

    A network failure or shrink rejection never appends fallback copies to
    existing events. Initial fallback bootstrap chooses one source only.
    Narrower ranges cannot erase an existing broader catalogue.
    """
    n_before = conn.execute('SELECT COUNT(*) FROM significant_quakes').fetchone()[0]
    existing_source = ','.join(row[0] for row in conn.execute(
        'SELECT DISTINCT source FROM significant_quakes ORDER BY source'))
    rows, source, error = [], '', ''
    if not (mirror_only or local_only):
        try:
            rows = list(fetch_ngdc_rows(start_year, end_year))
            # Count unique source identities, not accidental repeated pages.
            rows = list({row[0]: row for row in rows}.values())
            if not rows:
                raise ValueError('NGDC returned no events')
            if n_before and len(rows) < 0.95 * n_before:
                raise ValueError(f'NGDC shrink guard: {len(rows)} new vs {n_before} existing rows')
            outside = conn.execute('SELECT COUNT(*) FROM significant_quakes WHERE year<? OR year>?',
                                   (start_year,end_year)).fetchone()[0]
            if outside:
                raise ValueError('Requested range would erase existing out-of-range events')
            source = 'ngdc'
        except Exception as exc:
            error = str(exc)
            rows = []
            print(f'  ! NGDC refresh rejected: {error}', file=sys.stderr)

    if not rows and n_before:
        detail = error or 'Explicit fallback mode does not overwrite a nonempty snapshot'
        return _save_refresh_status(conn, 'retained_stale', existing_source,
                                    detail, start_year, end_year)

    if not rows and not local_only:
        try:
            rows = list(fetch_mirror_rows(start_year,end_year))
            source = 'noaa_mirror_2017'
        except Exception as exc:
            error += f'; mirror failed: {exc}'
            rows = []
    if not rows and not mirror_only:
        rows = [row for row in (fetch_local_recent() or [])
                if row[2] is not None and start_year <= row[2] <= end_year]
        source = 'local_recent'
    if not rows:
        return _save_refresh_status(conn, 'unavailable', source,
                                    error or 'No source provided events', start_year,end_year)
    if any(row[2] is None or not start_year <= row[2] <= end_year for row in rows):
        return _save_refresh_status(conn, 'retained_stale' if n_before else 'unavailable',
                                    existing_source, 'Source returned out-of-range events', start_year,end_year)

    # Atomic replacement: validation/insertion failure rolls back deletion.
    with conn:
        conn.execute('DELETE FROM significant_quakes')
        conn.executemany(
            'INSERT OR REPLACE INTO significant_quakes '
            '(id,time_ms,year,month,day,mag,lat,lon,location,deaths,damage_musd,source) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    status = 'fresh' if source == 'ngdc' else 'degraded_bootstrap'
    detail = 'Single source snapshot replacement' if status == 'fresh' else (
        'Historical/selected fallback only; no complete recent surveillance. ' + error)
    return _save_refresh_status(conn,status,source,detail,start_year,end_year)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--start-year', type=int, default=1900)
    parser.add_argument('--end-year', type=int, default=2099)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--mirror-only', action='store_true',
                       help='Bootstrap an empty DB from mirror only; preserve any existing snapshot')
    modes.add_argument('--local-only', action='store_true',
                       help='Bootstrap an empty DB from local TSV only; preserve any existing snapshot')
    args = parser.parse_args()
    if args.start_year > args.end_year:
        parser.error('start-year must be <= end-year')

    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(SCHEMA)
        result = refresh_significant(conn,args.start_year,args.end_year,
                                     mirror_only=args.mirror_only,local_only=args.local_only)
        print(json.dumps(result,indent=2))
        destination = Path(str(args.db)+'.significant.status.json')
        temporary = destination.with_name(destination.name+'.tmp')
        temporary.write_text(json.dumps(result,indent=2)+'\n')
        temporary.replace(destination)
        return 0 if result['status'] == 'fresh' else 2
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
