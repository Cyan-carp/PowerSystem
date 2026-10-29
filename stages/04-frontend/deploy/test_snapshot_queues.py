import importlib.util
from pathlib import Path
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing

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


if __name__ == '__main__':
    unittest.main()
