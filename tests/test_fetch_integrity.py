"""Offline regression tests for cache selection and cross-source duplication."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import fetch_quakes as q
import fetch_significant as s


def row(rid='ngdc_1', source='ngdc', year=2000):
    return (rid, 946684800000, year, 1, 1, 7.0, 1.0, 2.0, 'Example', 10, 0., source)


class QueryCacheTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.executescript(q.SCHEMA)
        self.addCleanup(self.con.close)

    def test_legacy_date_only_cache_does_not_skip_different_query(self):
        q.record_chunk(self.con,'2000-01-01T00:00:00','2001-01-01T00:00:00','year',2)
        with patch.object(q,'fetch_chunk',return_value={'type':'FeatureCollection','metadata':{'count':0},'features':[]}) as fetch:
            q.fetch_year(self.con,2000,4.,0,False)
        fetch.assert_called_once()
        self.assertEqual(q.chunk_done(self.con,'2000-01-01T00:00:00','2001-01-01T00:00:00',4.),0)

    def test_cache_isolated_by_magnitude_and_exact_repeats_reused(self):
        with patch.object(q,'fetch_chunk',return_value={'type':'FeatureCollection','metadata':{'count':0},'features':[]}) as fetch:
            q.fetch_year(self.con,2000,6.5,0,False)
            q.fetch_year(self.con,2000,6.5,0,False)
            self.assertEqual(fetch.call_count,1)
            q.fetch_year(self.con,2000,4.,0,False)
            self.assertEqual(fetch.call_count,2)
            q.fetch_year(self.con,2000,6.5,0,True)
            self.assertEqual(fetch.call_count,3)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM chunks_v2').fetchone()[0],2)

    def test_split_month_cache_keeps_query_identity(self):
        with patch.object(q,'fetch_chunk',side_effect=[None]+[{'type':'FeatureCollection','metadata':{'count':0},'features':[]}]*12):
            q.fetch_year(self.con,2000,4.,0,False)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM chunks_v2').fetchone()[0],13)
        self.assertIsNone(q.chunk_done(self.con,'2000-01-01T00:00:00','2001-01-01T00:00:00',6.5))

    def test_malformed_response_not_interpreted_as_empty_catalogue(self):
        response = Mock(status_code=200)
        response.json.return_value = {'error':'temporarily unavailable'}
        with patch.object(q.requests,'get',return_value=response):
            with self.assertRaises(ValueError):
                q.fetch_year(self.con,2000,4.,0,False)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM chunks_v2').fetchone()[0],0)

    def test_coverage_uses_successful_query_bounds_and_gaps(self):
        for year in [2000,2002]:
            q.record_chunk(self.con,f'{year}-01-01T00:00:00',f'{year+1}-01-01T00:00:00','year',0,6.5)
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'empty-but-covered.sqlite'
            q.write_coverage(self.con,db,6.5)
            meta=json.loads(Path(str(db)+'.coverage.json').read_text())
        self.assertEqual((meta['start_year'],meta['end_year']),(2000,2002))
        self.assertEqual(meta['gap_years'],[2001])
        self.assertEqual(meta['min_magnitude'],6.5)


class SignificantSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:')
        self.con.executescript(s.SCHEMA)
        self.addCleanup(self.con.close)

    def insert(self, rows):
        self.con.executemany('INSERT INTO significant_quakes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows)
        self.con.commit()

    def snapshot(self):
        return self.con.execute('SELECT * FROM significant_quakes ORDER BY id').fetchall()

    def test_failed_live_refresh_retains_exact_snapshot_and_never_loads_fallback(self):
        self.insert([row()]); before=self.snapshot()
        with patch.object(s,'fetch_ngdc_rows',side_effect=OSError('offline')), \
             patch.object(s,'fetch_mirror_rows') as mirror, patch.object(s,'fetch_local_recent') as local:
            result=s.refresh_significant(self.con)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(result['status'],'retained_stale')
        mirror.assert_not_called();local.assert_not_called()

    def test_shrink_guard_cannot_fall_through_to_append_duplicates(self):
        self.insert([row(f'ngdc_{i}') for i in range(100)]);before=self.snapshot()
        with patch.object(s,'fetch_ngdc_rows',return_value=[row(f'ngdc_{i}') for i in range(90)]), \
             patch.object(s,'fetch_mirror_rows') as mirror:
            result=s.refresh_significant(self.con)
        self.assertEqual(result['status'],'retained_stale')
        self.assertEqual(self.snapshot(),before)
        mirror.assert_not_called()

    def test_success_replaces_old_source_instead_of_appending(self):
        self.insert([row('mirror_1','noaa_mirror_2017')])
        with patch.object(s,'fetch_ngdc_rows',return_value=[row()]):
            result=s.refresh_significant(self.con)
        self.assertEqual(result['status'],'fresh')
        self.assertEqual(self.snapshot(),[row()])

    def test_empty_bootstrap_uses_one_fallback_source(self):
        with patch.object(s,'fetch_ngdc_rows',side_effect=OSError('offline')), \
             patch.object(s,'fetch_mirror_rows',return_value=[row('mirror_1','noaa_mirror_2017')]), \
             patch.object(s,'fetch_local_recent') as local:
            result=s.refresh_significant(self.con)
        self.assertEqual(result['status'],'degraded_bootstrap')
        self.assertEqual(len(self.snapshot()),1)
        local.assert_not_called()

    def test_failed_insert_rolls_back_replacement(self):
        self.insert([row()]);before=self.snapshot()
        with patch.object(s,'fetch_ngdc_rows',return_value=[row('ngdc_2')[:-1]]):
            with self.assertRaises(sqlite3.ProgrammingError):
                s.refresh_significant(self.con)
        self.assertEqual(self.snapshot(),before)

    def test_explicit_mirror_mode_does_not_fall_back_to_local(self):
        with patch.object(s,'fetch_mirror_rows',side_effect=OSError('offline')), \
             patch.object(s,'fetch_local_recent') as local:
            result=s.refresh_significant(self.con,mirror_only=True)
        self.assertEqual(result['status'],'unavailable')
        local.assert_not_called()

    def test_missing_day_does_not_manufacture_exact_timestamp(self):
        self.assertIsNone(s._to_time_ms(2000,1,None,None,None,None))
        self.assertIsNone(s._to_time_ms(2000,None,None,None,None,None))
        self.assertEqual(s._to_time_ms(2000,1,1,None,None,None),946684800000)

    def test_narrowed_query_does_not_erase_outside_events(self):
        self.insert([row(year=1900)]);before=self.snapshot()
        with patch.object(s,'fetch_ngdc_rows',return_value=[row('new',year=2000)]):
            result=s.refresh_significant(self.con,2000,2020)
        self.assertEqual(result['status'],'retained_stale')
        self.assertEqual(self.snapshot(),before)


if __name__ == '__main__':
    unittest.main()
