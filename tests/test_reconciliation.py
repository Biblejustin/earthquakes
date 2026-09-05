"""Scope boundaries and failure atomicity for authoritative USGS refreshes."""
import json
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import Mock, patch

import fetch_quakes as q

START = '2000-01-01T00:00:00Z'
END = '2001-01-01T00:00:00Z'
LO, HI = q._bounds(START, END)


def feature(rid, moment=LO, mag=5., place='original'):
    return {'type': 'Feature', 'id': rid, 'properties': {'time': moment, 'mag': mag, 'place': place},
            'geometry': {'type': 'Point', 'coordinates': [1., 2., 3.]}}


def payload(*features):
    return {'type': 'FeatureCollection', 'metadata': {'count': len(features), 'status': 200},
            'features': list(features)}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript(q.SCHEMA)
        self.addCleanup(self.conn.close)

    def seed(self, *features):
        q.upsert(self.conn, list(features))
        self.conn.commit()

    def snapshot(self):
        return self.conn.execute('SELECT * FROM quakes ORDER BY id').fetchall()

    def test_complete_refresh_revises_and_removes_only_exact_scope(self):
        self.seed(feature('withdrawn'), feature('revised'), feature('below', mag=3.9),
                  feature('before', LO-1), feature('boundary', HI))
        result = q.reconcile_complete(self.conn, payload(feature('revised', place='corrected'),
                                                        feature('new', HI-1, 4.)), START, END, 4.)
        self.assertEqual(result['removed'], 1)
        self.assertEqual({r[0] for r in self.snapshot()}, {'revised', 'new', 'below', 'before', 'boundary'})
        self.assertEqual(self.conn.execute("SELECT place FROM quakes WHERE id='revised'").fetchone()[0], 'corrected')
        old = self.conn.execute('SELECT event_id,previous_record_json FROM removed_quake_records').fetchone()
        self.assertEqual(old[0], 'withdrawn')
        self.assertEqual(json.loads(old[1])['mag'], 5.)
        self.assertEqual(q.chunk_done(self.conn, START, END, 4.), 2)
        again = q.reconcile_complete(self.conn, payload(feature('revised', place='corrected'),
                                                       feature('new', HI-1, 4.)), START, END, 4.)
        self.assertEqual(again['removed'], 0)

    def test_authoritative_zero_removes_threshold_events_but_preserves_other_scope(self):
        self.seed(feature('threshold', mag=4.), feature('below', mag=3.99), feature('end', HI))
        q.reconcile_complete(self.conn, payload(), START, END, 4.)
        self.assertEqual({r[0] for r in self.snapshot()}, {'below', 'end'})

    def test_invalid_responses_never_mutate_catalog_audit_or_cache(self):
        self.seed(feature('existing'))
        before = self.snapshot()
        truncated = payload(feature('new')); truncated['metadata']['count'] = 2
        missing = payload(); missing.pop('metadata')
        invalid = [truncated, missing, payload(feature('repeat'), feature('repeat')),
                   payload(feature('outside', HI)), payload(feature('low', mag=3.9)),
                   payload(feature('missing-mag', mag=None))]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                q.reconcile_complete(self.conn, data, START, END, 4.)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM quake_reconciliations').fetchone()[0], 0)
            self.assertIsNone(q.chunk_done(self.conn, START, END, 4.))

    def test_insert_and_cache_failures_roll_back_deletions_and_audit(self):
        self.seed(feature('existing'))
        before = self.snapshot()
        for target in ('upsert', 'record_chunk'):
            with self.subTest(target=target), patch.object(q, target, side_effect=RuntimeError('disk issue')):
                with self.assertRaises(RuntimeError):
                    q.reconcile_complete(self.conn, payload(feature('new')), START, END, 4.)
            self.assertEqual(self.snapshot(), before)
            for table in ('quake_reconciliations', 'removed_quake_records', 'chunks_v2'):
                self.assertEqual(self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_failed_last_split_month_leaves_entire_year_and_cache_unchanged(self):
        self.seed(feature('existing'))
        before = self.snapshot()
        responses = [None] + [payload()] * 11 + [OSError('last month unavailable')]
        with patch.object(q, 'fetch_chunk', side_effect=responses):
            with self.assertRaises(OSError):
                q.fetch_year(self.conn, 2000, 4., 0, True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM chunks_v2').fetchone()[0], 0)

    def test_historical_cache_expiry_revisits_previously_complete_scope(self):
        start, end = START.removesuffix('Z'), END.removesuffix('Z')
        q.record_chunk(self.conn, start, end, 'year', 1, 4.)
        self.conn.execute('UPDATE chunks_v2 SET fetched_at=0')
        with patch.object(q, 'fetch_chunk', return_value=payload()) as fetch:
            q.fetch_year(self.conn, 2000, 4., 0, False)
        fetch.assert_called_once()


class ResponseTests(unittest.TestCase):
    def response(self, status=200, data=None, text=''):
        response = Mock(status_code=status, text=text)
        response.json.return_value = data
        return response

    def test_exact_end_is_converted_to_last_included_millisecond(self):
        with patch.object(q.requests, 'get', side_effect=[self.response(data=payload(feature('ok'))),
                                                        self.response(text='1')]) as get:
            q.fetch_chunk(START, END, 4.)
        query = get.call_args_list[0].kwargs['params']
        self.assertEqual(query['endtime'], '2000-12-31T23:59:59.999+00:00')
        count = get.call_args_list[1].kwargs['params']
        self.assertEqual(count['endtime'], query['endtime'])
        self.assertEqual(count['minmagnitude'], 4.)

    def test_parseable_short_response_rejected_against_independent_count(self):
        with patch.object(q.requests, 'get', side_effect=[self.response(data=payload()), self.response(text='1')]):
            with self.assertRaisesRegex(ValueError, 'query count'):
                q.fetch_chunk(START, END, 4.)

    def test_live_service_below_floor_preferred_magnitude_is_audited_after_count_validation(self):
        data = payload(feature('selected'), feature('downgraded', mag=3.38))
        with patch.object(q.requests, 'get', side_effect=[self.response(data=data), self.response(text='2')]):
            normalized = q.fetch_chunk(START, END, 4.)
        self.assertEqual(normalized['metadata']['count'], 1)
        self.assertEqual(normalized['metadata']['service_count'], 2)
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript(q.SCHEMA)
            q.upsert(conn, [feature('downgraded', mag=4.1), feature('untouched', mag=3.5)])
            result = q.reconcile_complete(conn, normalized, START, END, 4.)
            self.assertEqual(result['removed'], 1)
            self.assertEqual(result['excluded_below_floor'], 1)
            self.assertEqual({r[0] for r in conn.execute('SELECT id FROM quakes')}, {'selected', 'untouched'})
            record = json.loads(conn.execute('SELECT preferred_record_json FROM excluded_usgs_response_records').fetchone()[0])
            self.assertEqual(record['properties']['mag'], 3.38)
        with patch.object(q.requests, 'get', side_effect=[self.response(data=data), self.response(text='1')]):
            with self.assertRaisesRegex(ValueError, 'query count'):
                q.fetch_chunk(START, END, 4.)

    def test_empty_http_response_requires_authoritative_zero_count(self):
        for count in ('1', '', 'error'):
            with self.subTest(count=count), patch.object(q.requests, 'get', side_effect=[self.response(204), self.response(text=count)]):
                with self.assertRaises(ValueError):
                    q.fetch_chunk(START, END, 4.)
        with patch.object(q.requests, 'get', side_effect=[self.response(204), self.response(text='0')]):
            self.assertEqual(q.fetch_chunk(START, END, 4.)['features'], [])


if __name__ == '__main__':
    unittest.main()
