#!/usr/bin/env python3
"""Known tail identities, strict thresholds and fail-closed matrix auditing."""

import contextlib
import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'admission_tail_audit.py'
SPEC = importlib.util.spec_from_file_location('admission_tail_audit', TOOL)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
MS = audit.MS


def write_csv(path, rows):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def callback(rid, tag, kind, submitted, start, finish, enabled=False, group=0):
    return {'request': rid, 'tag': tag, 'kind': kind, 'home': group, 'source': group,
            'chosen': group, 'hops': 0, 'reason': 1, 'migrated': 0, 'cross_numa': 0,
            'queue_before': 0, 'outstanding_before': 0, 'callback_inflight_before': 0,
            'preferred_score': '0', 'chosen_score': '0', 'worker_tid': 10 + group,
            'submitted_ns': submitted, 'start_ns': start, 'finish_ns': finish,
            'cpu_ns': 0, 'iterations': 1000 if kind == 'operator' else 0,
            'read_words': 32 if kind == 'operator' else 0,
            'write_words': 32 if kind == 'operator' else 0, 'checksum': tag,
            'selection_batch_tasks': 1, 'admission_enabled': int(enabled),
            'admission_evaluated': 0, 'admission_before': '', 'admission_after': '',
            **dict.fromkeys(audit.ADMISSION_COUNTS, 0)}


