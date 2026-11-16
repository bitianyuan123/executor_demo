#!/usr/bin/env python3
"""Integration, unknown-capacity and provenance tests for CPU trace curves."""
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cpu_trace_analysis', ROOT/'experiments/independent/scripts/analyze_cpu_trace.py')
a = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(a)


def dump(path, value):
    path.write_text(json.dumps(value))


def event(cpu, ns, prev, nxt):
    return f' task-1 [{cpu:03d}] d..2. {ns//10**9}.{ns%10**9:09d}: sched_switch: prev_comm=task prev_pid={prev} prev_prio=120 prev_state=S ==> next_comm=task next_pid={nxt} next_prio=120\n'


def fixture(root):
    directory = root/'runs/home'
    data = directory/'data'
    data.mkdir(parents=True)
    plan = {'cpu_union': [0, 1], 'pool_cpus': [[0], [1]], 'cpu_trace': {'bin_ms': 5},
            'analysis': {'warmup_seconds': .2, 'request_end_seconds': 1.}}
    plan['plan_sha256'] = a.digest(plan)
    dump(root/'plan.json', plan)
    status = {'status': 'ok', 'clock': 'mono', 'cpu_ids': [0, 1],
              'start_monotonic_ns': 1_000_000_000, 'end_monotonic_ns': 3_000_000_000,
              'stats': {str(cpu): {'overrun': 0, 'commit overrun': 0, 'dropped events': 0} for cpu in (0, 1)},
              'online_cpu_ids_start': [0, 1, 2, 3], 'online_cpu_ids_end': [0, 1, 2, 3]}
    dump(directory/'cpu_trace_status.json', status)
    dump(data/'metadata.json', {'epoch_monotonic_ns': 1_000_000_000, 'effective_masks': [[0], [1]]})
    rows = [{'burst': 0, 'home_pool': 0, 'request_planned_ns': 800_000_000, 'phase': 'initial', 'submit_ns': 801_000_000},
            {'burst': 0, 'home_pool': 0, 'request_planned_ns': 800_000_000, 'phase': 'steady', 'submit_ns': 1_400_000_000}]
    a.write_csv(data/'tasks.csv', rows)
    (directory/'cpu_trace.txt').write_text(event(0, 1_005_000_000, 0, 1)+event(1, 1_006_000_000, 2, 0)+
                                          event(0, 2_900_000_000, 1, 0)+event(1, 2_910_000_000, 0, 2))
    entry = {'policy': 'home', 'path': 'runs/home', 'raw_dir': 'runs/home/data', 'status': 'completed',
             'returncode': 0, 'warmup_seconds': .2, 'options': {'duration': 1.}}
    dump(root/'run_manifest.json', {'plan_sha256': plan['plan_sha256'], 'runs': [entry]})
    return directory


