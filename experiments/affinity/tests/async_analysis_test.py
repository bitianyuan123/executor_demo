#!/usr/bin/env python3
"""Versioned async identities, source timelines and failures; synthetic files only."""

import contextlib
import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location('async_test_' + name, ROOT / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


report, flow, diagnose = (module(name) for name in ('report', 'flow_report', 'diagnose'))
MS = 1_000_000


def write_csv(path, rows, fields=None):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def signatures(summary):
    summary['work_signature'] = json.dumps(summary['work_contract'], sort_keys=True)
    summary['fixed_work_signature'] = json.dumps({
        key: value for key, value in summary['work_contract'].items()
        if key not in ('arrival', 'qps', 'burst_size', 'burst_spacing_us',
                       'sync_block_percent', 'sync_block_us', 'async_contract')}, sort_keys=True)


def write_fixture(path, *, percent=100, delay_us=0, align_us=0, trace=True, policy='home'):
    path.mkdir()
    kinds = ('init', 'operator', 'operator', 'operator', 'operator',
             'join', 'operator', 'operator', 'join')
    selected = [{tag for tag, kind in enumerate(kinds) if kind == 'operator'
                 and report.mix(421 ^ report.mix(request) ^ report.mix(tag) ^ 0xa510c0de) % 100 < percent}
                for request in range(3)]
    contract = {'percent': percent, 'delay_us': delay_us, 'align_us': align_us,
                'capacity': 128, 'selection': 'splitmix64-v1'}
    work = {'seed': 421, 'requests': 3, 'qps': 10, 'arrival': 'regular', 'widths': [2, 1],
            'depths': [2, 2], 'model': 'branch-rw', 'iterations': 10, 'accesses': 2,
            'jitter': 0, 'long_every': 0, 'long_factor': 4, 'shared_bytes': 0,
            'branch_bytes': 64, 'work_scale_scope': 'task',
            'sync_block_percent': 0, 'sync_block_us': 0}
    if percent:
        work['async_contract'] = contract
    summary = {'schema': report.ASYNC_SCHEMA if percent else 'affinity-v2', 'origin_ns': 1000 * MS,
               'seed': 421, 'policy': policy, 'continuation': 'parent', 'affinity': 'cluster',
               'model': 'branch-rw', 'requests': 3, 'nodes_per_request': 9, 'widths': [2, 1],
               'depths': [2, 2], 'groups': [{'cpus': [0], 'capacity': 1, 'numa': 0},
                                         {'cpus': [1], 'capacity': 1, 'numa': 0}],
               'threads_per_pool': 2, 'trace': trace, 'completed': 3, 'failed_or_rejected': 0,
               'observed_tasks': 0, 'migrations': 0, 'cross_numa': 0,
               'options': {'home-policy': 'rr'}, 'process_cpu_s': .2, 'peak_inflight': 1,
               'qps': 10, 'arrival': 'regular', 'work_contract': work,
               'planned_total_iterations': 180, 'planned_total_accesses': 36}
    rows, tasks, events = [], [], []
    arrival_hash = checksum = 0
    for request in range(3):
        scheduled = summary['origin_ns'] + request * 100 * MS
        attempted = scheduled + MS
        rows.append({'request': request, 'scheduled_ns': scheduled, 'submitted_ns': attempted,
                     'finished_ns': scheduled + 80 * MS, 'status': 'completed',
                     'concurrent': 1, 'checksum': request + 101})
        arrival_hash = report.mix(arrival_hash ^ (scheduled - summary['origin_ns']))
        checksum = report.mix(checksum ^ (request + 101))
        for tag, kind in enumerate(kinds):
            submitted = attempted + tag * MS
            task = {'request': request, 'tag': tag, 'kind': kind, 'home': 0,
                    'source': 0, 'chosen': 0, 'hops': 0, 'reason': 0, 'migrated': 0,
                    'cross_numa': 0, 'queue_before': 0, 'outstanding_before': 1,
                    'callback_inflight_before': 0, 'submitted_ns': submitted,
                    'start_ns': submitted + MS, 'finish_ns': submitted + 5 * MS,
                    'cpu_ns': MS, 'iterations': 10 if kind == 'operator' else 0,
                    'read_words': 2 if kind == 'operator' else 0,
                    'write_words': 2 if kind == 'operator' else 0,
                    'checksum': tag + 1, 'logical_tag': tag}
            tasks.append(task)
            if tag not in selected[request]:
                continue
            issue = task['start_ns'] + MS
            due = issue + delay_us * 1000
            if align_us:
                grid = align_us * 1000
                due = summary['origin_ns'] + ((due - summary['origin_ns'] + grid - 1) // grid) * grid
            fired = due + MS // 2
            resume = {**task, 'tag': 9 + tag, 'kind': 'rpc_resume', 'logical_tag': tag,
                      'submitted_ns': fired + MS // 4, 'start_ns': fired + MS // 2,
                      'finish_ns': fired + 3 * MS // 4, 'cpu_ns': MS // 4,
                      'iterations': 0, 'read_words': 0, 'write_words': 0, 'checksum': 0}
            tasks.append(resume)
            events.append({'request': request, 'logical_tag': tag, 'issue_ns': issue, 'due_ns': due,
                           'fired_ns': fired, 'terminal_ns': fired + MS, 'status': 'resumed'})
    if percent:
        total = sum(map(len, selected))
        summary.update(async_contract=contract, task_slots_per_request=18,
                       planned_resume_counts=list(map(len, selected)), planned_resumes=total,
                       async_source={'registered': total, 'fired': total, 'canceled': 0,
                                     'rejected': 0, 'pending': 0, 'peak_pending': total,
                                     'callback_failures': 0, 'resume_succeeded': total, 'resume_failed': 0,
                                     'registration_rejected': 0, 'helper_cpu_s': .002, 'helper_cpu_valid': True})
    summary.update(arrival_hash=str(arrival_hash), checksum=str(checksum),
                   observed_tasks=len(tasks) if trace else 0)
    signatures(summary)
    save_fixture(path, summary, rows, tasks, events)
    return summary, rows, tasks, events


def save_fixture(path, summary, rows, tasks, events):
    (path / 'summary.json').write_text(json.dumps(summary))
    write_csv(path / 'requests.csv', rows)
    if summary['trace']:
        write_csv(path / 'tasks.csv', tasks)
        if summary['schema'] == report.ASYNC_SCHEMA:
            fields = ('request', 'logical_tag', 'issue_ns', 'due_ns', 'fired_ns', 'terminal_ns', 'status')
            write_csv(path / 'async.csv', events, fields)


class AsyncAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='affinity-async-analysis-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self, name='async', **kwargs):
        path = self.root / name
        return path, write_fixture(path, **kwargs)

    def test_sparse_selection_uses_logical_operator_not_slot_order(self):
        path, _ = self.fixture(percent=50)
        run = report.load_run(path)
        self.assertEqual(run['selected_resumes'], [{1, 6, 7}, {1, 2, 3, 4, 7}, {1, 2, 3, 4, 6}])
        self.assertEqual(len(run['async_events']), 13)
        self.assertEqual(len(run['tasks']), 27 + 13)
        self.assertEqual(run['metrics']['operator_cpu_per_request_ms'], 6)
        self.assertTrue(run['valid_for_comparison'])

    def test_zero_delay_allows_resume_before_operator_return_and_late_ack(self):
        path, (summary, rows, tasks, events) = self.fixture()
        first = events[0]
        original = next(row for row in tasks if row['request'] == 0 and row['tag'] == first['logical_tag'])
        resume = next(row for row in tasks if row['request'] == 0 and row['tag'] == 9 + first['logical_tag'])
        self.assertLess(resume['finish_ns'], original['finish_ns'])
        first['terminal_ns'] = rows[0]['finished_ns'] + MS
        save_fixture(path, summary, rows, tasks, events)
        self.assertEqual(len(report.load_run(path)['async_events']), 18)

    def test_alignment_uses_origin_and_source_lag_is_separate_from_queue_wait(self):
        path, _ = self.fixture(delay_us=1000, align_us=10000)
        run = report.load_run(path)
        timing = flow.async_timing(run, {0, 1, 2})
        self.assertEqual(timing['source_due_to_fire_ms']['mean'], .5)
        self.assertEqual(timing['fire_to_resume_submit_ms']['mean'], .25)
        self.assertEqual(timing['resume_executor_wait_ms']['mean'], .25)
        self.assertGreater(timing['issue_to_due_ms']['mean'], 1)

    def test_direct_and_async_contracts_verify_only_fixed_business_work(self):
        direct, _ = self.fixture('direct', percent=0)
        immediate, _ = self.fixture('zero')
        delayed, _ = self.fixture('delayed', delay_us=1000, policy='outstanding')
        runs = [report.load_run(path) for path in (direct, immediate, delayed)]
        self.assertEqual(len({run['summary']['fixed_work_signature'] for run in runs}), 1)
        self.assertEqual(len({run['pairing_key'] for run in runs}), 3)
        self.assertEqual(len(report.fixed_business_checks(runs)), 1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report.print_report(runs, 1000, 0, None)
        self.assertIn('Fixed business-work audit:', output.getvalue())
        self.assertIn('delayed: not paired (0 eligible home runs', output.getvalue())
        self.assertNotIn('delta mean/p95/p99 ms=', output.getvalue())
        paired, _ = self.fixture('matching-home', delay_us=1000)
        self.assertEqual(report.load_run(paired)['pairing_key'], runs[-1]['pairing_key'])

    def test_trace_zero_keeps_source_measurements_and_no_event_or_task_samples(self):
        path, (summary, rows, tasks, events) = self.fixture(trace=False)
        summary['async_source']['helper_cpu_valid'] = False
        save_fixture(path, summary, rows, tasks, events)
        run = report.load_run(path)
        self.assertIsNone(run['async_events'])
        self.assertIsNone(run['metrics']['operator_cpu_mean_ms'])
        self.assertIsNone(flow.async_timing(run, {0, 1, 2}))
        self.assertFalse((path / 'async.csv').exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report.print_report([run], 1000, 0, None)
            flow.print_flow(run, 0, None)
        self.assertIn('helper CPU s=-', out.getvalue())
        self.assertIn('event timing and observed resume callback metrics unavailable', out.getvalue())
        self.assertIn('Async event and resume timing unavailable: trace=0', out.getvalue())

    def test_flow_resume_is_control_role_and_first_fork_remains_business_only(self):
        path, _ = self.fixture()
        run = report.load_run(path)
        roles = flow.task_roles(run['summary'], run['tasks'])
        self.assertEqual(roles['rpc_resume']['n'], 18)
        self.assertEqual(roles['ordinary successors']['n'], 9)
        self.assertEqual(sum(row['n'] for row in roles.values()), 45)
        origin = run['summary']['origin_ns']
        first = diagnose.first_fork(run, origin, origin + 300 * MS)
        self.assertEqual(first['status'], 'available')
        self.assertEqual(len(first['rows']), 3)
        self.assertEqual(first['rows'][0]['recorded_submit_span_ms'], 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            flow.print_flow(run, 0, None)
        self.assertIn('3 completed requests x 9 business callbacks + 18 rpc_resume', out.getvalue())
        self.assertIn('source_due_to_fire_ms: n=18', out.getvalue())

    def test_unknown_schema_invalid_slot_plan_selection_and_contract_rejected(self):
        changes = [lambda s: s.update(schema='affinity-v4'),
                   lambda s: s.update(task_slots_per_request=19),
                   lambda s: s['planned_resume_counts'].__setitem__(0, 5),
                   lambda s: s.update(planned_resumes=19),
                   lambda s: s['async_contract'].update(selection='unknown'),
                   lambda s: s['async_contract'].update(percent=0),
                   lambda s: s.update(work_signature='{}'),
                   lambda s: s.update(fixed_work_signature=s['work_signature'])]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                path, (summary, rows, tasks, events) = self.fixture(str(index))
                change(summary)
                save_fixture(path, summary, rows, tasks, events)
                with self.assertRaises(ValueError):
                    report.load_run(path)

    def test_missing_duplicate_unselected_and_wrong_logical_tags_rejected(self):
        changes = [lambda tasks: tasks.pop(),
                   lambda tasks: tasks.append(copy.deepcopy(tasks[0])),
                   lambda tasks: tasks[1].update(logical_tag=8),
                   lambda tasks: tasks[2].update(tag=9, logical_tag=0, kind='rpc_resume'),
                   lambda tasks: tasks[2].update(iterations=1)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                path, (summary, rows, tasks, events) = self.fixture(str(index))
                change(tasks)
                summary['observed_tasks'] = len(tasks)
                save_fixture(path, summary, rows, tasks, events)
                with self.assertRaises(ValueError):
                    report.load_run(path)

    def test_async_event_identity_missing_resumes_and_source_conservation_rejected(self):
        mutations = [lambda s, e: e.pop(),
                     lambda s, e: e.append(copy.deepcopy(e[0])),
                     lambda s, e: e[0].update(logical_tag=0),
                     lambda s, e: e[0].update(status='unknown'),
                     lambda s, e: s['async_source'].update(pending=1),
                     lambda s, e: s['async_source'].update(callback_failures=1),
                     lambda s, e: s['async_source'].update(resume_succeeded=17),
                     lambda s, e: s['async_source'].update(peak_pending=0),
                     lambda s, e: s['async_source'].update(peak_pending=129)]
        for index, change in enumerate(mutations):
            with self.subTest(index=index):
                path, (summary, rows, tasks, events) = self.fixture(str(index))
                change(summary, events)
                save_fixture(path, summary, rows, tasks, events)
                with self.assertRaises(ValueError):
                    report.load_run(path)

    def test_wrong_due_early_fire_issue_and_ack_times_rejected(self):
        mutations = [lambda e: e[0].update(due_ns=e[0]['due_ns'] + 1),
                     lambda e: e[0].update(fired_ns=e[0]['due_ns'] - 1),
                     lambda e: e[0].update(issue_ns=e[0]['issue_ns'] - 100 * MS),
                     lambda e: e[0].update(terminal_ns=e[0]['fired_ns'] + 1),
                     lambda e: e[0].update(terminal_ns=e[0]['fired_ns'] - 1)]
        for index, change in enumerate(mutations):
            with self.subTest(index=index):
                path, (summary, rows, tasks, events) = self.fixture(str(index), delay_us=1000)
                change(events)
                save_fixture(path, summary, rows, tasks, events)
                with self.assertRaises(ValueError):
                    report.load_run(path)

    def test_failures_keep_only_valid_plan_subset_and_do_not_require_success_shape(self):
        for event_status, request_status in (('canceled', 'async_canceled'),
                                             ('resume_failed', 'async_resume_failed'),
                                             ('register_rejected', 'async_register_rejected'),
                                             (None, 'failed')):
            with self.subTest(status=event_status):
                path, (summary, rows, tasks, events) = self.fixture(str(event_status), delay_us=5000)
                first = events[0]
                tasks[:] = [task for task in tasks if task['request'] != 0 or task['tag'] in (0, first['logical_tag'])]
                events[:] = [event for event in events if event['request'] != 0]
                source = summary['async_source']
                source.update(registered=12, fired=12, resume_succeeded=12, peak_pending=12)
                if event_status is not None:
                    first['status'] = event_status
                    if event_status == 'register_rejected':
                        first.update(fired_ns=0, terminal_ns=first['issue_ns'] + 1)
                        source.update(rejected=1, registration_rejected=1)
                    else:
                        source['registered'] += 1
                        if event_status == 'canceled':
                            first.update(fired_ns=first['issue_ns'] + 1, terminal_ns=first['issue_ns'] + 2)
                            source['canceled'] = 1
                        else:
                            source['fired'] += 1
                            source['resume_failed'] = 1
                    events.insert(0, first)
                rows[0]['status'] = request_status
                checksum = 0
                for row in rows[1:]:
                    checksum = report.mix(checksum ^ row['checksum'])
                summary.update(completed=2, failed_or_rejected=1, checksum=str(checksum),
                               observed_tasks=len(tasks))
                save_fixture(path, summary, rows, tasks, events)
                run = report.load_run(path)
                self.assertFalse(run['valid_for_comparison'])
                self.assertEqual(run['cohort'][0]['status'], request_status)
                self.assertEqual(len(run['completed_cohort']), 2)

    def test_fixed_work_check_ignores_resume_cpu_but_rejects_changed_business(self):
        first, _ = self.fixture('one')
        second, (summary, rows, tasks, events) = self.fixture('two', delay_us=1000)
        for task in tasks:
            if task['kind'] == 'rpc_resume':
                task['cpu_ns'] *= 2
        save_fixture(second, summary, rows, tasks, events)
        report.fixed_business_checks([report.load_run(first), report.load_run(second)])
        next(task for task in tasks if task['kind'] == 'operator')['checksum'] += 1
        save_fixture(second, summary, rows, tasks, events)
        with self.assertRaisesRegex(ValueError, 'fixed business-work operator'):
            report.fixed_business_checks([report.load_run(first), report.load_run(second)])

    def test_all_cli_read_async_schema_and_trace_zero_without_task_values(self):
        first, _ = self.fixture('traced')
        second, _ = self.fixture('untraced', trace=False)
        for tool in ('report.py', 'flow_report.py', 'diagnose.py', 'arrival_audit.py'):
            with self.subTest(tool=tool):
                args = [sys.executable, str(ROOT / tool), str(first), str(second),
                        '--warmup-seconds', '0', '--window-seconds', '.3']
                if tool == 'diagnose.py':
                    args += ['--bin-ms', '100', '--max-bins', '4']
                result = subprocess.run(args, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('traced', result.stdout)
                self.assertIn('untraced', result.stdout)


if __name__ == '__main__':
    unittest.main()
