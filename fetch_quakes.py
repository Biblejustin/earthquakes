#!/usr/bin/env python3
"""Fetch USGS earthquake catalog into a local SQLite database.

Pulls from the USGS FDSN event service in yearly chunks. Any year that exceeds
the API's 20,000-result cap is auto-split into months. Resumable: completed
chunks are recorded with a query fingerprint; changing the magnitude floor
refetches historical chunks. Legacy cache entries without query parameters are
not trusted. Current year is always refetched; historical caches expire after
30 days by default. Complete responses reconcile only their exact time/magnitude
scope, with removed records retained in an audit table.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
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

-- Legacy cache retained for audit; its query parameters were never stored.
CREATE TABLE IF NOT EXISTS chunks (
    start_iso   TEXT NOT NULL,
    end_iso     TEXT NOT NULL,
    granularity TEXT NOT NULL,
    fetched_at  INTEGER NOT NULL,
    count       INTEGER NOT NULL,
    PRIMARY KEY (start_iso, end_iso)
);
CREATE TABLE IF NOT EXISTS chunks_v2 (
    start_iso TEXT NOT NULL,
    end_iso TEXT NOT NULL,
    query_fingerprint TEXT NOT NULL,
    min_magnitude REAL NOT NULL,
    granularity TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (start_iso, end_iso, query_fingerprint)
);
CREATE TABLE IF NOT EXISTS quake_reconciliations (
    id INTEGER PRIMARY KEY,
    reconciled_at TEXT NOT NULL,
    start_iso TEXT NOT NULL,
    end_iso TEXT NOT NULL,
    min_magnitude REAL NOT NULL,
    query_fingerprint TEXT NOT NULL,
    incoming_count INTEGER NOT NULL,
    removed_count INTEGER NOT NULL,
    response_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS removed_quake_records (
    reconciliation_id INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    previous_record_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(reconciliation_id,event_id),
    FOREIGN KEY(reconciliation_id) REFERENCES quake_reconciliations(id)
);
CREATE TABLE IF NOT EXISTS excluded_usgs_response_records (
    reconciliation_id INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    preferred_record_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(reconciliation_id,event_id),
    FOREIGN KEY(reconciliation_id) REFERENCES quake_reconciliations(id)
);
"""


def _utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    result = result.astimezone(timezone.utc)
    if result.microsecond % 1000:
        raise ValueError('Query bounds must use whole milliseconds')
    return result


def _bounds(start_iso, end_iso):
    start, end = _utc(start_iso), _utc(end_iso)
    if end <= start:
        raise ValueError('Query end must be after start')
    return int(start.timestamp()*1000), int(end.timestamp()*1000)


def _params(start_iso, end_iso, min_mag):
    _bounds(start_iso,end_iso)
    if not math.isfinite(min_mag):
        raise ValueError('Minimum magnitude must be finite')
    # USGS start/end are inclusive. Local interval is [start,end), so request
    # through the last included millisecond; adjacent chunks never overlap.
    return dict(format='geojson',starttime=_utc(start_iso).isoformat(timespec='milliseconds'),
                endtime=(_utc(end_iso)-timedelta(milliseconds=1)).isoformat(timespec='milliseconds'),
                minmagnitude=min_mag,orderby='time-asc')


def validate_response(data, start_iso, end_iso, min_mag, *, allow_below_floor=False):
    """Validate completeness and every mutation-relevant field before any write."""
    if not isinstance(data,dict) or data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'),list):
        raise ValueError('USGS response must be a complete GeoJSON FeatureCollection')
    metadata=data.get('metadata')
    if not isinstance(metadata,dict) or isinstance(metadata.get('count'),bool):
        raise ValueError('USGS response requires integer metadata.count')
    count=metadata.get('count')
    if not isinstance(count,int) or count < 0 or count != len(data['features']):
        raise ValueError('USGS metadata count disagrees with returned features')
    if metadata.get('status',200) != 200:
        raise ValueError('USGS response metadata indicates failure')
    lo,hi=_bounds(start_iso,end_iso)
    if not math.isfinite(min_mag):
        raise ValueError('Minimum magnitude must be finite')
    seen=set()
    for feature in data['features']:
        if not isinstance(feature,dict) or not isinstance(feature.get('id'),str) or not feature['id']:
            raise ValueError('USGS event requires stable nonempty ID')
        if feature['id'] in seen:
            raise ValueError('USGS response repeats an event ID')
        seen.add(feature['id'])
        props=feature.get('properties')
        if not isinstance(props,dict):
            raise ValueError('USGS event requires properties')
        moment,mag=props.get('time'),props.get('mag')
        if (isinstance(moment,bool) or not isinstance(moment,(int,float)) or not math.isfinite(moment)
                or int(moment) != moment or not lo <= moment < hi):
            raise ValueError('USGS event timestamp outside exact requested scope')
        if (isinstance(mag,bool) or not isinstance(mag,(int,float)) or not math.isfinite(mag)
                or (mag < min_mag and not allow_below_floor)):
            raise ValueError('USGS event magnitude outside requested scope')
        if props.get('status') == 'deleted':
            raise ValueError('USGS live response unexpectedly includes deleted event')
    return data['features']


