from datetime import timedelta
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from .management.commands.probe_stream_latency import cached_identities
from .stream_latency_probe import ReceiverProbe, compare, statistics, fingerprint
from .microstructure_archive import stamp
from .test_microstructure_archive import NOW, INSTRUMENT, book, trade
from .test_stream_analysis import row


class LatencyProbeTests(TestCase):
    def test_empty_latency_is_not_zero(self):
        self.assertIsNone(statistics([])['median_seconds'])

    def test_latency_buckets_and_negative_clocks(self):
        result = statistics([-.1, .1, 2., 6.])
        self.assertEqual(result['over_5s'], 1)
        self.assertEqual(result['negative_clock_difference'], 1)

    def test_unknown_instrument_and_missing_timestamp_not_invented(self):
        probe = ReceiverProbe([INSTRUMENT])
        probe.observe(NS(orderbook=NS(instrument_uid='unknown')), NOW)
        probe.observe(NS(orderbook=NS(instrument_uid=INSTRUMENT['uid'], time=None)), NOW)
        self.assertFalse(probe.observations)
        self.assertEqual(probe.counts['invalid_clock_or_payload'], 1)

    def test_receive_time_is_used_without_disk(self):
        probe = ReceiverProbe([INSTRUMENT])
        probe.observe(NS(orderbook=book(NOW), trade=trade(NOW)), NOW+timedelta(seconds=2))
        self.assertEqual(len(probe.observations), 2)
        self.assertEqual(probe.observations[0]['latency_seconds'], 2)

    def direct(self):
        return {'kind':'book', 'ticker':'SBER', 'exchange_at':stamp(NOW), 'received_at':stamp(NOW+timedelta(seconds=.1)),
                'latency_seconds':.1, 'source':'ORDERBOOK_TYPE_EXCHANGE', 'is_consistent':True,
                'fingerprint':fingerprint([[100.,80]], [[100.05,20]])}

    def test_identical_quotes_are_matched_once(self):
        archived = row(2)
        archived['exchange_at'] = stamp(NOW)
        result = compare([self.direct(),self.direct()], [archived,archived])
        self.assertEqual(result['matched_books'], 1)
        self.assertAlmostEqual(result['pairs'][0]['archive_minus_probe_seconds'], 1.9)

    def test_different_levels_or_unconfirmed_sources_do_not_match(self):
        archived = row(2)
        archived['exchange_at'] = stamp(NOW)
        archived['payload']['bids'][0][1] = 81
        self.assertEqual(compare([self.direct()], [archived])['matched_books'], 0)
        archived['payload']['bids'][0][1] = 80
        archived['is_consistent'] = 0
        self.assertEqual(compare([self.direct()], [archived])['matched_books'], 0)

    def test_cached_identity_reads_only_public_local_metadata(self):
        with TemporaryDirectory() as directory:
            session = Path(directory).resolve()
            (session/'0001-book.sqlite3').write_bytes(b'fixture')
            rows = [{'instrument_uid':'uid', 'payload':json.dumps({'lot_size':10})}]
            with patch('dashboard.management.commands.probe_stream_latency.read_rows', return_value=rows):
                self.assertEqual(cached_identities(session,['SBER']), [{'uid':'uid','lot':10,'ticker':'SBER'}])

    def test_cached_identity_missing_or_changed_refused(self):
        with TemporaryDirectory() as directory:
            session = Path(directory).resolve()
            with self.assertRaises(ValueError):
                cached_identities(session,['SBER'])
            for n in ('0001','0002'):
                (session/f'{n}-book.sqlite3').write_bytes(b'fixture')
            responses = [[{'instrument_uid':'uid', 'payload':json.dumps({'lot_size':10})}],
                         [{'instrument_uid':'changed', 'payload':json.dumps({'lot_size':10})}]]
            with patch('dashboard.management.commands.probe_stream_latency.read_rows', side_effect=responses), self.assertRaises(ValueError):
                cached_identities(session,['SBER'])
