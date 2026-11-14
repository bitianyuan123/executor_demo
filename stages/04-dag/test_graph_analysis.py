#!/usr/bin/env python3
"""Independent arithmetic fixtures for DAG path decomposition and invalid dependency rejection."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from analyze_graph import analyze


class GraphAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        (self.path / 'metadata.json').write_text(json.dumps({'schema_version': 2, 'phase': 'completed',
                                                          'config': {'task_log': True, 'graph': 'chains', 'chain_length': 1}}))
        (self.path / 'summary.json').write_text(json.dumps({'conservation_ok': True, 'io_failed': False, 'offered': 1, 'task_rows': 3}))
        self.write('requests.csv', [{'request_id': '0', 'status': 'completed', 'cpu_nodes': '3', 'operator_nodes': '2',
                                    'fanout': '2', 'waves': '1', 'scheduled_ns': '0', 'finish_ns': '155000'}])
        self.tasks = [self.task(0, 10, 20, 100, 20), self.task(1, 10, 25, 80, 30), self.task(2, 110, 130, 150, 15, '0;1')]
        self.write('tasks.csv', self.tasks)

    def write(self, name, rows):
        with (self.path / name).open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def task(self, node, submit, start, finish, cpu, predecessors=''):
        return {'request_id': '0', 'node_id': str(node), 'stage': '0', 'status': 'completed',
                'kind': 'join' if node == 2 else 'operator', 'predecessors': predecessors,
                'submit_ns': str(submit * 1000), 'start_ns': str(start * 1000), 'finish_ns': str(finish * 1000),
                'cpu_us': str(cpu), 'wall_us': str(finish - start), 'enqueue_us': str(start - submit)}

    def test_latest_predecessor_and_exact_path_sum(self):
        result = analyze(self.path)['requests'][0]
        self.assertEqual(result['path'], ['0:0', '2:0'])
        self.assertAlmostEqual(result['latency_ms'], .155)
        self.assertAlmostEqual(result['path_dispatch_rpc_ms'], .020)
        self.assertAlmostEqual(result['path_submit_to_start_ms'], .030)
        self.assertAlmostEqual(result['path_execution_wall_ms'], .100)
        self.assertAlmostEqual(result['request_tail_ms'], .005)
        self.assertAlmostEqual(result['parallel_sum_execution_wall_ms'], .155)
        self.assertAlmostEqual(result['parallel_sum_submit_to_start_ms'], .045)

    def test_rpc_target_delay_cannot_be_shortened(self):
        for row in self.tasks:
            row['rpc_delay_target_us'] = '0'
        continuation = dict(self.tasks[0], stage='1', predecessors='0',
                            submit_ns='110000', start_ns='115000', finish_ns='120000',
                            rpc_delay_target_us='20')
        self.tasks.insert(1, continuation)
        self.write('tasks.csv', self.tasks)
        summary_path = self.path / 'summary.json'
        summary = json.loads(summary_path.read_text())
        summary['task_rows'] = 4
        summary_path.write_text(json.dumps(summary))
        with self.assertRaisesRegex(ValueError, 'RPC continuation'):
            analyze(self.path)

    def test_dependency_violation_is_rejected(self):
        self.tasks[-1]['submit_ns'] = '70000'
        self.write('tasks.csv', self.tasks)
        with self.assertRaisesRegex(ValueError, 'before its predecessor'):
            analyze(self.path)

    def test_duplicate_stage_is_rejected(self):
        self.tasks[-1] = self.tasks[0].copy()
        self.write('tasks.csv', self.tasks)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            analyze(self.path)

    def test_missing_dependency_cannot_self_validate(self):
        self.tasks[-1]['predecessors'] = '0'
        self.write('tasks.csv', self.tasks)
        with self.assertRaisesRegex(ValueError, 'configured graph'):
            analyze(self.path)

    def test_off_path_timestamp_violation_is_rejected(self):
        self.tasks[1]['start_ns'] = '90000'
        self.write('tasks.csv', self.tasks)
        with self.assertRaisesRegex(ValueError, 'timestamps'):
            analyze(self.path)

    def test_orphan_task_is_rejected(self):
        self.tasks[1]['request_id'] = '9'
        self.write('tasks.csv', self.tasks)
        with self.assertRaisesRegex(ValueError, 'unknown request'):
            analyze(self.path)


if __name__ == '__main__':
    unittest.main()
