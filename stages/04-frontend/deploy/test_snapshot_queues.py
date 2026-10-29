import importlib.util
from pathlib import Path
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from unittest import mock

spec = importlib.util.spec_from_file_location('queue_snapshot', Path(__file__).with_name('snapshot_queues.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class QueueSnapshotTests(unittest.TestCase):
    def test_wal_snapshot_preserves_actual_rows_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'source.sqlite', Path(folder)/'copy.sqlite'
            with closing(sqlite3.connect(source)) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE queue(id INTEGER PRIMARY KEY,payload TEXT)')
                live.executemany('INSERT INTO queue VALUES(?,?)', [(i, 'fixture-'+str(i)) for i in range(2000)])
                live.commit()
                expected = live.execute('SELECT * FROM queue ORDER BY id').fetchall()
                result = module.snapshot_one(source, target, time.monotonic()+10)
                with closing(sqlite3.connect(target)) as restored:
                    self.assertEqual(expected, restored.execute('SELECT * FROM queue ORDER BY id').fetchall())
                self.assertEqual(expected, live.execute('SELECT * FROM queue ORDER BY id').fetchall())
                self.assertGreater(result['source_pages'], 0)

    def test_expired_budget_cannot_produce_a_signed_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(TimeoutError):
                module.snapshot_one(Path(folder)/'source', Path(folder)/'copy', time.monotonic()-1)
            self.assertFalse((Path(folder)/'copy').exists())

    def test_locate_follows_the_moved_data_directory_instead_of_the_leftover(self):
        # The 2026-10-29 gateway incident: /data/gateway.sqlite stayed behind while the
        # service started writing /data/fresh-20261029-r1/gateway.sqlite.
        with tempfile.TemporaryDirectory() as folder:
            root, nested = Path(folder), Path(folder)/'fresh-20261029-r1'
            nested.mkdir()
            (root/'gateway.sqlite').write_bytes(b'leftover')
            (nested/'gateway.sqlite').write_bytes(b'live')
            os.utime(root/'gateway.sqlite', (1_700_000_000, 1_700_000_000))
            os.utime(nested/'gateway.sqlite', (1_700_000_900, 1_700_000_900))
            self.assertEqual(nested/'gateway.sqlite', module.locate('gateway.sqlite', root))

    def test_locate_falls_back_to_the_volume_root_when_nothing_nested_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'simulator.sqlite').write_bytes(b'only')
            self.assertEqual(root/'simulator.sqlite', module.locate('simulator.sqlite', root))

    def test_locate_prefers_the_nested_layout_on_equal_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            root, nested = Path(folder), Path(folder)/'budget-1'
            nested.mkdir()
            for path in (root/'gateway.sqlite', nested/'gateway.sqlite'):
                path.write_bytes(b'x')
                os.utime(path, (1_700_000_000, 1_700_000_000))
            self.assertEqual(nested/'gateway.sqlite', module.locate('gateway.sqlite', root))

    def test_locate_refuses_to_snapshot_a_queue_that_is_not_there(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                module.locate('gateway.sqlite', Path(folder))

    def test_live_writer_is_read_through_the_pinned_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'live.sqlite', Path(folder)/'copy.sqlite'
            with closing(sqlite3.connect(source)) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE queue(id INTEGER PRIMARY KEY)')
                live.execute('INSERT INTO queue VALUES(1)')
                live.commit()
                # The writer is still open, so -shm exists and only mode=ro may be used.
                self.assertTrue(source.with_name('live.sqlite-shm').exists())
                module.snapshot_one(source, target, time.monotonic()+10)
            with closing(sqlite3.connect(target)) as restored:
                self.assertEqual([(1,)], restored.execute('SELECT * FROM queue').fetchall())

    def test_stopped_writer_database_without_shm_is_still_readable(self):
        # The backup pauses telemetry, so the gateway is stopped when its queue is
        # snapshotted: WAL mode, no -shm, no -wal. mode=ro refuses that file.
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'gateway.sqlite', Path(folder)/'copy.sqlite'
            with closing(sqlite3.connect(source)) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE queue(id INTEGER PRIMARY KEY,payload TEXT)')
                live.executemany('INSERT INTO queue VALUES(?,?)', [(1, 'a'), (2, 'b')])
                live.commit()
            self.assertFalse(source.with_name('gateway.sqlite-shm').exists())
            self.assertFalse(source.with_name('gateway.sqlite-wal').exists())
            module.snapshot_one(source, target, time.monotonic()+10)
            with closing(sqlite3.connect(target)) as restored:
                self.assertEqual([(1, 'a'), (2, 'b')],
                                 restored.execute('SELECT * FROM queue ORDER BY id').fetchall())

    def _fail_pinned_open_only(self):
        real_connect = sqlite3.connect

        def connect(database, *args, **kwargs):
            if 'mode=ro' in str(database) and 'immutable' not in str(database):
                raise sqlite3.OperationalError('unable to open database file')
            return real_connect(database, *args, **kwargs)
        return mock.patch.object(module.sqlite3, 'connect', connect)

    def test_pinned_open_falls_back_to_immutable_for_a_quiesced_file(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'gateway.sqlite', Path(folder)/'copy.sqlite'
            with closing(sqlite3.connect(source)) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE queue(id INTEGER PRIMARY KEY)')
                live.execute('INSERT INTO queue VALUES(7)')
                live.commit()
            with self._fail_pinned_open_only():
                module.snapshot_one(source, target, time.monotonic()+10)
            with closing(sqlite3.connect(target)) as restored:
                self.assertEqual([(7,)], restored.execute('SELECT * FROM queue').fetchall())

    def test_pinned_open_refuses_a_wal_that_would_need_recovery(self):
        # A non-empty -wal without -shm cannot be read immutably without dropping the
        # pages it still holds, so the snapshot must fail instead of losing them.
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'gateway.sqlite', Path(folder)/'copy.sqlite'
            with closing(sqlite3.connect(source)) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE queue(id INTEGER PRIMARY KEY)')
                live.commit()
            source.with_name('gateway.sqlite-wal').write_bytes(b'unrecovered-wal-frames')
            with self._fail_pinned_open_only():
                with self.assertRaises(RuntimeError) as caught:
                    module.snapshot_one(source, target, time.monotonic()+10)
            self.assertIn('queue_snapshot_wal_recovery_required', str(caught.exception))
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