def fetch_chunk(start_iso: str, end_iso: str, min_mag: float) -> dict | None:
    """Return a validated complete response, or None only for result-cap errors."""
    params=_params(start_iso,end_iso,min_mag)
    response=requests.get(API,params=params,timeout=180)
    if response.status_code == 400 and 'exceed' in response.text.lower() and ('maximum' in response.text.lower() or 'limit' in response.text.lower()):
        return None
    response.raise_for_status()
    if response.status_code == 204:
        # Empty HTTP bodies have no count metadata. Corroborate an authoritative
        # zero with the same service's count query before removing scoped rows.
        count_params={k:v for k,v in params.items() if k not in ('format','orderby')}
        count_params['format']='text'
        count_response=requests.get(API.removesuffix('/query')+'/count',params=count_params,timeout=60)
        count_response.raise_for_status()
        if count_response.text.strip() != '0':
            raise ValueError('USGS empty response not corroborated by zero count')
        data={'type':'FeatureCollection','metadata':{'count':0,'status':200},'features':[]}
    else:
        data=response.json()
    features = validate_response(data,start_iso,end_iso,min_mag,allow_below_floor=True)
    if response.status_code != 204:
        # A parseable but shortened body must not authorize deletion. Check the
        # service's independent total using the identical selection bounds.
        count_params={k:v for k,v in params.items() if k not in ('format','orderby')}
        count_params['format']='text'
        count_response=requests.get(API.removesuffix('/query')+'/count',params=count_params,timeout=60)
        count_response.raise_for_status()
        expected=count_response.text.strip()
        if not expected.isdecimal() or int(expected) != len(features):
            raise ValueError('USGS query count disagrees with returned events; retain prior scope')
    # Live USGS queries can return a preferred magnitude below their requested
    # floor (observed nc21364840: requested >=4, preferred 3.38). Validate total
    # retrieval first, then impose our explicit preferred-magnitude definition.
    # Retain excluded source records for audit instead of accepting them into
    # the selected catalog or weakening the exact-scope deletion condition.
    excluded=[feature for feature in features if feature['properties']['mag'] < min_mag]
    if excluded:
        selected=[feature for feature in features if feature['properties']['mag'] >= min_mag]
        data={**data,'features':selected,
              'metadata':{**data['metadata'],'count':len(selected),'service_count':len(features)},
              '_excluded_below_floor':excluded}
    validate_response(data,start_iso,end_iso,min_mag)
    return data


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