class CpuTraceAnalysis(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self):
        return a.analyze(self.root/'plan.json', self.root/'run_manifest.json', self.root/'out')

    def update(self, path, change):
        content = a.read_json(path)
        change(content)
        dump(path, content)

    def test_exact_five_ms_integration_clipped_edges_and_full_capacity(self):
        path = self.root/'trace'
        path.write_text(event(0, 2_000_000, 0, 1)+event(1, 3_000_000, 9, 0)+
                        event(0, 7_000_000, 1, 0)+event(1, 9_000_000, 0, 9))
        tracks, issues = a.parse_trace(path, [0, 1], 1_000_000, 12_000_000)
        self.assertEqual(issues, [])
        self.assertEqual(tracks[0]['intervals'], [(2_000_000, 7_000_000)])
        self.assertEqual(tracks[1]['intervals'], [(1_000_000, 3_000_000), (9_000_000, 12_000_000)])
        curve, _ = a.make_curve(tracks, {'union': [0, 1], 'pool0': [0]}, 1_000_000, 12_000_000, 0, 5_000_000)
        union = [row for row in curve if row['scope'] == 'union']
        self.assertEqual([r['busy_ns'] for r in union], [5_000_000, 3_000_000, 2_000_000])
        self.assertEqual([r['capacity_ns'] for r in union], [8_000_000, 10_000_000, 4_000_000])
        self.assertEqual([r['occupancy_pct'] for r in union], [62.5, 30., 50.])
        self.assertEqual([r['complete_bin'] for r in union], [0, 1, 0])

    def test_events_on_boundary_are_not_double_counted(self):
        path = self.root/'trace'
        path.write_text(event(0, 5_000_000, 0, 1)+event(0, 10_000_000, 1, 0))
        tracks, _ = a.parse_trace(path, [0], 0, 15_000_000)
        curve, _ = a.make_curve(tracks, {'union': [0]}, 0, 15_000_000, 0, 5_000_000)
        self.assertEqual([r['occupancy_pct'] for r in curve], [0., 100., 0.])

    def test_no_event_cpu_is_unknown_and_union_never_renormalizes(self):
        path = self.root/'trace'
        path.write_text(event(0, 5_000_000, 0, 1))
        tracks, _ = a.parse_trace(path, [0, 1], 0, 10_000_000)
        self.assertEqual(tracks[1]['status'], 'unknown')
        curve, _ = a.make_curve(tracks, {'union': [0, 1], 'pool0': [0]}, 0, 10_000_000, 0, 5_000_000)
        union = [r for r in curve if r['scope'] == 'union'][-1]
        self.assertIsNone(union['busy_ns'])
        self.assertIsNone(union['occupancy_pct'])
        self.assertEqual(union['known_busy_ns'], 5_000_000)
        self.assertEqual(union['capacity_ns'], 10_000_000)
        self.assertEqual(union['known_capacity_ns'], 5_000_000)
        self.assertEqual(union['coverage'], .5)
        self.assertEqual(curve[-1]['occupancy_pct'], 100.)

    def test_first_and_last_event_state_inference(self):
        path = self.root/'trace'
        path.write_text(event(0, 3, 9, 0)+event(0, 7, 0, 9))
        tracks, _ = a.parse_trace(path, [0], 1, 10)
        self.assertEqual(tracks[0]['intervals'], [(1, 3), (7, 10)])

    def test_order_is_per_cpu_not_global_and_reversals_are_invalid(self):
        path = self.root/'trace'
        path.write_text(event(0, 6, 0, 1)+event(1, 3, 0, 2)+event(0, 8, 1, 0))
        tracks, _ = a.parse_trace(path, [0, 1], 0, 10)
        self.assertTrue(all(t['status'] == 'ok' for t in tracks.values()))
        path.write_text(event(0, 6, 0, 1)+event(0, 3, 1, 0))
        tracks, _ = a.parse_trace(path, [0], 0, 10)
        self.assertIn('per_cpu_time_reversal', tracks[0]['issues'])
        self.assertEqual(tracks[0]['intervals'], [])

    def test_missing_idle_transition_invalidates_cpu(self):
        path = self.root/'trace'
        path.write_text(event(0, 3, 0, 1)+event(0, 7, 0, 9))
        tracks, _ = a.parse_trace(path, [0], 0, 10)
        self.assertIn('idle_state_discontinuity', tracks[0]['issues'])

    def test_lost_event_marker_and_malformed_events_are_not_ignored(self):
        path = self.root/'trace'
        path.write_text('CPU:0 [LOST 7 EVENTS]\nnot a valid sched_switch: line\n')
        _, issues = a.parse_trace(path, [0], 0, 10)
        self.assertIn('trace_reports_lost_events', issues)
        self.assertTrue(any('unparseable_sched_switch' in issue for issue in issues))

    def test_full_cli_and_late_followup_tasks_remain_in_cohort(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = a.main(['--plan', str(self.root/'plan.json'), '--manifest', str(self.root/'run_manifest.json'), '--output', str(self.root/'out')])
        self.assertEqual(code, 0)
        result = a.read_json(self.root/'out/summary.json')['runs'][0]
        self.assertEqual(result['cohort_requests'], 1)
        self.assertEqual(result['cohort_tasks'], 2)
        self.assertEqual(result['aligned_windows'][0]['mean_home_occupancy_pct'], 100.)
        for name in ('cpu_curve.csv', 'aligned_home_curve.csv', 'aligned_windows.csv', 'cpu_full.svg', 'cpu_zoom.svg', 'cpu_aligned.svg', 'quality.json', 'report.txt'):
            self.assertTrue((self.root/'out/home'/name).is_file(), name)
        self.assertIn('actual initial', (self.root/'out/home/cpu_full.svg').read_text())

    def test_clock_mismatch_is_invalid_and_removes_stale_curves(self):
        self.assertEqual(self.analyze()['status'], 'ok')
        self.update(self.directory/'cpu_trace_status.json', lambda value: value.update(clock='local'))
        result = self.analyze()['runs'][0]
        self.assertIn('trace_clock_not_mono', result['issues'])
        self.assertFalse((self.root/'out/home/cpu_curve.csv').exists())

    def test_loss_counter_and_hotplug_are_rejected_even_if_status_says_ok(self):
        path = self.directory/'cpu_trace_status.json'
        self.update(path, lambda value: value['stats']['0'].update(overrun=1))
        self.update(path, lambda value: value.update(online_cpu_ids_end=[0, 1, 2]))
        result = self.analyze()['runs'][0]
        self.assertIn('capture_loss:0:overrun', result['issues'])
        self.assertIn('cpu_hotplug_unsupported', result['issues'])

    def test_missing_loss_counter_is_not_assumed_zero(self):
        self.update(self.directory/'cpu_trace_status.json', lambda value: value['stats']['0'].pop('overrun'))
        self.assertIn('missing_loss_counter:0:overrun', self.analyze()['runs'][0]['issues'])

    def test_unknown_cpu_cli_failure_keeps_partial_scope_evidence(self):
        (self.directory/'cpu_trace.txt').write_text(event(0, 1_005_000_000, 0, 1))
        result = self.analyze()['runs'][0]
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['measurement_window']['scopes']['union']['occupancy_pct'])
        self.assertEqual(result['measurement_window']['scopes']['pool0']['occupancy_pct'], 100.)

    def test_artifact_tampering_and_plan_hash_drift_are_rejected(self):
        self.update(self.root/'run_manifest.json', lambda value: value['runs'][0].update(artifact_sha256={'cpu_trace.txt': 'bad'}))
        self.update(self.root/'plan.json', lambda value: value.update(extra='changed'))
        issues = self.analyze()['runs'][0]['issues']
        self.assertIn('artifact_hash_mismatch:cpu_trace.txt', issues)
        self.assertIn('plan_hash_mismatch', issues)

    def test_truncated_trace_cannot_pass_read_event_count_check(self):
        self.update(self.directory/'cpu_trace_status.json', lambda value: value['stats']['0'].update({'read events': 3}))
        self.assertIn('trace_event_count_mismatch:0', self.analyze()['runs'][0]['issues'])

    def test_incomplete_aligned_windows_report_coverage_without_zero_fill(self):
        self.update(self.directory/'cpu_trace_status.json', lambda value: value.update(end_monotonic_ns=1_850_000_000))
        (self.directory/'cpu_trace.txt').write_text(event(0, 1_005_000_000, 0, 1)+event(1, 1_006_000_000, 2, 0))
        result = self.analyze()['runs'][0]
        self.assertEqual(result['aligned_windows'][0]['requests'], 1)
        self.assertEqual(result['aligned_windows'][1]['requests'], 0)
        self.assertIsNone(result['aligned_windows'][1]['mean_home_occupancy_pct'])
        self.assertEqual(result['aligned_windows'][1]['expected_requests'], 1)
        self.assertEqual(result['status'], 'partial')

    def test_cpu_ids_mismatch_and_capture_failure_rejected(self):
        self.update(self.directory/'cpu_trace_status.json', lambda value: value.update(cpu_ids=[0], status='failed'))
        issues = self.analyze()['runs'][0]['issues']
        self.assertIn('capture_cpu_ids_mismatch', issues)
        self.assertIn('capture_status_not_ok', issues)

    def test_diagnostic_peak_high_fraction_and_contiguous_streak(self):
        def row(index, occupancy):
            return {'scope': 'union', 'start_monotonic_ns': index*5_000_000,
                    'end_monotonic_ns': (index+1)*5_000_000, 'complete_bin': 1,
                    'occupancy_pct': occupancy}
        rows = [row(i, value) for i, value in enumerate((100., 95., 50., 100.))]
        diag = a.curve_diagnostics(rows, ['union'], 0, 20_000_000, 5_000_000)['union']
        self.assertEqual(diag['observed_complete_bin_peak_pct'], 100.)
        self.assertEqual(diag['ge90_duration_fraction'], .75)
        self.assertEqual(diag['longest_observed_ge90_ms'], 10.)
        self.assertEqual(diag['complete_bin_coverage'], 1.)
        # An absent window must split a streak and cannot be assumed below 90%.
        rows = [row(0, 100.), row(2, 100.), row(3, 100.)]
        diag = a.curve_diagnostics(rows, ['union'], 0, 20_000_000, 5_000_000)['union']
        self.assertEqual(diag['longest_observed_ge90_ms'], 10.)
        self.assertEqual(diag['complete_bin_coverage'], .75)
        self.assertIsNone(diag['ge90_duration_fraction'])
        rows.insert(1, row(1, None))
        diag = a.curve_diagnostics(rows, ['union'], 0, 20_000_000, 5_000_000)['union']
        self.assertEqual(diag['longest_observed_ge90_ms'], 10.)
        self.assertIsNone(diag['ge90_duration_fraction'])

    def test_diagnostics_exclude_partial_bins_and_measurement_boundaries(self):
        rows = [{'scope': 'union', 'start_monotonic_ns': a_ns, 'end_monotonic_ns': b_ns,
                 'complete_bin': complete, 'occupancy_pct': value}
                for a_ns, b_ns, complete, value in ((0, 5_000_000, 1, 100),
                    (5_000_000, 10_000_000, 1, 20), (10_000_000, 12_000_000, 0, 100))]
        diag = a.curve_diagnostics(rows, ['union'], 1_000_000, 12_000_000, 5_000_000)['union']
        self.assertEqual(diag['expected_complete_bins'], 1)
        self.assertEqual(diag['observed_complete_bin_peak_pct'], 20)
        self.assertEqual(diag['ge90_duration_fraction'], 0.)
        self.assertEqual(diag['longest_observed_ge90_ms'], 0.)

    def test_terminal_summary_has_bin_statistics_observer_and_five_phase_windows(self):
        self.update(self.directory/'cpu_trace_status.json', lambda value: value.update(
            observer_process_cpu_seconds=.03, reader_thread_cpu_seconds=.01))
        report = self.analyze()
        self.assertEqual(len(report['runs'][0]['aligned_windows']), 5)
        text = (self.root/'out/report.txt').read_text()
        self.assertIn('observer_process_cpu_seconds', text)
        self.assertIn('ge90_duration_fraction', text)
        self.assertIn('longest_observed_ge90_ms', text)
        self.assertIn('200-300', text)
        self.assertIn('not a routing threshold', text)
        self.assertNotIn('independent 5 ms', text)

    def test_short_peak_display_has_fine_windows_full_period_and_two_zoom_views(self):
        plan = a.read_json(self.root/'plan.json')
        plan.update(profile='short-peak', nominal={'per_pool_request_gap_ms': 500}, cpu_trace={'bin_ms': 1})
        plan['plan_sha256'] = a.digest({key: value for key, value in plan.items() if key != 'plan_sha256'})
        dump(self.root/'plan.json', plan)
        self.update(self.root/'run_manifest.json', lambda value: value.update(plan_sha256=plan['plan_sha256']))
        result = self.analyze()['runs'][0]
        self.assertEqual(result['status'], 'ok')
        # Capture validity does not disguise insufficient waveform evidence.
        self.assertFalse(result['request_waveform']['passed'])
        self.assertEqual(result['aligned_curve']['anchor'], 'actual_first_initial_submit')
        self.assertEqual(result['aligned_curve']['range_ms'], [-10, 500])
        windows = result['aligned_windows']
        self.assertEqual([(r['start_ms'], r['end_ms']) for r in windows[:4]], [(0, 5), (5, 10), (10, 20), (20, 30)])
        self.assertEqual((windows[-1]['start_ms'], windows[-1]['end_ms']), (300, 500))
        with (self.root/'out/home/aligned_home_curve.csv').open() as handle:
            aligned = list(csv.DictReader(handle))
        self.assertEqual(max(float(r['relative_end_ms']) for r in aligned), 500)
        for filename in ('cpu_aligned.svg', 'cpu_aligned_peak.svg'):
            svg = (self.root/'out/home'/filename).read_text()
            self.assertIn('actual first initial submit', svg)
            self.assertIn('NOT the distribution', svg)
        self.assertTrue((self.root/'out/home/cpu_peak.svg').exists())
        self.assertIn('ALIGNED CURVE', (self.root/'out/report.txt').read_text())

    def test_monotonic_timestamp_conversion_does_not_use_float(self):
        self.assertEqual(a.stamp_ns('2195011.576957'), 2195011576957000)
        self.assertEqual(a.stamp_ns('999999999.123456789'), 999999999123456789)


