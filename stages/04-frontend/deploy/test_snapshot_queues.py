import importlib.util
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
