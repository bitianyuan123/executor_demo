#!/usr/bin/env python3
"""Private tracefs capture lifecycle and loss-detection regression checks."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'experiments/independent/scripts/cpu_trace.py'
SPEC = importlib.util.spec_from_file_location('cpu_trace', MODULE)
cpu_trace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cpu_trace)


class FakeCapture(cpu_trace.IdleTraceCapture):
    def __init__(self, *args, trace_text='', loss=None, fail_write=None, online_end=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace_text = trace_text
        self.loss = loss or {}
        self.fail_write = fail_write
        self.online_end = online_end
        self.writes = []

    def _online_cpus(self):
        return self.online_end if self._started and self.online_end is not None else self.cpus

    def _make_instance(self):
        super()._make_instance()
        for relative, data in {
            'tracing_on': '0', 'current_tracer': 'nop',
            'trace_clock': '[local] global mono', 'buffer_size_kb': '256',
            'tracing_cpumask': 'f', 'trace_pipe': self.trace_text,
            'events/sched/sched_switch/filter': 'none',
            'events/sched/sched_switch/enable': '0',
        }.items():
            path = self.instance / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        for cpu in self.cpus:
            path = self.instance / f'per_cpu/cpu{cpu}/stats'
            path.parent.mkdir(parents=True)
            path.write_text('\n'.join(f'{key}: {self.loss.get(key, 0)}' for key in ('entries', *cpu_trace.LOSS_FIELDS)) + '\n')

    def _remove_instance(self):
        shutil.rmtree(self.instance)

    def _write(self, relative, value):
        if relative == self.fail_write:
            raise PermissionError('fake denied')
        self.writes.append((relative, value))
        super()._write(relative, value)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tracefs = self.root / 'tracefs'
        (self.tracefs / 'instances').mkdir(parents=True)
        # Evidence that no global file or other tracer instance is modified.
        (self.tracefs / 'tracing_on').write_text('keep-global')
        (self.tracefs / 'instances/other').mkdir()
        (self.tracefs / 'instances/other/tracing_on').write_text('keep-other')

    def tearDown(self):
        self.assertEqual((self.tracefs / 'tracing_on').read_text(), 'keep-global')
        self.assertEqual((self.tracefs / 'instances/other/tracing_on').read_text(), 'keep-other')
        self.temporary.cleanup()

    def capture(self, **kwargs):
        return FakeCapture([[0, 1], [32]], self.root / 'output', self.tracefs, **kwargs)

    def test_mask_and_cpu_list(self):
        self.assertEqual(cpu_trace.cpu_mask([0, 31, 32, 239]), '00008000,00000000,00000000,00000000,00000000,00000000,00000001,80000001')
        self.assertEqual(cpu_trace.parse_cpu_list('0-3,8'), [0, 1, 2, 3, 8])
        for value in ('', '3-1', '0,0', '-1', '1-2,2'):
            with self.assertRaises(ValueError):
                cpu_trace.parse_cpu_list(value)

    def test_lifecycle_no_thread_until_requested_and_cleanup(self):
        trace = '<idle>-0 [000] 1.000001: sched_switch: prev_pid=0 ==> next_pid=21\n'
        capture = self.capture(trace_text=trace)
        capture.start()
        self.assertIsNone(capture._reader_thread)
        self.assertEqual(capture.status['status'], 'capturing')
        self.assertFalse(any(path.startswith('options') or path == 'trace_options' for path, _ in capture.writes))
        self.assertEqual((capture.instance / 'tracing_cpumask').read_text().strip(), '00000001,00000003')
        capture.start_reader()
        status = capture.stop()
        self.assertEqual(status['status'], 'ok', status)
        self.assertTrue(status['cleanup_complete'])
        self.assertFalse(capture.instance.exists())
        self.assertGreaterEqual(status['end_monotonic_ns'], status['start_monotonic_ns'])
        self.assertEqual(status['bytes_written'], len(trace))
        self.assertEqual((self.root / 'output/cpu_trace.txt').read_text(), trace)
        self.assertEqual(json.loads((self.root / 'output/cpu_trace_status.json').read_text()), status)
        self.assertIs(capture.stop(), status)
        self.assertGreaterEqual(status['observer_process_cpu_seconds'], status['reader_thread_cpu_seconds'])

    def test_partial_start_error_is_visible_and_cleans_only_own_instance(self):
        capture = self.capture(fail_write='buffer_size_kb')
        with self.assertRaisesRegex(RuntimeError, 'fake denied'):
            capture.start()
        self.assertEqual(capture.status['status'], 'failed')
        self.assertTrue(capture.status['cleanup_complete'])
        self.assertFalse(capture.instance.exists())
        self.assertTrue((self.root / 'output/cpu_trace_status.json').exists())

    def test_missing_tracefs_fails_without_silent_fallback(self):
        capture = FakeCapture([[0]], self.root / 'output', self.root / 'missing')
        with self.assertRaisesRegex(RuntimeError, 'instances directory unavailable'):
            capture.start()
        self.assertEqual(capture.status['status'], 'failed')
        self.assertTrue(capture.status['cleanup_complete'])

    def test_loss_counters_fail(self):
        for key in cpu_trace.LOSS_FIELDS:
            with self.subTest(key=key):
                capture = self.capture(loss={key: 1})
                capture.start()
                result = capture.stop()
                self.assertEqual(result['status'], 'failed')
                self.assertTrue(any(f'{key}=1' in error for error in result['errors']))
                self.assertTrue(result['cleanup_complete'])

    def test_remaining_buffer_entries_fail(self):
        capture = self.capture(loss={'entries': 2})
        capture.start()
        result = capture.stop()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any('not completely drained' in error for error in result['errors']))
        self.assertTrue(result['cleanup_complete'])

    def test_lost_marker_split_across_read_blocks_fails(self):
        capture = self.capture()
        capture.start()
        capture._consume(b'CPU: 0 [LOST 2 ')
        capture._consume(b'EVENTS]\n')
        result = capture.stop()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['lost_event_marker'])
        self.assertEqual((self.root / 'output/cpu_trace.txt').read_bytes(), b'CPU: 0 [LOST 2 EVENTS]\n')

    def test_hotplug_fails(self):
        capture = self.capture(online_end=[0, 1])
        capture.start()
        result = capture.stop()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any('offline' in error for error in result['errors']))
        self.assertTrue(result['cleanup_complete'])

    def test_missing_loss_counter_fails(self):
        capture = self.capture()
        capture.start()
        (capture.instance / 'per_cpu/cpu0/stats').write_text('overrun: 0\n')
        result = capture.stop()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any('missing or invalid' in error for error in result['errors']))
        self.assertTrue(result['cleanup_complete'])

    def test_reader_timeout_retains_shared_handles_and_instance(self):
        class StuckReader:
            def join(self, timeout):
                self.timeout = timeout

            def is_alive(self):
                return True

        capture = self.capture()
        capture.start()
        capture._reader_thread = StuckReader()
        pipe_fd = capture._pipe_fd
        trace_file = capture._trace_file
        try:
            result = capture.stop()
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(result['cleanup_complete'])
            self.assertIn('reader still alive', result['cleanup_deferred_reason'])
            self.assertTrue(capture.instance.exists())
            self.assertEqual(result['instance'], str(capture.instance))
            self.assertFalse(trace_file.closed)
            os.fstat(pipe_fd)  # Would fail if stop closed the shared descriptor.
            self.assertEqual((capture.instance / 'tracing_on').read_text().strip(), '0')
            self.assertEqual((capture.instance / 'events/sched/sched_switch/enable').read_text().strip(), '0')
            self.assertEqual(capture._reader_thread.timeout, 5)
            saved = json.loads((self.root / 'output/cpu_trace_status.json').read_text())
            self.assertEqual(saved['status'], 'failed')
            self.assertFalse(saved['cleanup_complete'])
            self.assertIs(capture.stop(), result)
        finally:
            # This is a fake reader on temporary files, so test-owned cleanup
            # cannot race with an actual live consumer or online trace instance.
            os.close(pipe_fd)
            trace_file.close()
            capture._remove_instance()

    def test_stop_without_reader_drains_and_no_restart(self):
        capture = self.capture(trace_text='kept\n')
        capture.start()
        self.assertEqual(capture.stop()['status'], 'ok')
        self.assertEqual((self.root / 'output/cpu_trace.txt').read_text(), 'kept\n')
        with self.assertRaises(RuntimeError):
            capture.start()


if __name__ == '__main__':
    unittest.main()