class ShortPeakWaveform(unittest.TestCase):
    def scene(self, pulses=((0, 10),), count=30, period=500, high_cpus=10):
        start, end = 1_000_000_000, 1_000_000_000+round(count*period*1e6)
        plan = {'profile': 'short-peak', 'pool_cpus': [list(range(10))],
                'nominal': {'per_pool_request_gap_ms': period}}
        requests = [{'burst': i, 'home_pool': 0, 'planned_monotonic_ns': start+round(i*period*1e6),
                     'initial_start_monotonic_ns': start+round(i*period*1e6)} for i in range(count)]
        tracks = {}
        for cpu in range(10):
            intervals = ([(start, end)] if cpu < 5 else
                [(r['planned_monotonic_ns']+round(left*1e6), r['planned_monotonic_ns']+round(right*1e6))
                 for r in requests for left, right in pulses] if cpu < high_cpus else [])
            tracks[cpu] = {'status': 'ok', 'intervals': intervals}
        scopes = {'union': list(tracks), 'pool0': list(tracks)}
        integrals = {cpu: a.Integral(track['intervals']) for cpu, track in tracks.items()}
        measurement = {'full_capture_coverage': True,
                       'scopes': {'union': a.scope_value(list(tracks), tracks, integrals, start, end)}}
        return [plan, 'home', requests, scopes, tracks, integrals, start, end, 1_000_000, measurement]

    def test_short_peak_passes_fixed_contract_and_preserves_per_request_evidence(self):
        result, rows = a.request_waveform(*self.scene())
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(rows), 30)
        self.assertEqual(result['sensitivity']['90']['onset_width_ms']['median'], 10)
        self.assertEqual(result['background_mean_pct']['median'], 50)
        self.assertAlmostEqual(result['union_mean_pct'], 51)
        self.assertEqual(rows[0]['background_window_ms'], 470)

    def test_long_peak_fails_even_when_aligned_mean_might_hide_it(self):
        result, rows = a.request_waveform(*self.scene(pulses=((0, 200),)))
        self.assertFalse(result['passed'])
        self.assertEqual(rows[0]['high90_onset_width_ms'], 200)
        self.assertIn('p90_onset_high_width_exceeds_25ms', result['issues'])
        self.assertIn('background_p90_longest_high_exceeds_10ms', result['issues'])

    def test_missing_cpu_or_missing_capture_never_passes(self):
        scene = self.scene()
        scene[4][9]['status'] = 'unknown'
        result, rows = a.request_waveform(*scene)
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertEqual(result['complete_requests'], 0)
        self.assertEqual(len(rows), 30)
        self.assertIn('complete_trace_required', result['issues'])
        scene = self.scene()
        scene[7] -= 100_000_000
        result, rows = a.request_waveform(*scene)
        self.assertEqual(result['complete_requests'], 29)
        self.assertFalse(result['passed'])
        self.assertIn('capture_does_not_cover_request_window', rows[-1]['issues'])

    def test_no_onset_and_fragmented_onset_fail_separately(self):
        result, _ = a.request_waveform(*self.scene(pulses=((100, 110),)))
        self.assertIn('near_onset_high_run_fraction_below_90pct', result['issues'])
        result, _ = a.request_waveform(*self.scene(pulses=((0, 10), (14, 24))))
        self.assertEqual(result['sensitivity']['90']['onset_width_ms']['median'], 10)
        self.assertIn('fragmented_onset_fraction_exceeds_10pct', result['issues'])
        self.assertFalse(result['passed'])

    def test_long_late_peak_fails_despite_short_initial_peak(self):
        result, _ = a.request_waveform(*self.scene(pulses=((0, 10), (200, 240))))
        self.assertEqual(result['sensitivity']['90']['onset_width_ms']['median'], 10)
        self.assertEqual(result['sensitivity']['90']['background_longest_high_ms']['p90'], 40)
        self.assertIn('background_p90_longest_high_exceeds_10ms', result['issues'])

    def test_sensitivity_does_not_replace_declared_threshold(self):
        result, _ = a.request_waveform(*self.scene(high_cpus=9))
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['sensitivity']['80']['onset_fraction'], 1)
        self.assertEqual(result['sensitivity']['90']['onset_fraction'], 1)
        self.assertEqual(result['sensitivity']['95']['onset_fraction'], 0)
        self.assertEqual(result['contract']['high_pct'], 90)
        result, _ = a.request_waveform(*self.scene(high_cpus=8))
        self.assertFalse(result['passed'])
        self.assertEqual(result['sensitivity']['80']['onset_fraction'], 1)
        self.assertEqual(result['sensitivity']['90']['onset_fraction'], 0)

    def test_delayed_wave_clipped_at_next_actual_home_and_not_silently_dropped(self):
        scene = self.scene()
        scene[2][0]['initial_start_monotonic_ns'] += 100_000_000
        result, rows = a.request_waveform(*scene)
        first = rows[0]
        self.assertEqual(first['anchor_lag_ms'], 100)
        self.assertTrue(first['clipped_by_next_request'])
        self.assertEqual(first['window_end_monotonic_ns'], scene[2][1]['initial_start_monotonic_ns'])
        self.assertEqual(first['background_window_ms'], 370)
        self.assertIn('next_home_request_shortens_background', first['issues'])
        self.assertEqual(result['requests'], 30)
        self.assertEqual(result['complete_requests'], 29)
        self.assertFalse(result['passed'])

    def test_period_is_not_hardcoded_and_only_home_is_gated(self):
        result, rows = a.request_waveform(*self.scene(period=400))
        self.assertTrue(result['passed'], result)
        self.assertEqual(rows[0]['background_window_ms'], 370)
        scene = self.scene()
        scene[1] = 'source_only'
        result, rows = a.request_waveform(*scene)
        self.assertEqual(result['status'], 'not_applicable')
        self.assertFalse(result['passed'])
        self.assertEqual(len(rows), 30)
        scene[0]['profile'] = 'legacy'
        result, rows = a.request_waveform(*scene)
        self.assertEqual(result['status'], 'not_applicable')
        self.assertEqual(rows, [])

    def test_too_coarse_bins_and_changed_frozen_contract_cannot_pass(self):
        scene = self.scene()
        scene[8] = 5_000_000
        result, _ = a.request_waveform(*scene)
        self.assertIn('integration_bin_exceeds_2ms', result['issues'])
        self.assertFalse(result['passed'])
        scene = self.scene()
        scene[0]['waveform_acceptance'] = {**a.WAVEFORM_CONTRACT, 'high_pct': 80}
        result, _ = a.request_waveform(*scene)
        self.assertIn('frozen_waveform_contract_mismatch', result['issues'])
        self.assertFalse(result['passed'])

    def test_short_alignment_clips_next_burst_and_keeps_missing_wave_denominator(self):
        scene = self.scene()
        scene[2][0]['initial_start_monotonic_ns'] += 100_000_000
        rows, windows = a.align_requests(scene[2], scene[3], scene[4], scene[5],
            scene[6], scene[7], scene[8], 500)
        last = next(row for row in rows if row['scope'] == 'home_mean' and row['relative_start_ms'] == 499)
        self.assertEqual(last['expected_requests'], 30)
        self.assertEqual(last['requests'], 29)
        self.assertAlmostEqual(last['coverage'], 29/30)
        self.assertEqual(windows[-1]['requests'], 30)
        self.assertEqual(windows[-1]['expected_requests'], 30)
        self.assertEqual(windows[-1]['complete_requests'], 29)
        self.assertEqual(windows[-1]['truncated_requests'], 1)
        self.assertAlmostEqual(windows[-1]['duration_coverage'], 5900/6000)
        self.assertEqual(windows[-1]['max_missing_window_ms'], 100)
        # No extra part of the following home burst is averaged into this tail.
        self.assertEqual(last['occupancy_pct'], 50)

    def test_microsecond_tail_truncation_keeps_measurement_and_exposes_time_coverage(self):
        scene = self.scene()
        scene[2][0]['initial_start_monotonic_ns'] += 50_000
        rows, windows = a.align_requests(scene[2], scene[3], scene[4], scene[5],
            scene[6], scene[7], scene[8], 500)
        last = next(row for row in rows if row['scope'] == 'home_mean' and row['relative_start_ms'] == 499)
        self.assertEqual(last['requests'], 30)
        self.assertEqual(last['complete_requests'], 29)
        self.assertAlmostEqual(last['duration_coverage'], 29.95/30)
        tail = windows[-1]
        self.assertEqual(tail['requests'], 30)
        self.assertEqual(tail['complete_requests'], 29)
        self.assertAlmostEqual(tail['duration_coverage'], 5999.95/6000)
        self.assertAlmostEqual(tail['max_missing_window_ms'], .05)
        self.assertEqual(tail['mean_home_occupancy_pct'], 50)

    def test_terminal_waveform_is_compact_and_preserves_fixed_threshold_meaning(self):
        result, _ = a.request_waveform(*self.scene())
        lines = a.waveform_text(result)
        text = '\n'.join(lines)
        self.assertIn('REQUEST WAVEFORM status=passed; passed=true', text)
        self.assertIn('complete_waves=30/30', text)
        self.assertIn('80,100.000,10.000', text)
        self.assertIn('90,100.000,10.000', text)
        self.assertIn('95,100.000,10.000', text)
        self.assertIn('NOT business calibration', text)
        self.assertNotIn('"contract":', text)
        self.assertLessEqual(len(lines), 11)


if __name__ == '__main__':
    unittest.main()
