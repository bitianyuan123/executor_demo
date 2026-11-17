#!/usr/bin/env python3
"""Evidence parser checks using the committed compact evidence, no attachments.

A printable transcript is reconstructed as a fixture, then deliberately damaged
at conservation/precision/boundary points. No target experiment is launched.
"""
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import unittest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('review_cluster_cpu_log', ROOT/'scripts/review_cluster_cpu_log.py')
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)
DATA = ROOT/'results/review'


def fixture():
    recorded = json.loads((DATA/'4021_summary.json').read_text())
    cpu = {}
    with (DATA/'4021_cpu_windows.csv').open() as handle:
        for row in csv.DictReader(handle):
            cpu.setdefault(row['round_id'], []).append(row)
    meta = recorded['metadata']
    lines = ['OPTIONAL CPU SUPPLEMENT: PLAN_ONLY', 'ROUND 99-fictional', 'never execute this',
             'OPTIONAL CPU SUPPLEMENT: EXECUTE', f'CPU set: {meta["cpu_set"]}', f'Partitions: {meta["partitions"]}',
             f'repository_commit={meta["repository_commit"]}', f'binary_sha256={meta["binary_sha256"]}']
    for label in review.ORDER:
        lines += ['ROUND '+label, shlex.join(meta['commands'][label]['argv'])]
    for run in recorded['rounds']:
        label, r, w, a = run['round_id'], run['report'], run['whole_run'], run['arrival']
        resource, work = run['resources'], run['work']
        lines += ['START ROUND '+label, 'CPU_CAPTURE '+json.dumps(run['cpu_capture']),
                  'CPU_SCOPE '+json.dumps(run['cpu_scope']), 'CPU_SERIES_BEGIN '+','.join(review.SERIES_FIELDS)]
        for row in cpu[label]:
            stream = io.StringIO()
            csv.writer(stream, lineterminator='').writerow([row[k] for k in review.SERIES_FIELDS])
            lines.append(stream.getvalue())
        lines += ['CPU_SERIES_END', 'CPU groups (cluster): '+json.dumps(run['cpu_groups']),
                  f'| demo | {r["model"]}/{r["policy"]}/{r["continuation"]} | {r["completed"]}/{r["offered"]} | {r["mean_ms"]:.3f} | {r["p95_ms"]:.3f} | {r["p99_ms"]:.3f} | {r["task_wait_p99_ms"]:.3f} | {r["operator_cpu_mean_ms"]:.3f}/{r["operator_wall_mean_ms"]:.3f} | {r["migrations"]}/{r["tasks"]} | {r["cross_numa"]} |',
                  f'  seed={resource["seed"]}, pool_count={resource["pool_count"]}, affinity={resource["affinity"]}, threads/pool={resource["threads_per_pool"]}, whole completed/rejected-or-failed={w["completed"]}/{w["rejected_or_failed"]}, operator CPU/request ms={w["operator_cpu_per_request_ms"]:.3f}',
                  f'  WHOLE RUN (not cohort): process CPU s={w["process_cpu_seconds"]:.6f}, process CPU/completed-request ms={w["process_cpu_per_request_ms"]:.3f}, peak_inflight={w["peak_inflight"]}, submission lag p99 ms={w["submission_lag_p99_ms"]:.3f}',
                  f'  work_signature_sha256_prefix={work["work_signature_sha256_prefix"]}, arrival_hash={work["arrival_hash"]}, checksum={work["checksum"]}, pages='+json.dumps(work['pages']),
                  f'  fixed_work_sha256_prefix={work["fixed_work_sha256_prefix"]}; equal fixed work does not imply equal arrival schedules or equal CPU service time.',
                  f'  arrival={a["kind"]}, burst size/spacing us={a["burst_size_spacing_us"]}, whole planned iterations/accesses={a["whole_iterations"]}/{a["whole_accesses"]}, per-request mean={a["per_request_mean_iterations"]}/{a["per_request_mean_accesses"]}',
                  '  WALL-WINDOW callback occupancy '+run['callback_occupancy_text'],
                  'Arrival='+run['flow_arrival_text'],
                  f'Completed means ms: planned latency={a["mean_planned_latency_ms"]:.3f} = attempt lag {a["mean_attempt_lag_ms"]:.3f} + attempt-to-finish {a["mean_attempt_to_finish_ms"]:.3f} (rounding may differ).']
        for role in run['roles']:
            reasons = ', '.join(f'{key}:{value}' for key, value in role['reasons'].items())
            lines.append(f'| {role["role"]} | {role["n"]} | {role["changed_pool"]} | {role["back_to_home"]} | {role["wait_mean_ms"]:.3f} | {role["cpu_mean_ms"]:.3f} | {role["wall_mean_ms"]:.3f} | {reasons} |')
        lines += ['SAME_WORK_AUDIT: '+run['same_work_audit'], review.ROLE_HEADER]
        for role in run['role_csv']:
            lines.append(f'{role["role"]},{role["n"]},{role["changed_pool"]},{role["changed_pool_percent"]:.6f},{role["wait_mean_ms"]:.6f}')
        lines += ['END ROUND '+label]
    lines.append(next(line for line in lines if line.startswith('| demo |')))
    return ('\n'.join(lines)+'\n').encode()


class TranscriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = fixture()

    def test_six_blocks_only_and_original_line_provenance(self):
        summary, rows = review.extract(self.data, 'fixture.log')
        self.assertEqual(summary['audit']['cpu_rows'], 5724)
        self.assertEqual(summary['source']['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual([x['round_id'] for x in summary['rounds']], review.ORDER)
        self.assertEqual(summary['rounds'][0]['report']['p99_ms'], 146.012)
        lines = self.data.decode().splitlines()
        self.assertTrue(lines[rows[0]['source_line']-1].startswith('2071.26076,'))
        self.assertEqual(summary['rounds'][-1]['audit']['status'], 'passed')

    def test_duplicate_inside_block_rejected_outside_is_ignored(self):
        line = next(x for x in self.data.splitlines() if x.startswith(b'| demo |'))
        damaged = self.data.replace(line, line+b'\n'+line, 1)
        with self.assertRaisesRegex(review.EvidenceError, 'report row: expected one'):
            review.extract(damaged)

    def test_truncated_block_rejected(self):
        with self.assertRaisesRegex(review.EvidenceError, 'truncated'):
            review.extract(self.data.replace(b'END ROUND 06-home', b'', 1))

    def test_missing_cluster_row_rejected(self):
        lines = self.data.splitlines()
        at = next(i for i, x in enumerate(lines) if x.startswith(b'CPU_SERIES_BEGIN'))
        del lines[at+1]
        with self.assertRaisesRegex(review.EvidenceError, 'incomplete CPU groups'):
            review.extract(b'\n'.join(lines))

    def test_wrong_utilization_rejected(self):
        damaged = self.data.replace(b'1.802848,11.267802', b'1.802848,21.267802', 1)
        self.assertNotEqual(damaged, self.data)
        with self.assertRaisesRegex(review.EvidenceError, 'CPU/util'):
            review.extract(damaged)

    def test_process_field_cannot_differ_by_cluster(self):
        damaged = self.data.replace(b',14.848577,,', b',15.848577,,', 1)
        self.assertNotEqual(damaged, self.data)
        with self.assertRaisesRegex(review.EvidenceError, 'inconsistent process_cpu_equivalent'):
            review.extract(damaged)

    def test_scope_mean_corruption_rejected(self):
        damaged = self.data.replace(b'17.912955271492663', b'18.912955271492663', 1)
        self.assertNotEqual(damaged, self.data)
        with self.assertRaisesRegex(review.EvidenceError, 'weighted CPU mean'):
            review.extract(damaged)

    def test_exact_role_csv_corruption_rejected(self):
        damaged = self.data.replace(b'first-wave stage0,15360,0,0.000000', b'first-wave stage0,15361,0,0.000000', 1)
        with self.assertRaisesRegex(review.EvidenceError, 'role CSV/table disagree'):
            review.extract(damaged)

    def test_same_work_marker_required(self):
        damaged = self.data.replace(b'SAME_WORK_AUDIT: resources, arrival, request checksums and operator work match first home',
                                    b'SAME_WORK_AUDIT: failed', 1)
        with self.assertRaisesRegex(review.EvidenceError, 'same-work audit absent'):
            review.extract(damaged)

    def test_cross_round_checksum_change_rejected(self):
        damaged = self.data.replace(b'checksum=15104970427462885364', b'checksum=15104970427462885365', 1)
        with self.assertRaisesRegex(review.EvidenceError, 'checksum mismatch'):
            review.extract(damaged)

    def test_wrong_order_rejected(self):
        damaged = self.data.replace(b'START ROUND 01-home', b'START ROUND 09-home', 1).replace(
            b'END ROUND 01-home', b'END ROUND 09-home', 1)
        with self.assertRaisesRegex(review.EvidenceError, 'fixed order'):
            review.extract(damaged)


if __name__ == '__main__':
    unittest.main()
