import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('observe', Path(__file__).with_name('observe-telemetry.py'))
observe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observe)


def sample(turn):
    return {'utc': f'2026-10-29T08:0{turn}:00Z', 'errors': {}, 'ready_http': 200,
            'metrics': {'fresh_devices': 143, 'expected_devices': 143, 'probe_age_seconds': 1,
                        'cache_sample_worst_age_seconds': 3, 'probe_pg_ok': 1, 'probe_redis_ok': 1,
                        'probe_td_ok': 1, 'maintenance': 0, 'success_total': 100 + turn},
            'pending_count': 10 - turn, 'heads': [{'device_id': 1, 'id': 100 + turn}],
            'td_latest': [{'device_id': 1, 'ts': f'2026-10-29T08:0{turn}:00Z', 'seq': turn, 'age_seconds': 1}],
            'events': {'api': {'counts': {}}},
            'energy': {'dirty_count': 1, 'days': [{'source_quality': 'provisional',
                       'newest_update': f'2026-10-29T08:0{turn}:00Z'}]}}


class ObservationTests(unittest.TestCase):
    def test_multiline_psql_json(self):
        self.assertEqual(observe.decode_rows('BEGIN\nSET\n[{"a":1},\n{"a":2}]\n42\nCOMMIT\n'), [[{'a': 1}, {'a': 2}], 42])

    def test_partial_energy_can_pass_without_claiming_complete(self):
        result = observe.summarize([sample(0), sample(1)], 300, 300)
        self.assertEqual(result['result'], 'short_observation_pass')
        self.assertFalse(result['acceptance_written'])
        self.assertFalse(result['long_term_stability_claimed'])

    def test_connected_but_stale_td_fails(self):
        rows = [sample(0), sample(1)]
        rows[1]['td_latest'][0]['age_seconds'] = 9000
        self.assertIn('td_current_sample_not_proven', observe.summarize(rows, 300, 300)['reasons'])

    def test_queue_reduction_from_quarantine_is_not_success(self):
        rows = [sample(0), sample(1)]
        rows[1]['metrics']['success_total'] = 100
        self.assertIn('business_success_not_advanced', observe.summarize(rows, 300, 300)['reasons'])

    def test_head_stall_and_energy_stall_are_separate(self):
        rows = [sample(0), sample(1)]
        rows[1]['heads'] = copy.deepcopy(rows[0]['heads'])
        rows[1]['energy'] = copy.deepcopy(rows[0]['energy'])
        reasons = observe.summarize(rows, 300, 300)['reasons']
        self.assertIn('history_head_not_advanced', reasons)
        self.assertIn('dirty_energy_not_advanced', reasons)

    def test_missing_probe_and_error_log_cannot_pass(self):
        rows = [sample(0), sample(1)]
        rows[1]['events'] = {'errors': {'emqx': 'TimeoutExpired'}}
        rows[1]['errors'] = {'pending_count': 'TimeoutExpired'}
        rows[1]['events']['api'] = {'counts': {'energy_rebuild_failed': 1}}
        reasons = observe.summarize(rows, 300, 300)['reasons']
        self.assertIn('probe_error', reasons)
        self.assertIn('log_probe_error', reasons)
        self.assertIn('runtime_error_event', reasons)

    def test_interrupt_and_single_sample_remain_incomplete(self):
        self.assertIn('observation_incomplete', observe.summarize([sample(0)], 300, 10, True)['reasons'])

    def test_fairness_includes_drained_devices_and_detects_regression(self):
        result = observe.delta_heads([{'device_id': 1, 'id': 9}, {'device_id': 2, 'id': 8}], [{'device_id': 2, 'id': 7}])
        self.assertEqual(result['advanced_or_drained'], [1])
        self.assertEqual(result['regressed'], [2])


if __name__ == '__main__':
    unittest.main()