def synthetic_matrix(root):
    """Two synthetic requests per run, complete graph/receipt contracts.

    Timestamp intervals exercise analysis only; they are not physical worker
    schedules or observed benchmark measurements.
    """
    options = {**audit.matrix.FIXED, 'cpus': '0;1', 'threads': '1', 'requests': '2',
               'iterations': '1000', 'accesses': '32', 'qps': '10'}
    plan = audit.matrix.make_plan(Path(sys.executable), root, options)
    metadata = {'schema': audit.matrix.SCHEMA, 'development': True,
                'binary': sys.executable, 'binary_sha256': 'synthetic-binary',
                'sources_sha256': {'synthetic-source': 'identity'},
                'fixed_options': options, 'git_head': 'synthetic-test-fixture',
                'plan_sha256': audit.matrix.json_digest(plan)}
    (root / 'metadata.json').write_text(json.dumps(metadata))
    (root / 'plan.json').write_text(json.dumps(plan))
    for entry in plan:
        path = Path(entry['directory'])
        path.mkdir()
        origin, tasks, requests = 1_000_000_000, [], []
        depths = list(map(int, entry['options']['depths'].split(',')))
        enabled = entry['target_limit'] > 0
        for rid in range(2):
            scheduled = origin + rid * 100 * MS
            attempted = scheduled + (2 if rid == 0 else 0) * MS
            root_task = callback(rid, 0, 'init', attempted, attempted + MS,
                                 attempted + 2 * MS, enabled, rid)
            tasks.append(root_task)
            offset, parent = 1, root_task
            for width, depth in zip((80, 4), depths):
                previous = [parent] * width
                for stage in range(depth):
                    current = []
                    for branch in range(width):
                        tag = offset + stage * width + branch
                        submitted = previous[branch]['start_ns'] + MS // 2
                        # Known strict boundaries and rare long wait in request 0.
                        wait = {1: MS, 2: 5 * MS, 3: 20 * MS, 4: 21 * MS}.get(tag, MS // 100)
                        if rid == 1:
                            wait = MS // 100
                        task = callback(rid, tag, 'operator', submitted, submitted + wait,
                                        submitted + wait + MS, enabled, rid)
                        tasks.append(task)
                        current.append(task)
                    previous = current
                submitted = max(row['finish_ns'] for row in previous) - MS // 10
                parent = callback(rid, offset + width * depth, 'join', submitted,
                                  submitted + MS // 10, submitted + 11 * MS // 10, enabled, rid)
                tasks.append(parent)
                offset += width * depth + 1
            requests.append({'request': rid, 'scheduled_ns': scheduled, 'submitted_ns': attempted,
                             'finished_ns': parent['finish_ns'], 'status': 'completed',
                             'concurrent': 1, 'checksum': rid + 1})
        arrival = checksum = 0
        for row in requests:
            arrival = audit.report.mix(arrival ^ (row['scheduled_ns'] - origin))
            checksum = audit.report.mix(checksum ^ row['checksum'])
        stats = dict.fromkeys(('selections', 'evaluated_selections', 'candidate_checks',
                               'candidate_score_invalid', 'candidate_invalid', 'candidate_over_limit',
                               'candidate_insufficient_gain', 'return_home_rejected', 'no_eligible_target'), 0)
        reasons = dict.fromkeys(audit.matrix.DECISION_REASONS, 0)
        if enabled:
            stats['selections'] = len(tasks)
            reasons['BelowBusyThreshold'] = len(tasks)
        admission = {'enabled': enabled, 'limit': entry['target_limit'], 'stats': stats,
                     'decision_reasons': reasons, 'policy': 'outstanding-after' if enabled else 'disabled',
                     'units': 'outstanding_reserved_incoming_tasks_per_capacity' if enabled else 'none'}
        summary = {'schema': 'affinity-v2', 'origin_ns': origin, 'seed': entry['seed'],
                   'work_signature': '{}', 'policy': 'outstanding', 'continuation': 'parent',
                   'affinity': 'cluster', 'model': 'branch-rw', 'requests': 2,
                   'nodes_per_request': 171, 'widths': [80, 4], 'depths': depths,
                   'groups': [{'cpus': [i], 'capacity': 1, 'numa': 0} for i in range(2)],
                   'threads_per_pool': 1, 'trace': True, 'arrival_hash': str(arrival),
                   'checksum': str(checksum), 'completed': 2, 'failed_or_rejected': 0,
                   'observed_tasks': len(tasks), 'migrations': 0, 'cross_numa': 0,
                   'options': entry['options'], 'process_cpu_s': 0, 'peak_inflight': 1,
                   'routing': {'target_limit': entry['target_limit']}, 'balance_at': 'all',
                   'target_admission': admission, 'arrival': 'regular'}
        receipt = {'schema': audit.matrix.SCHEMA + '-receipt', 'command': entry['command'],
                   'exit_code': 0, 'timed_out': False,
                   **{key: metadata[key] for key in ('binary_sha256', 'sources_sha256')},
                   **{'after_' + key: metadata[key] for key in ('binary_sha256', 'sources_sha256')}}
        (path / 'summary.json').write_text(json.dumps(summary))
        (path / 'execution.json').write_text(json.dumps(receipt))
        write_csv(path / 'requests.csv', requests)
        write_csv(path / 'tasks.csv', tasks)
    return metadata, plan


class TailAuditTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='admission-tail-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.metadata, self.plan = synthetic_matrix(self.root)

    def test_complete_matrix_thirteen_lines_development_and_strict_thresholds(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = audit.main([str(self.root), '--warmup-seconds', '0', '--window-seconds', 'all'])
        self.assertEqual(status, 0)
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(rows), 13)
        self.assertEqual(rows[0]['validated_pairs'], 8)
        self.assertTrue(all(row['development'] for row in rows))
        for row in rows[1:]:
            self.assertEqual(row['cohort_requests'], 2)
            self.assertEqual(row['callback_waits']['all']['callbacks'], 342)
            thresholds = row['callback_waits']['all']['thresholds']
            self.assertEqual([item['callbacks'] for item in thresholds], [3, 2, 1])
            self.assertEqual([item['wait_ms'] for item in thresholds], [46, 41, 21])
            for index, item in enumerate(thresholds):
                self.assertEqual(item['callbacks'], sum(role['thresholds'][index]['callbacks']
                                                       for role in row['callback_waits']['by_role'].values()))
            self.assertEqual([item['tag'] for item in row['top_callbacks_by_wait']], [4, 3, 2])
            self.assertEqual(row['top_requests_by_planned_latency'][0]['request'], 0)
            self.assertEqual(row['top_requests_by_planned_latency'][0]['attempt_lag_ms'], 2)
            self.assertEqual(row['top_requests_by_planned_latency'][0]['max_wait_callback']['tag'], 4)
            self.assertEqual(row['max_wait_callback_by_role']['ordinary successors']['role'], 'ordinary successors')
            self.assertEqual(row['max_wait_callback_by_role']['first-wave stage0']['tag'], 4)
            request = row['top_requests_by_planned_latency'][0]
            self.assertAlmostEqual(request['planned_latency_ms'], request['attempt_lag_ms'] + request['after_attempt_ms'])
            self.assertNotIn('preferred', row['top_callbacks_by_wait'][0])
            self.assertEqual(row['top_callbacks_by_wait'][0]['cpu_ms'], 0)

    def test_cohort_keeps_late_callbacks_and_omits_other_request_from_top(self):
        rows = audit.audit_matrix(self.root, .1, .001, 1)
        for row in rows[1:]:
            self.assertEqual(row['cohort_requests'], 1)
            self.assertEqual(row['callback_waits']['all']['callbacks'], 171)
            self.assertEqual(row['top_requests_by_planned_latency'][0]['request'], 1)
            self.assertEqual(row['top_callbacks_by_wait'][0]['request'], 1)
            self.assertGreater(row['top_requests_by_planned_latency'][0]['finished_ms_from_origin'], 101)

    def test_role_map_preserves_wave_stage_branch(self):
        tags = audit.role_map({'widths': [2, 1], 'depths': [2, 2], 'nodes_per_request': 9})
        self.assertEqual(tags[3], {'role': 'ordinary successors', 'wave': 0, 'stage': 1, 'branch': 0})
        self.assertEqual(tags[6]['role'], 'later-wave stage0')
        self.assertEqual(tags[7]['stage'], 1)
        self.assertEqual(tags[8]['role'], 'join')

    def test_overlap_uses_union_and_whole_run_other_requests_not_cpu(self):
        waiting = callback(2, 4, 'operator', 0, 10 * MS, 11 * MS)
        others = [callback(0, 1, 'operator', 0, MS, 8 * MS),
                  callback(1, 2, 'operator', 0, 3 * MS, 9 * MS),
                  callback(2, 3, 'operator', 0, 9 * MS, 10 * MS),
                  callback(3, 5, 'operator', 0, 10 * MS, 12 * MS),
                  callback(4, 6, 'operator', 0, MS, 9 * MS, group=1)]
        tags = {row['tag']: {'role': 'ordinary successors'} for row in [waiting, *others]}
        result = audit.wait_overlap(waiting, [waiting, *others], tags)
        self.assertEqual(result['overlapping_callbacks'], 3)
        self.assertEqual(result['same_request_callbacks'], 1)
        self.assertEqual(result['other_request_callbacks'], 2)
        self.assertEqual(result['covered_union_ms'], 9)
        self.assertEqual(result['callback_wall_sum_ms'], 14)
        self.assertEqual(result['max_overlapping_callback_wall_intervals'], 2)
        self.assertTrue(all(row['cpu_ns'] == 0 for row in others))
        waiting['submitted_ns'] = waiting['start_ns']
        self.assertEqual(audit.wait_overlap(waiting, others, tags)['overlapping_callbacks'], 0)

    def test_last_run_bad_receipt_prints_no_partial_results(self):
        path = Path(self.plan[-1]['directory']) / 'execution.json'
        receipt = json.loads(path.read_text())
        receipt['exit_code'] = 9
        path.write_text(json.dumps(receipt))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = audit.main([str(self.root), '--warmup-seconds', '0', '--window-seconds', 'all'])
        self.assertEqual(status, 2)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('receipt', err.getvalue())

    def test_missing_matrix_cell_and_changed_build_or_work_pair_are_rejected(self):
        plan_path = self.root / 'plan.json'
        plan_path.write_text(json.dumps(self.plan[:-1]))
        with self.assertRaisesRegex(ValueError, 'twelve-run'):
            audit.audit_matrix(self.root, 0, None)
        plan_path.write_text(json.dumps(self.plan))
        path = Path(self.plan[0]['directory']) / 'tasks.csv'
        rows = audit.report.read_csv(path, ('iterations',))
        rows[1]['iterations'] = '1001'
        write_csv(path, rows)
        with self.assertRaisesRegex(ValueError, 'resources/build/work/checksum/arrival'):
            audit.audit_matrix(self.root, 0, None)

    def test_trace_diagnostic_corruption_and_unevaluated_zero_are_rejected(self):
        for field, value, match in (('candidate_checks', '1', 'disabled admission'),
                                     ('admission_before', '0', 'must be blank'),
                                     ('selection_batch_tasks', '2', 'chunk=1')):
            with self.subTest(field=field):
                row = callback(0, 1, 'operator', 0, MS, 2 * MS)
                row[field] = value
                with self.assertRaisesRegex(ValueError, match):
                    audit.admission_fields(row, 0)
        entry = next(row for row in self.plan if row['target_limit'] > 0)
        path = Path(entry['directory']) / 'tasks.csv'
        rows = audit.report.read_csv(path, ('candidate_checks',))
        rows[1]['candidate_checks'] = '1'
        write_csv(path, rows)
        with self.assertRaisesRegex(ValueError, 'trace/admission summary'):
            audit.audit_matrix(self.root, 0, None)

    def test_selected_admission_projection_requires_valid_measured_fields(self):
        row = callback(0, 1, 'operator', 0, MS, 2 * MS, enabled=True)
        row.update(chosen=1, migrated=1, reason=3, admission_evaluated=1,
                   admission_before='0.5', admission_after='1', candidate_checks=1)
        self.assertEqual(audit.admission_fields(row, 1)['admission_after'], 1)
        for value in ('', 'nan', 'inf', '-1', '1.01'):
            changed = copy.deepcopy(row)
            changed['admission_after'] = value
            with self.assertRaises(ValueError):
                audit.admission_fields(changed, 1)

    def test_target_cannot_use_development_window_and_invalid_inputs_fail(self):
        for warmup, window, top in ((-1, 16, 3), (0, 0, 3), (0, float('nan'), 3), (0, None, 0), (0, None, 101)):
            with self.assertRaises(ValueError):
                audit.audit_matrix(self.root, warmup, window, top)
        metadata = copy.deepcopy(self.metadata)
        metadata['development'] = 'false'
        (self.root / 'metadata.json').write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'explicit boolean'):
            audit.audit_matrix(self.root, 0, None)
        metadata['development'] = False
        # A falsified target label is caught even before its non-target window:
        # the original matrix validator rejects small development resources.
        (self.root / 'metadata.json').write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'fixed experiment contract'):
            audit.audit_matrix(self.root, 0, None)


if __name__ == '__main__':
    unittest.main()