def query_fingerprint(min_mag: float) -> str:
    """Cache identity includes selection criteria, not only requested dates."""
    if not math.isfinite(min_mag):
        raise ValueError('Minimum magnitude must be finite')
    query = dict(api=API, format='geojson', minmagnitude=float(min_mag), orderby='time-asc',
                 interval='start_inclusive_end_exclusive_millisecond', reconciliation_version=1)
    return hashlib.sha256(json.dumps(query, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def chunk_done(conn: sqlite3.Connection, start_iso: str, end_iso: str,
               min_mag: float | None = None, max_age_days: float | None = None) -> int | None:
    # Old records are inspectable for compatibility but never qualify for a new
    # parameterized fetch. Unknown historical magnitude floors require refetch.
    if min_mag is None:
        row = conn.execute('SELECT count FROM chunks WHERE start_iso=? AND end_iso=?',
                           (start_iso, end_iso)).fetchone()
    else:
        row = conn.execute(
            'SELECT count,fetched_at FROM chunks_v2 WHERE start_iso=? AND end_iso=? AND query_fingerprint=?',
            (start_iso, end_iso, query_fingerprint(min_mag))).fetchone()
    if row and min_mag is not None and max_age_days is not None:
        if max_age_days < 0 or not math.isfinite(max_age_days):
            raise ValueError('Cache age must be finite and nonnegative')
        if time.time()-row[1] >= max_age_days*86400:
            return None
    return row[0] if row else None


def record_chunk(conn, start_iso, end_iso, granularity, count, min_mag=None) -> None:
    if min_mag is None:
        conn.execute(
            'INSERT OR REPLACE INTO chunks (start_iso,end_iso,granularity,fetched_at,count) VALUES (?,?,?,?,?)',
            (start_iso,end_iso,granularity,int(time.time()),count))
    else:
        conn.execute(
            'INSERT OR REPLACE INTO chunks_v2 '
            '(start_iso,end_iso,query_fingerprint,min_magnitude,granularity,fetched_at,count) '
            'VALUES (?,?,?,?,?,?,?)',
            (start_iso,end_iso,query_fingerprint(min_mag),min_mag,granularity,int(time.time()),count))


def write_coverage(conn, db_path, min_mag):
    """Publish successful query coverage, never the date of the last earthquake."""
    rows = conn.execute(
        "SELECT start_iso FROM chunks_v2 WHERE query_fingerprint=? AND granularity='year' ORDER BY start_iso",
        (query_fingerprint(min_mag),)).fetchall()
    if not rows:
        return
    years = sorted({int(row[0][:4]) for row in rows})
    now = datetime.now(timezone.utc)
    meta = dict(schema_version=1,start_year=years[0],end_year=years[-1],
                complete_through_year=min(years[-1],now.year-1),
                gap_years=sorted(set(range(years[0],years[-1]+1))-set(years)),
                completeness='source_defined_catalog',source_url=API,
                source_version=query_fingerprint(min_mag),min_magnitude=min_mag,
                as_of=now.isoformat(),notes='Successful full-year queries at recorded magnitude floor; current year provisional. Historical cache does not imply every historical event is detected.')
    dest = Path(str(db_path)+'.coverage.json')
    temporary = dest.with_name(dest.name+'.tmp')
    temporary.write_text(json.dumps(meta,indent=2)+'\n')
    temporary.replace(dest)


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = f"{year}-{month:02d}-01T00:00:00"
    if month == 12:
        end = f"{year + 1}-01-01T00:00:00"
    else:
        end = f"{year}-{month + 1:02d}-01T00:00:00"
    return start, end


def reconcile_complete(conn, data, start_iso, end_iso, min_mag, *, granularity='interval', subchunks=()):
    """Atomic scoped replacement plus audit/cache; validation cannot delete rows.

    Absent IDs are removed only if their stored origin time and magnitude lie
    inside this complete query. Absence alone cannot distinguish withdrawal,
    a magnitude downgrade, or an origin-time revision beyond the interval.
    """
    features=validate_response(data,start_iso,end_iso,min_mag)
    excluded=data.get('_excluded_below_floor',[])
    if excluded:
        validate_response(dict(type='FeatureCollection',metadata=dict(count=len(excluded)),features=excluded),
                          start_iso,end_iso,min_mag,allow_below_floor=True)
        if any(feature['properties']['mag'] >= min_mag for feature in excluded):
            raise ValueError('Excluded response record is inside the magnitude scope')
        if {feature['id'] for feature in features} & {feature['id'] for feature in excluded}:
            raise ValueError('USGS event ID occurs in both selected and excluded responses')
    lo,hi=_bounds(start_iso,end_iso)
    digest=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    conn.execute('SAVEPOINT reconcile_quakes')
    try:
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS incoming_quake_ids (id TEXT PRIMARY KEY)')
        conn.execute('DELETE FROM incoming_quake_ids')
        conn.executemany('INSERT INTO incoming_quake_ids VALUES (?)',[(feature['id'],) for feature in features])
        cursor=conn.execute(
            'SELECT * FROM quakes WHERE time_ms>=? AND time_ms<? AND mag>=? '
            'AND NOT EXISTS (SELECT 1 FROM incoming_quake_ids incoming WHERE incoming.id=quakes.id)',
            (lo,hi,min_mag))
        names=[column[0] for column in cursor.description]
        removed=[dict(zip(names,row)) for row in cursor.fetchall()]
        audit=conn.execute(
            'INSERT INTO quake_reconciliations '
            '(reconciled_at,start_iso,end_iso,min_magnitude,query_fingerprint,incoming_count,removed_count,response_sha256) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (datetime.now(timezone.utc).isoformat(),start_iso,end_iso,min_mag,query_fingerprint(min_mag),len(features),len(removed),digest))
        conn.executemany('INSERT INTO removed_quake_records VALUES (?,?,?,?)',[
            (audit.lastrowid,row['id'],json.dumps(row,sort_keys=True,allow_nan=False),
             'Absent from complete scoped response; withdrawal or revision outside query scope') for row in removed])
        conn.executemany('INSERT INTO excluded_usgs_response_records VALUES (?,?,?,?)',[
            (audit.lastrowid,feature['id'],json.dumps(feature,sort_keys=True,allow_nan=False),
             'Complete USGS query returned preferred magnitude below requested floor; excluded after independent total-count validation')
            for feature in excluded])
        conn.execute(
            'DELETE FROM quakes WHERE time_ms>=? AND time_ms<? AND mag>=? '
            'AND NOT EXISTS (SELECT 1 FROM incoming_quake_ids incoming WHERE incoming.id=quakes.id)',
            (lo,hi,min_mag))
        upsert(conn,features)
        record_chunk(conn,start_iso,end_iso,granularity,len(features),min_mag)
        for substart,subend,count in subchunks:
            record_chunk(conn,substart,subend,'month',count,min_mag)
        conn.execute('DELETE FROM incoming_quake_ids')
        conn.execute('RELEASE SAVEPOINT reconcile_quakes')
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT reconcile_quakes')
        conn.execute('RELEASE SAVEPOINT reconcile_quakes')
        raise
    return dict(received=len(features),removed=len(removed),excluded_below_floor=len(excluded),
                reconciliation_id=audit.lastrowid,response_sha256=digest)


def fetch_interval(conn,start_iso,end_iso,min_mag):
    """Fetch and reconcile a bounded exact interval; no annual-coverage claim."""
    data=fetch_chunk(start_iso,end_iso,min_mag)
    if data is None:
        raise RuntimeError('Interval exceeds USGS result cap; use a narrower interval')
    result=reconcile_complete(conn,data,start_iso,end_iso,min_mag)
    conn.commit()
    return result


def fetch_year(conn, year: int, min_mag: float, sleep: float, force: bool,
               max_cache_age_days: float = 30) -> int:
    start=f'{year}-01-01T00:00:00'
    end=f'{year+1}-01-01T00:00:00'
    if not force and chunk_done(conn,start,end,min_mag,max_cache_age_days) is not None:
        return 0
    print(f'  {year}: ',end='',flush=True)
    data=fetch_chunk(start,end,min_mag)
    subchunks=[]
    if data is None:
        print('over cap; staging all months: ',end='',flush=True)
        features=[]
        excluded=[]
        for month in range(1,13):
            month_start,month_end=month_bounds(year,month)
            monthly=fetch_chunk(month_start,month_end,min_mag)
            if monthly is None:
                raise RuntimeError(f'Month {year}-{month:02d} exceeds result cap; narrow interval needed')
            monthly_features=validate_response(monthly,month_start,month_end,min_mag)
            features.extend(monthly_features)
            excluded.extend(monthly.get('_excluded_below_floor',[]))
            subchunks.append((month_start,month_end,len(monthly_features)))
            print(f'{month:02d}={len(monthly_features)} ',end='',flush=True)
            time.sleep(sleep)
        data=dict(type='FeatureCollection',metadata=dict(count=len(features),status=200),features=features)
        if excluded:
            data['_excluded_below_floor']=excluded
    result=reconcile_complete(conn,data,start,end,min_mag,granularity='year',subchunks=subchunks)
    conn.commit()
    print(f'{result["received"]} selected, {result["removed"]} stale scoped records removed, '
          f'{result["excluded_below_floor"]} source records below preferred-magnitude floor',flush=True)
    time.sleep(sleep)
    return result['received']


def main(argv=None) -> int:
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
    ap.add_argument('--force',action='store_true',help='Reconcile every requested year regardless of cache')
    ap.add_argument('--max-cache-age-days',type=float,default=30,
                    help='Historical query-cache lifetime; default30days, zero always refreshes')
    ap.add_argument('--start-time',help='Exact UTC interval start (inclusive); requires --end-time')
    ap.add_argument('--end-time',help='Exact UTC interval end (exclusive); requires --start-time')
    args = ap.parse_args(argv)
    if bool(args.start_time) != bool(args.end_time):
        ap.error('--start-time and --end-time must be supplied together')
    if not math.isfinite(args.max_cache_age_days) or args.max_cache_age_days < 0:
        ap.error('--max-cache-age-days must be finite and nonnegative')
    if args.start_time:
        try:
            _bounds(args.start_time,args.end_time)
        except ValueError as exc:
            ap.error(str(exc))
        if _utc(args.end_time) > datetime.now(timezone.utc):
            ap.error('Exact interval cannot end in the future')
    if args.start_year > args.end_year or args.end_year > datetime.now(timezone.utc).year:
        ap.error('Require start-year <= end-year <= current year')
    if not math.isfinite(args.min_mag):
        ap.error('Minimum magnitude must be finite')

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.commit()

    if args.start_time:
        try:
            result=fetch_interval(conn,args.start_time,args.end_time,args.min_mag)
            print(json.dumps(result,indent=2))
            return 0
        finally:
            conn.close()

    this_year = datetime.now(timezone.utc).year
    print(
        f"Fetching M>={args.min_mag} from {args.start_year} through "
        f"{args.end_year} → {args.db}"
    )
    total = 0
    for year in range(args.start_year, args.end_year + 1):
        # Always re-fetch the current year — it keeps growing
        force = args.force or year >= this_year
        total += fetch_year(conn, year, args.min_mag, args.sleep, force=force,
                            max_cache_age_days=args.max_cache_age_days)

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
    write_coverage(conn, args.db, args.min_mag)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
