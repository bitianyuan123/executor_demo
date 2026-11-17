#!/usr/bin/env python3
"""Check CPU temporal aggregation and invalid-window handling, not performance."""
import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest

MODULE_PATH = Path(__file__).resolve().parents[3] / 'reports/final_review/cpu_window_metrics.py'
SPEC = importlib.util.spec_from_file_location('cpu_window_metrics', MODULE_PATH)
METRICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METRICS)


def rows_for(start, end, utilizations):
    return [dict(relative_start_ms=str(start), relative_end_ms=str(end), cluster=str(cluster),
                 valid='1', util_percent=str(util), busy_cpu_equivalent=str(util / 25),
                 process_cpu_equivalent='2.0', sample_lag_ms='0.1')
            for cluster, util in enumerate(utilizations)]


def derive(rows):
    return METRICS.derive(rows, cpu_count_per_cluster=4, numa_groups=((0, 1),))


class CPUWindowMetricsTest(unittest.TestCase):
    def test_unequal_windows_use_elapsed_time_not_row_average(self):
        rows = rows_for(0, 100, [100, 0]) + rows_for(100, 400, [0, 0])
        result = derive(rows)
        self.assertAlmostEqual(result['valid_seconds'], .4)
        self.assertAlmostEqual(result['global_mean_percent'], 12.5)
        self.assertAlmostEqual(result['global_mean_cpu_equiv'], 1)
        self.assertAlmostEqual(result['max_cluster_util_percent']['mean'], 25)
        self.assertAlmostEqual(result['mean_local_span_pp'], 25)
        self.assertEqual(result['max_cluster_util_percent']['p95'], 100)
        self.assertEqual(result['interval_ms']['min'], 100)
        self.assertEqual(result['interval_ms']['max'], 300)

    def test_missing_cluster_and_invalid_flag_never_fill_zero(self):
        valid = rows_for(0, 100, [50, 50])
        missing = rows_for(100, 200, [0])
        invalid = rows_for(200, 300, [0, 0])
        invalid[0]['valid'] = '0'
        result = derive(valid + missing + invalid)
        self.assertEqual(result['valid_window_count'], 1)
        self.assertEqual(result['invalid_window_count'], 2)
        self.assertAlmostEqual(result['global_mean_percent'], 50)
        self.assertAlmostEqual(result['valid_seconds'], .1)
        self.assertAlmostEqual(result['observed_seconds'], .3)

    def test_duplicate_and_blank_cpu_value_invalidate_entire_window(self):
        duplicate = rows_for(0, 100, [50, 50])
        duplicate.append(dict(duplicate[0]))
        empty = rows_for(100, 200, [50, 50])
        empty[1]['busy_cpu_equivalent'] = ''
        result = derive(duplicate + empty)
        self.assertEqual(result['invalid_window_count'], 2)
        self.assertEqual(result['valid_window_count'], 0)
        self.assertIsNone(result['global_mean_percent'])
        self.assertIsNone(result['max_cluster_util_percent']['p99'])

    def test_overlapping_windows_rejected(self):
        with self.assertRaisesRegex(ValueError, 'overlap'):
            derive(rows_for(0, 100, [0, 0]) + rows_for(50, 150, [0, 0]))

    def test_load_rounds_preserves_empty_cell_and_valid_zero(self):
        rows = rows_for(0, 100, [50, 50])
        rows[0].update(round_id='a', valid='0', process_cpu_equivalent='')
        rows[1].update(round_id='b')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'windows.csv'
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            result = METRICS.load_rounds(path)
        self.assertEqual(set(result), {'a', 'b'})
        self.assertEqual(result['a'][0]['valid'], '0')
        self.assertEqual(result['a'][0]['process_cpu_equivalent'], '')


if __name__ == '__main__':
    unittest.main()
