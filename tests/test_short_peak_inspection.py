#!/usr/bin/env python3
"""Meaningful shape, provenance, missing-data and read-only inspection checks."""
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('short_peak_inspection',
    ROOT/'experiments/independent/scripts/inspect_short_peak.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def bins(*runs):
    return [(float(i), float(i+1), 100. if any(left <= i < right for left, right in runs) else 50.)
            for i in range(30)]


def dump(path, value):
    path.write_text(json.dumps(value))


def event(cpu, ns, prev, nxt):
    return (f' task-1 [{cpu:03d}] d..2. {ns//10**9}.{ns%10**9:09d}: sched_switch: '
            f'prev_comm=task prev_pid={prev} prev_prio=120 prev_state=S ==> '
            f'next_comm=task next_pid={nxt} next_prio=120\n')


def fixture(root):
    directory = root/'runs/home'
    data = directory/'data'
    data.mkdir(parents=True)
    epoch = 1_000_000_000
    plan = {'profile': 'short-peak', 'cpu_union': [0, 1], 'pool_cpus': [[0, 1]],
            'cpu_trace': {'bin_ms': 1}, 'nominal': {'per_pool_request_gap_ms': 100.},
            'waveform_acceptance': dict(m.a.WAVEFORM_CONTRACT),
            'analysis': {'warmup_seconds': .01, 'request_end_seconds': .2}}
    plan['plan_sha256'] = m.a.digest(plan)
    dump(root/'plan.json', plan)
    capture = {'status': 'ok', 'clock': 'mono', 'cpu_ids': [0, 1],
               'start_monotonic_ns': epoch, 'end_monotonic_ns': epoch+400_000_000,
               'stats': {str(cpu): {'overrun': 0, 'commit overrun': 0, 'dropped events': 0}
                         for cpu in (0, 1)}}
    dump(directory/'cpu_trace_status.json', capture)
    dump(data/'metadata.json', {'epoch_monotonic_ns': epoch, 'effective_masks': [[0, 1]]})
    rows = []
    for burst, offset in enumerate((20_000_000, 120_000_000)):
        for delta in (0, 1_000_000):
            rows.append({'burst': burst, 'home_pool': 0, 'request_planned_ns': offset,
                         'phase': 'initial', 'submit_ns': offset+delta})
    m.a.write_csv(data/'tasks.csv', rows)
    trace = event(0, epoch+1_000_000, 0, 1)+event(0, epoch+399_000_000, 1, 0)
    # First request: a 1-ms notch within a 9-ms event. Second: late second peak.
    for start, end in ((20, 22), (23, 29), (120, 125), (142, 145)):
        trace += event(1, epoch+start*1_000_000, 0, 2)+event(1, epoch+end*1_000_000, 2, 0)
    (directory/'cpu_trace.txt').write_text(trace)
    entry = {'policy': 'home', 'path': 'runs/home', 'raw_dir': 'runs/home/data',
             'status': 'completed', 'returncode': 0, 'plan_sha256': plan['plan_sha256'],
             'warmup_seconds': .01, 'options': {'duration': .2},
             'artifact_sha256': {name: m.a.file_digest(directory/name) for name in
                 ('data/tasks.csv', 'data/metadata.json', 'cpu_trace.txt', 'cpu_trace_status.json')}}
    manifest = {'plan_sha256': plan['plan_sha256'], 'runs': [entry]}
    dump(root/'run_manifest.json', manifest)
    original = m.a.analyze(root/'plan.json', root/'run_manifest.json', root/'cpu-curves')
    shape = original['runs'][0]['request_waveform']
    manifest['home_waveform_gate'] = {'status': shape['status'], 'passed': shape['passed'],
                                    'comparison_allowed': False, 'home_request_waveform': shape}
    dump(root/'run_manifest.json', manifest)
    return directory


class ShapeDescription(unittest.TestCase):
    def test_one_ms_dip_and_late_second_peak_are_different(self):
        notch = m.describe_bins(bins((0, 2), (3, 9)))
        late = m.describe_bins(bins((0, 5), (22, 25)))
        self.assertTrue(notch['strict_fragmented'])
        self.assertTrue(late['strict_fragmented'])
        self.assertEqual(notch['category'], 'split_within_20ms')
        self.assertEqual(notch['envelope_ms'], 9.)
        self.assertEqual(notch['max_gap_ms'], 1.)
        self.assertEqual(notch['late_20_30_high_ms'], 0.)
        self.assertTrue(notch['all_gaps_at_most_1ms'])
        self.assertEqual(late['category'], 'high_after_20ms')
        self.assertEqual(late['envelope_ms'], 25.)
        self.assertEqual(late['late_20_30_high_ms'], 3.)
        self.assertEqual(late['max_gap_ms'], 17.)

    def test_continuing_peak_is_censored_at_30_and_not_recovered(self):
        row = m.describe_bins(bins((0, 30)))
        self.assertTrue(row['high_at_30ms_boundary'])
        self.assertEqual(row['late_20_30_high_ms'], 10.)
        self.assertFalse(row['strict_fragmented'])

    def test_unknown_missing_and_gapped_bins_are_never_zero_filled(self):
        for rows in (bins()[:-1], bins()[1:], bins()[:5]+bins()[6:],
                     [(0., 1., None)]+bins()[1:]):
            with self.assertRaisesRegex(ValueError, 'incomplete_or_invalid'):
                m.describe_bins(rows)

    def test_no_high_segment_has_unknown_envelope_and_zero_observed_high(self):
        row = m.describe_bins(bins())
        self.assertEqual(row['segment_count'], 0)
        self.assertIsNone(row['envelope_ms'])
        self.assertEqual(row['total_high_ms'], 0.)


class InspectionIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def inspect(self):
        return m.inspect(self.root/'plan.json', self.root/'run_manifest.json')

    def update(self, path, change):
        value = m.a.read_json(path)
        change(value)
        dump(path, value)

    def test_exact_segments_match_saved_strict_and_leave_original_files_untouched(self):
        files = [self.root/'plan.json', self.root/'run_manifest.json',
                 self.root/'cpu-curves/summary.json', self.root/'cpu-curves/report.txt',
                 self.root/'cpu-curves/home/request_waveform.csv', self.directory/'cpu_trace.txt']
        before = {str(path): path.read_bytes() for path in files}
        with contextlib.redirect_stdout(io.StringIO()):
            code = m.main(['--plan', str(self.root/'plan.json')])
        self.assertEqual(code, 0)
        self.assertEqual(before, {str(path): path.read_bytes() for path in files})
        result = m.a.read_json(self.root/'short-peak-inspection/inspection.json')
        self.assertEqual(result['fragment_count_check']['fragmented'], 2)
        self.assertTrue(result['fragment_count_check']['matches'])
        self.assertFalse(result['strict_original_recomputed']['passed'])
        self.assertEqual(result['groups']['fragmented']['categories']['split_within_20ms'], 1)
        self.assertEqual(result['groups']['fragmented']['categories']['high_after_20ms'], 1)
        self.assertEqual(result['worst_fragmented_examples'][0]['burst'], 1)
        self.assertEqual(result['worst_fragmented_examples'][0]['segments_ms'], [[0., 5.], [22., 25.]])
        with (self.root/'short-peak-inspection/waves.csv').open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0]['initial_submit_span_ms']), 1.)

    def test_plan_identity_mismatch_does_not_emit_quantitative_rows(self):
        self.update(self.root/'plan.json', lambda value: value.update(profile='modified'))
        result, rows = self.inspect()
        self.assertEqual(result['status'], 'invalid')
        self.assertIn('plan_hash_mismatch', result['issues'])
        self.assertEqual(rows, [])

    def test_saved_fragment_count_mismatch_is_not_silently_replaced(self):
        self.update(self.root/'run_manifest.json', lambda value:
            value['home_waveform_gate']['home_request_waveform']['sensitivity']['90'].update(fragmented_fraction=0.))
        result, rows = self.inspect()
        self.assertEqual(result['status'], 'invalid')
        self.assertIn('saved_strict_result_mismatch:manifest.home_waveform_gate', result['issues'])
        self.assertEqual(rows, [])

    def test_missing_trace_and_artifact_mutation_are_rejected(self):
        path = self.directory/'cpu_trace.txt'
        path.write_text(path.read_text()+'extra bytes\n')
        result, rows = self.inspect()
        self.assertEqual(result['status'], 'invalid')
        self.assertIn('artifact_hash_mismatch:cpu_trace.txt', result['issues'])
        self.assertEqual(rows, [])
        path.unlink()
        self.assertEqual(self.inspect()[0]['status'], 'invalid')

    def test_unknown_cpu_without_artifact_hash_is_still_rejected(self):
        self.update(self.root/'run_manifest.json', lambda value:
            value['runs'][0].pop('artifact_sha256'))
        path = self.directory/'cpu_trace.txt'
        path.write_text('\n'.join(line for line in path.read_text().splitlines() if '[001]' not in line)+'\n')
        result, rows = self.inspect()
        self.assertEqual(result['status'], 'invalid')
        self.assertIn('incomplete_cpu_trace', result['issues'])
        self.assertEqual(rows, [])

    def test_inspection_cannot_overwrite_saved_analysis(self):
        for directory in (self.root/'cpu-curves', self.root/'cpu-curves/home'):
            original = (directory/'report.txt').read_bytes()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                m.main(['--plan', str(self.root/'plan.json'), '--output', str(directory)])
            self.assertEqual((directory/'report.txt').read_bytes(), original)

    def test_arbitrary_existing_report_or_waves_file_is_not_ours(self):
        for name in ('report.txt', 'waves.csv', 'inspection.json'):
            output = self.root/('unrelated-'+name)
            output.mkdir()
            (output/name).write_text('unrelated user content')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                m.main(['--plan', str(self.root/'plan.json'), '--output', str(output)])
            self.assertEqual((output/name).read_text(), 'unrelated user content')
            self.assertEqual(len(list(output.iterdir())), 1)

    def test_matching_inspection_can_be_regenerated_but_other_plan_cannot(self):
        output = self.root/'owned'
        command = ['--plan', str(self.root/'plan.json'), '--output', str(output)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(command), 0)
            self.assertEqual(m.main(command), 0)
        self.update(output/'inspection.json', lambda value: value.update(plan='/unrelated/plan.json'))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.main(command)

    def test_symlinked_output_file_never_overwrites_its_target(self):
        output = self.root/'linked-output'
        output.mkdir()
        original = self.root/'keep.txt'
        original.write_text('must remain')
        (output/'report.txt').symlink_to(original)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.main(['--plan', str(self.root/'plan.json'), '--output', str(output)])
        self.assertEqual(original.read_text(), 'must remain')


if __name__ == '__main__':
    unittest.main()
