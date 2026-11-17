#!/usr/bin/env python3
"""Validate the offline report's reviewed evidence and generated deliverables.

No target CSVs or performance tests are required. Missing publication artifacts
are allowed only without --require-artifacts, while they are still being built.
"""
import argparse
import base64
import hashlib
import json
import math
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ERRORS = []
CHECKS = Counter()


def check(condition, message, category="data"):
    CHECKS[category] += 1
    if not condition:
        ERRORS.append(message)


def read_json(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same(a, b):
    if isinstance(a, (float, int)) and isinstance(b, (float, int)):
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-10)
    return a == b


def pointer(document, path):
    value = document
    for token in path.strip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def review_rows(path):
    """Read published home/fork/all mean/p99 and optional whole-CPU tables."""
    rows = {}
    for line in path.read_text().splitlines():
        if not re.match(r"\|\s*42[012]\s*\|", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        seed = int(cells[0])
        if len(cells) == 4 and all(re.fullmatch(r"[\d.]+\s*/\s*[\d.]+", c) for c in cells[1:]):
            for strategy, cell in zip(["home", "fork-parent", "all-parent"], cells[1:]):
                mean, p99 = map(float, cell.split("/"))
                rows.setdefault((seed, strategy), {}).update(mean_ms=mean, p99_ms=p99)
        elif len(cells) == 5 and all(re.fullmatch(r"[\d.]+", c) for c in cells[1:4]):
            for strategy, cell in zip(["home", "fork-parent", "all-parent"], cells[1:4]):
                cpu = float(cell)
                rows.setdefault((seed, strategy), {}).update(process_cpu_s=cpu, cpu_per_request_ms=cpu * 1000 / 240)
    return rows


def validate_comparisons():
    d = read_json(HERE / "data/comparisons.json")
    documents = {}
    expected_ids = set()
    expected_by_batch = {}
    historical = {}
    for batch, source in d["sources"].items():
        path = ROOT / source["file"]
        check(path.is_file(), f"Missing source {path}", "sources")
        if not path.is_file():
            continue
        check(digest(path) == source["sha256"], f"Stale source hash: {source['file']}", "sources")
        if path.suffix == ".json":
            original = read_json(path)
            documents[source["file"]] = original
            raw_runs = original["runs"]
            indexes = raw_runs.keys() if isinstance(raw_runs, dict) else range(len(raw_runs))
            ids = {f"{batch}:{i}" for i in indexes}
            check(source["upstream_log_sha256"] == original.get("source_sha256"), f"Wrong upstream log hash: {batch}", "sources")
        else:
            historical[batch] = review_rows(path)
            ids = {f"{batch}:{i}" for i in range(len(historical[batch]))}
        expected_ids.update(ids)
        expected_by_batch[batch] = len(ids)
    rows = d["runs"]
    idx = {r["id"]: r for r in rows}
    check(len(idx) == len(rows), "Duplicate run IDs")
    check(set(idx) == expected_ids, f"Source run coverage differs: missing={expected_ids-set(idx)}, extra={set(idx)-expected_ids}")
    check(Counter(r["batch"] for r in rows) == Counter(expected_by_batch), "Run counts per source differ")
    for r in rows:
        label = r["id"]
        check(r["cpu_utilization_percent"] is None, f"Invented CPU utilization: {label}")
        check(r["cohort_completed"] == r["cohort_offered"], f"Cohort completion mismatch: {label}")
        check(r["raw_target_csv_available"] is False, f"Unfounded raw CSV claim: {label}")
        check(r["cross_batch_causal_comparison"] is False, f"Cross-batch causal claim: {label}")
        original = documents.get(r["source_file"])
        if original is not None:
            raw = pointer(original, r["source_pointer"])
            source_contract = original.get("common_cli_contract", {}).copy()
            source_contract.update(raw.get("cli_options", {}))
            source_contract.update(raw.get("options", {}))
            for key, value in source_contract.items():
                check(r["contract"].get(key) == value, f"Contract mismatch: {label}.{key}")
            for field, flag, convert in [("threads", "--threads", int), ("trace", "--trace", int), ("qps", "--qps", float), ("whole_completed", "--requests", int)]:
                if flag in source_contract:
                    check(r[field] == convert(source_contract[flag]), f"Wrong source contract dimension: {label}.{field}")
            if "--arrival" in source_contract:
                check(r["arrival"] == source_contract["--arrival"], f"Wrong arrival kind: {label}")
            for name, path in r["metric_sources"].items():
                if path is None:
                    check(r[name] is None, f"Non-null metric without source: {label}.{name}")
                    continue
                try:
                    actual = pointer(original, path)
                    check(same(r[name], actual), f"Metric mismatch: {label}.{name}: {r[name]} != {actual}")
                except (KeyError, IndexError, ValueError, TypeError) as e:
                    check(False, f"Bad source pointer {label}.{name}: {path}: {e}")
            if r["batch"] == "4018":
                check(r["scenario"] == raw["cell"].split("-")[0].upper(), f"Wrong async scenario: {label}")
                check(r["trace"] == int(raw["overrides"]["trace"]), f"Wrong async trace: {label}")
                check(r["policy"] == raw["overrides"]["policy"], f"Wrong async policy: {label}")
            elif r["batch"] == "4020":
                check(r["trace"] == int(raw["options"]["--trace"]), f"Wrong A1 trace: {label}")
                check(r["policy"] == raw["options"]["--policy"], f"Wrong A1 policy: {label}")
            else:
                check(r["seed"] == raw["seed"], f"Wrong seed: {label}")
                if "threads" in raw:
                    check(r["threads"] == raw["threads"], f"Wrong threads: {label}")
                if "policy" in raw:
                    check(r["policy"] == raw["policy"], f"Wrong policy: {label}")
                if "strategy" in raw:
                    check(r["strategy"] == raw["strategy"], f"Wrong strategy: {label}")
                if "trace" in raw:
                    check(r["trace"] == raw["trace"], f"Wrong trace: {label}")
        else:
            raw = historical[r["batch"]][(r["seed"], r["strategy"])]
            for name in ["mean_ms", "p99_ms", "process_cpu_s", "cpu_per_request_ms"]:
                check(same(r.get(name), raw.get(name)), f"Historical table mismatch: {label}.{name}")
        if r.get("process_cpu_s") is not None and r.get("cpu_per_request_ms") is not None:
            recomputed = r["process_cpu_s"] * 1000 / r["whole_completed"]
            check(abs(recomputed-r["cpu_per_request_ms"]) < 0.00051, f"CPU denominator mismatch: {label}")

    # Explicitly preserve the two home trace1 cells and all six trace0 cells;
    # there was no O trace1 in the eight-cell pilot, only in the later A1 batch.
    actual_async = {(r["scenario"], r["policy"], r["trace"]) for r in rows if r["batch"] == "4018"}
    expected_async = {(s, p, 0) for s in ["D0", "A0", "A1"] for p in ["home", "outstanding"]} | {("A0", "home", 1), ("A1", "home", 1)}
    check(actual_async == expected_async, "4018 missing/extra async cells")

    expected_pairs = set()
    for c in rows:
        for b in rows:
            if c["id"] == b["id"] or (c["batch"], c["scenario"], c["seed"]) != (b["batch"], b["scenario"], b["seed"]):
                continue
            if c["threads"] == b["threads"] and c["trace"] == b["trace"]:
                if c["policy"] != "home" and b["policy"] == "home":
                    expected_pairs.add(("same_batch_policy_minus_home", c["id"], b["id"]))
                if c["strategy"] == "all-parent" and b["strategy"] == "fork-parent":
                    expected_pairs.add(("same_batch_all_minus_fork", c["id"], b["id"]))
            if c["strategy"] == b["strategy"]:
                if c["threads"] == b["threads"] and c["trace"] == 1 and b["trace"] == 0:
                    expected_pairs.add(("same_batch_trace1_minus_trace0", c["id"], b["id"]))
                if c["trace"] == b["trace"] and c["threads"] == 16 and b["threads"] == 128:
                    expected_pairs.add(("same_batch_16_minus_128_threads", c["id"], b["id"]))
    actual_pairs = set()
    for p in d["paired_comparisons"]:
        identity = (p["kind"], p["candidate_id"], p["baseline_id"])
        check(identity not in actual_pairs, f"Duplicate comparison: {identity}", "pairs")
        actual_pairs.add(identity)
        c, b = idx[p["candidate_id"]], idx[p["baseline_id"]]
        for name in ["batch", "scenario", "seed", "threads", "trace"]:
            check(p[name] == c[name], f"Pair metadata mismatch: {identity}.{name}", "pairs")
        check(p["candidate_strategy"] == c["strategy"] and p["baseline_strategy"] == b["strategy"], f"Pair strategy mismatch: {identity}", "pairs")
        for name in ["mean_ms", "p95_ms", "p99_ms", "cpu_per_request_ms"]:
            value = p["metrics"][name]
            cv, bv = c.get(name), b.get(name)
            delta = cv-bv if cv is not None and bv is not None else None
            percent = 100*(cv/bv-1) if cv is not None and bv not in [None, 0] else None
            for field, expected in [("candidate", cv), ("baseline", bv), ("delta", delta), ("delta_percent", percent)]:
                check(same(value[field], expected), f"Pair arithmetic mismatch: {identity}.{name}.{field}", "pairs")
    check(actual_pairs == expected_pairs, f"Comparison coverage differs: missing={expected_pairs-actual_pairs}, extra={actual_pairs-expected_pairs}", "pairs")

    seen = set()
    for m in d["mechanism_observations"]:
        identity = (m["run_id"], m["kind"])
        check(identity not in seen, f"Duplicate mechanism {identity}", "mechanisms")
        seen.add(identity)
        r = idx[m["run_id"]]
        check(m["source_file"] == r["source_file"], f"Wrong mechanism source: {identity}", "mechanisms")
        check(m["source_pointer"] == r["source_pointer"]+"/"+m["kind"], f"Wrong mechanism pointer: {identity}", "mechanisms")
        check(pointer(documents[m["source_file"]], m["source_pointer"]) == m["values"], f"Changed mechanism values: {identity}", "mechanisms")
    mechanism_fields = {
        "4013": ["roles", "first_fork", "joint_load", "pool_callback_occupancy", "all_callback_occupancy_mean", "all_callback_occupancy_peak"],
        "4015": ["roles", "first_fork", "joint_load", "pool_callback_occupancy", "all_callback_occupancy_mean", "all_callback_occupancy_peak"],
        "4018": ["trace1_cohort_async_stages", "trace1_cohort_business_operators", "trace1_cohort_rpc_resume", "async_source_whole"],
        "4020": ["flow", "source", "callback_occupancy_printed"],
    }
    expected_mechanisms = set()
    for r in rows:
        if r["batch"] not in mechanism_fields:
            continue
        raw = pointer(documents[r["source_file"]], r["source_pointer"])
        expected_mechanisms.update((r["id"], k) for k in mechanism_fields[r["batch"]] if raw.get(k) is not None)
    check(seen == expected_mechanisms, "Mechanism coverage differs", "mechanisms")
    print(f"Reviewed evidence: {len(rows)} runs from {len(expected_by_batch)} sources, {len(actual_pairs)} pairs, {len(seen)} mechanisms")


def validate_scenarios():
    """Audit new scenario fields against retained source rows, independently of the builder."""
    d = read_json(HERE / 'data/scenario_results.json')
    base = read_json(HERE / 'data/comparisons.json')
    selected = {'4011', '4012', '4013', '4014', '4015', '4016', '4018', '4020'}
    originals = {r['id']: r for r in base['runs'] if r['batch'] in selected}
    documents = {}
    for source in d['sources'].values():
        relative = Path(source['file'])
        path = ROOT / relative
        check(not relative.is_absolute() and path.resolve().is_relative_to(ROOT),
              f'Nonportable scenario source: {relative}', 'scenarios')
        check(path.is_file(), f'Missing scenario source: {relative}', 'scenarios')
        if not path.is_file():
            continue
        check(digest(path) == source['sha256'], f'Stale scenario source: {relative}', 'scenarios')
        documents[source['file']] = read_json(path)
    extra_file = 'reports/final_review/data/4011_routing_evidence.json'
    extra = documents[extra_file]
    summary = documents['results/review/4011_summary.json']
    check(extra['source_log'] == '4011.log', 'Wrong retained routing log name', 'scenarios')
    check(extra['source_sha256'] == summary['source_sha256']['4011.log'],
          'Routing excerpt original SHA differs from reviewed summary', 'scenarios')
    check(extra['source_summary_file'] == 'results/review/4011_summary.json',
          'Wrong routing excerpt summary link', 'scenarios')
    check(len(extra['runs']) == 9, 'Missing routing excerpt cells', 'scenarios')
    # Parse the retained literal table cells rather than trust newly normalized values.
    extra_parsed = {}
    excerpt_line_numbers = []
    for name, source in extra['runs'].items():
        values = [v.strip() for v in source['request_summary']['text'].strip('|').split('|')]
        check(values[0] == name and name.endswith('trace1'), f'Wrong routing row identity: {name}', 'scenarios')
        check('/' + name + ':' in source['run_header']['text'], f'Wrong routing run context: {name}', 'scenarios')
        check(source['run_header']['line'] < source['role_rows'][0]['line'],
              f'Role lines precede routing run header: {name}', 'scenarios')
        excerpt_line_numbers.extend([source['request_summary']['line'], source['run_header']['line']])
        count, offered = map(int, values[2].split('/'))
        cpu, wall = map(float, values[7].split('/'))
        migrated, callbacks = map(int, values[8].split('/'))
        expected = dict(mean_ms=float(values[3]), p99_ms=float(values[5]),
                        cohort_completed=count, cohort_offered=offered,
                        task_wait_p99_ms=float(values[6]), operator_cpu_mean_ms=cpu,
                        operator_wall_mean_ms=wall, cohort_migrations=migrated,
                        cohort_callbacks=callbacks, cohort_cross_numa=int(values[9]))
        expected_roles = []
        for row in source['role_rows']:
            excerpt_line_numbers.append(row['line'])
            c = [v.strip() for v in row['text'].strip('|').split('|')]
            expected_roles.append({
                'role': c[0], 'n': int(c[1]), 'source_ne_chosen': int(c[2]),
                'source_not_home_chosen_home': int(c[3]), 'wait_mean_ms': float(c[4]),
                'cpu_mean_ms': float(c[5]), 'wall_mean_ms': float(c[6]),
                'routing_reasons': {key: int(value) for key, value in
                                    (item.strip().split(':') for item in c[7].split(','))},
                'source_line': row['line'],
            })
        check(len(expected_roles) == 5, f'Wrong routing role count: {name}', 'scenarios')
        extra_parsed[name] = (expected, expected_roles)
    check(all(isinstance(n, int) and n > 0 for n in excerpt_line_numbers),
          'Invalid original log line number', 'scenarios')
    check(len(excerpt_line_numbers) == len(set(excerpt_line_numbers)),
          'Duplicate original routing excerpt line numbers', 'scenarios')

    rows = d['runs']
    indexed = {r['id']: r for r in rows}
    check(len(indexed) == len(rows) == 79, 'Wrong scenario run count or duplicate IDs', 'scenarios')
    check(set(indexed) == set(originals), 'Scenario coverage differs from reviewed source selection', 'scenarios')
    for r in rows:
        label = r['id']
        b = originals[label]
        overridden = set()
        if r['batch'] == '4011' and r['trace']:
            overridden.update(['task_wait_p99_ms', 'operator_cpu_mean_ms', 'operator_wall_mean_ms'])
        if r['batch'] == '4020' and r['trace']:
            overridden.update(['task_wait_p99_ms', 'operator_cpu_mean_ms'])
        for field, value in b.items():
            if field not in overridden:
                check(same(r.get(field), value), f'Scenario changes reviewed base field: {label}.{field}', 'scenarios')
        raw = pointer(documents[r['source_file']], r['source_pointer'])
        s = d['scenarios'][r['scenario_id']]
        for field, expected in [('qps', s['qps']), ('arrival', s['arrival']),
                                ('whole_completed', s['whole_requests']),
                                ('cohort_completed', s['cohort_requests'])]:
            check(r[field] == expected, f'Scenario contract mismatch: {label}.{field}', 'scenarios')
        check(r['threads'] in s['threads'], f'Scenario thread budget mismatch: {label}', 'scenarios')
        for field, flag in [('iterations_base', '--iterations'), ('accesses_base', '--accesses')]:
            if flag in r['contract']:
                check(s[field] == int(r['contract'][flag]), f'Work units mismatch: {label}.{field}', 'scenarios')
        if s['arrival'] == 'regular':
            check(same(s['request_interval_ms'], 1000 / s['qps']), f'Arrival interval mismatch: {label}', 'scenarios')
        else:
            check(same(s['batch_period_ms'], 1000 * s['batch_size'] / s['qps']), f'Burst period mismatch: {label}', 'scenarios')
            check(s['nominal_batch_span_us'] == (s['batch_size']-1) * s['within_batch_spacing_us'],
                  f'Burst span mismatch: {label}', 'scenarios')
        # Missing instrumentation never silently becomes zero or utilization.
        check(r['real_time_cpu_utilization'] is None and r['cpu_utilization_percent'] is None,
              f'Invented CPU utilization: {label}', 'scenarios')
        if '--cpu-sampling' in r['contract']:
            check(r['contract']['--cpu-sampling'] == '0', f'Unexpected sampled target run: {label}', 'scenarios')
        if r['trace'] == 0:
            check(r['roles'] is None and r['cohort_callbacks'] is None and
                  r['cohort_migrations'] is None and r['cohort_migration_rate_percent'] is None,
                  f'Invented trace0 migration/role evidence: {label}', 'scenarios')
        if r['batch'] == '4018':
            source = r['whole_process_average_cpu_source']
            check(source['file'] == r['source_file'], f'CPU denominator source differs: {label}', 'scenarios')
            check(same(pointer(documents[source['file']], source['cpu_pointer']), r['process_cpu_s']),
                  f'CPU source pointer mismatch: {label}', 'scenarios')
            check(same(pointer(documents[source['file']], source['wall_pointer']), r['whole_duration_with_drain_s']),
                  f'Wall source pointer mismatch: {label}', 'scenarios')
            check(source['formula'] is None and r['whole_process_average_cpu_percent_of_96'] is None,
                  f'Used mismatched CPU/wall endpoints as utilization: {label}', 'scenarios')

        expected_roles = raw.get('roles')
        if r['batch'] == '4011' and r['trace']:
            name = f"seed-{r['seed']}-{r['strategy']}-trace1"
            expected, expected_roles = extra_parsed[name]
            for field, value in expected.items():
                check(same(r[field], value), f'Routing excerpt metric mismatch: {label}.{field}', 'scenarios')
            source = r['additional_metric_source']
            check(source['file'] == extra_file and source['pointer'] == f'/runs/{name}/request_summary',
                  f'Routing row pointer mismatch: {label}', 'scenarios')
            check(source['original_log_sha256'] == extra['source_sha256'] and
                  source['original_line'] == extra['runs'][name]['request_summary']['line'],
                  f'Routing excerpt provenance mismatch: {label}', 'scenarios')
        elif r['batch'] == '4020' and r['trace']:
            expected_roles = raw['flow']['roles']
            check(r['task_wait_p99_ms'] == raw['cohort']['task_wait_p99_ms'], f'A1 task wait mismatch: {label}', 'scenarios')
            business_roles = ['first-wave stage0', 'later-wave stage0', 'ordinary successors']
            weighted = sum(x['n'] * x['cpu_mean_ms'] for x in expected_roles if x['role'] in business_roles)
            check(same(r['operator_cpu_mean_ms'], weighted/(168*r['cohort_completed'])),
                  f'Weighted operator CPU mismatch: {label}', 'scenarios')
        else:
            for field in ['cohort_migrations', 'cohort_callbacks', 'cohort_cross_numa']:
                check(r[field] == raw.get(field), f'Changed source count: {label}.{field}', 'scenarios')
        if expected_roles is None:
            check(r['roles'] is None and r['roles_source_file'] is None and r['roles_source_pointer'] is None,
                  f'Invented roles/source pointer: {label}', 'scenarios')
            continue
        actual_roles = [{k: v for k, v in role.items() if k != 'migration_percent_derived'} for role in r['roles']]
        check(actual_roles == expected_roles, f'Role source values differ: {label}', 'scenarios')
        check(r['roles_source_file'] in documents and bool(r['roles_source_pointer']),
              f'Missing role source: {label}', 'scenarios')
        if r['roles_source_file'] != extra_file:
            check(pointer(documents[r['roles_source_file']], r['roles_source_pointer']) == expected_roles,
                  f'Role source pointer mismatch: {label}', 'scenarios')
        else:
            check(r['roles_source_pointer'] == f'/runs/{name}/role_rows', f'Role excerpt pointer mismatch: {label}', 'scenarios')
        callbacks = sum(row['n'] for row in r['roles'])
        migrations = sum(row['source_ne_chosen'] for row in r['roles'])
        check(callbacks == r['cohort_callbacks'] and migrations == r['cohort_migrations'],
              f'Role count/migration conservation failure: {label}', 'scenarios')
        resumes = sum(row['n'] for row in r['roles'] if row['role'] == 'rpc_resume')
        check(callbacks == 171*r['cohort_completed'] + resumes, f'Graph callback denominator mismatch: {label}', 'scenarios')
        check(same(r['cohort_migration_rate_percent'], 100*migrations/callbacks),
              f'Wrong total migration ratio: {label}', 'scenarios')
        for role in r['roles']:
            check(0 <= role['source_ne_chosen'] <= role['n'], f'Invalid role migration bounds: {label}', 'scenarios')
            check(same(role['migration_percent_derived'], 100*role['source_ne_chosen']/role['n']),
                  f'Wrong role migration ratio: {label}/{role["role"]}', 'scenarios')
            check(sum(role['routing_reasons'].values()) == role['n'], f'Role chooser reasons do not conserve: {label}', 'scenarios')
    check(d['common']['business_operators_per_request'] == 80*2+4*2 and
          d['common']['base_callbacks_per_request'] == 1+80*2+1+4*2+1,
          'Common graph contract mismatch', 'scenarios')
    audit = d['cpu_time_series_audit']
    check(audit['cpu_sampling_enabled'] is False and
          audit['runtime_series_recoverable_from_existing_selected_target_csv'] is False and
          audit['existing_summary_average_cpu_possible'] is False,
          'Unsupported CPU time-series/average recovery claim', 'scenarios')
    print(f'Historical scenario evidence: {len(rows)} runs, {len(extra_parsed)} retained routing excerpts, original CPU series explicitly unavailable')


def validate_cpu_supplement(source_log=None):
    """Keep the new CPU supplement separate from the 97 historical run summaries."""
    from cpu_window_metrics import derive, load_rounds

    category = 'cpu_supplement'

    def audit(condition, message):
        check(condition, '4021: ' + message, category)

    def close(actual, expected, message, tolerance=1e-6):
        audit(isinstance(actual, (float, int)) and isinstance(expected, (float, int)) and
              math.isfinite(actual) and math.isfinite(expected) and
              abs(actual - expected) <= tolerance, message)

    def quantile(values, p):
        return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]

    path = ROOT / 'results/review/4021_summary.json'
    audit(path.is_file(), 'summary missing')
    if not path.is_file():
        return
    data = read_json(path)
    source = data['source']
    audit(source['name'] == '4021.log' and bool(re.fullmatch(r'[0-9a-f]{64}', source['sha256'])),
          'missing or invalid original log digest')
    audit(source['bytes'] > 0 and source['line_count'] > 0, 'missing original log extent')
    # The retained report is portable: an original attachment is optional. When
    # explicitly supplied, verify it without storing its machine-specific path.
    if source_log is not None:
        audit(source_log.is_file(), 'optional original log not found')
        if source_log.is_file():
            audit(digest(source_log) == source['sha256'], 'original log digest differs')
            audit(source_log.stat().st_size == source['bytes'], 'original log byte count differs')
            audit(len(source_log.read_text().splitlines()) == source['line_count'],
                  'original log line count differs')
    csv_source = data['cpu_windows_file']
    csv_name = Path(csv_source['name'])
    audit(csv_name.name == str(csv_name) and csv_name.suffix == '.csv', 'CSV path is not a local basename')
    csv_path = path.parent / csv_name
    audit(csv_path.is_file(), 'CPU windows CSV missing')
    if not csv_path.is_file():
        return
    audit(digest(csv_path) == csv_source['sha256'], 'CPU windows CSV digest differs')
    grouped = load_rounds(csv_path)
    expected_ids = ['01-home', '02-queue', '03-outstanding', '04-outstanding', '05-queue', '06-home']
    expected_policies = ['home', 'queue', 'outstanding', 'outstanding', 'queue', 'home']
    rounds = data['rounds']
    audit([r['round_id'] for r in rounds] == expected_ids, 'six-round sequence differs')
    audit([r['policy'] for r in rounds] == expected_policies, 'six-round policies differ')
    audit(set(grouped) == set(expected_ids), 'CPU CSV round coverage differs')
    audit(data['audit']['status'] == 'passed' and data['audit']['executed_rounds'] == 6,
          'upstream six-round audit is not successful')
    row_count = sum(len(rows) for rows in grouped.values())
    audit(row_count == csv_source['rows'] == data['audit']['cpu_rows'] == 5724,
          'total CPU CSV row count differs')
    metadata = data['metadata']
    audit(metadata['policies'] == ','.join(expected_policies), 'declared policy sequence differs')
    audit(bool(re.fullmatch(r'[0-9a-f]{40}', metadata['repository_commit'])), 'missing repository revision')
    audit(bool(re.match(r'[0-9a-f]{64}\s+', metadata['binary_sha256'])), 'missing binary digest')
    common = metadata['common_demo_options']
    contract = dict(affinity='cluster', threads='16', capacity_scale='1', requests='240',
                    qps='12', arrival='regular', widths='80,4', depths='2,2',
                    iterations='3000000', accesses='100000', model='branch-rw',
                    branch_kb='64', shared_kb='0', jitter_percent='25', long_every='8',
                    long_factor='4', work_scale_scope='task', estimate_mode='constant',
                    home_policy='rr', continuation='parent', balance_at='all', batch_chunk='1',
                    busy_high='1', return_low='0.5', min_gain='0', cross_numa='0',
                    cross_numa_penalty='0', max_hops='4', max_outstanding='0',
                    cpu_sampling='0', trace='1', page_sample='0', max_inflight='16',
                    memory_mb='512', seed='421', sync_block_percent='0', sync_block_us='0',
                    async_percent='0', async_delay_us='0', async_align_us='0', async_capacity='4096')
    for key, value in contract.items():
        audit(common.get(key) == value, 'changed workload/resource contract: ' + key)
    partitions = [list(range(0, 16)), list(range(16, 32)), list(range(32, 48)),
                  list(range(192, 208)), list(range(208, 224)), list(range(224, 240))]
    approved_cpus = [cpu for partition in partitions for cpu in partition]
    audit(metadata['cpu_set'] == '0-47,192-239', 'wrong selected CPU set')
    audit(metadata['partitions'] == common['cpus'] == '0-15;16-31;32-47;192-207;208-223;224-239',
          'wrong cluster partitions')
    role_counts = {'init': 192, 'first-wave stage0': 15360, 'later-wave stage0': 768,
                   'ordinary successors': 16128, 'join': 384}
    first_work = rounds[0]['work']
    for run in rounds:
        label = run['round_id']
        rows = grouped.get(label, [])
        audit(bool(rows), label + ': missing CPU rows')
        if not rows:
            continue
        metrics = derive(rows)
        scope, capture = run['cpu_scope'], run['cpu_capture']
        report, whole = run['report'], run['whole_run']
        commands = metadata['commands'][label]
        demo, capture_options = commands['demo_options'], commands['capture_options']
        audit({k: v for k, v in demo.items() if k not in ['policy', 'output']} == common,
              label + ': command differs from common workload')
        audit(demo['policy'] == run['policy'] == report['policy'], label + ': command policy differs')
        argv = commands['argv']
        audit('--' in argv, label + ': launch delimiter missing')
        split = argv.index('--')
        for options, tokens in [(capture_options, argv[:split]), (demo, argv[split+1:])]:
            for key, value in options.items():
                flag = '--' + key.replace('_', '-')
                audit(flag in tokens and tokens.index(flag) + 1 < len(tokens) and
                      tokens[tokens.index(flag)+1] == value, label + ': normalized option differs: ' + flag)
        for key, value in dict(partitions=metadata['partitions'], interval_ms='100',
                               timeout_seconds='120', warmup_seconds='2', window_seconds='16').items():
            audit(capture_options[key] == value, label + ': capture option differs: ' + key)
        audit(capture['status'] == 'complete' and capture['child_returncode'] == 0 and
              capture['exit_reason'] == 'completed' and capture['capture_errors'] == [] and
              capture['summary_error'] is None, label + ': unsuccessful capture')
        audit(capture['controller_allowed_cpus'] == approved_cpus, label + ': collector CPU mask differs')
        audit(capture['interval_ms'] == 100 and capture['clock_ticks_per_second'] == 100,
              label + ': capture interval/tick contract differs')
        audit(0 <= capture['collector_sampling_cpu_seconds'] <= capture['collector_cpu_seconds'],
              label + ': invalid collector CPU accounting')
        audit(scope['available'] and scope['warmup_seconds'] == 2 and scope['window_seconds'] == 16,
              label + ': formal scope differs')
        audit(scope['scope_start_ns'] == scope['origin_ns'] + 2_000_000_000 and
              scope['scope_end_ns'] == scope['origin_ns'] + 18_000_000_000,
              label + ': formal scope origin mismatch')
        audit(metrics['row_count'] == run['audit']['cpu_rows'] == run['derived']['cpu_windows_csv_rows'] == 954,
              label + ': per-round CPU row count differs')
        audit(metrics['window_count'] == metrics['valid_window_count'] ==
              run['audit']['complete_six_cluster_windows'] == 159 and metrics['invalid_window_count'] == 0,
              label + ': incomplete/invalid simultaneous CPU windows')
        audit(metrics['first_start_ms'] >= 2000 and metrics['last_end_ms'] <= 18000,
              label + ': CPU windows outside formal scope')
        close(metrics['valid_seconds'], (metrics['last_end_ms'] - metrics['first_start_ms']) / 1000,
              label + ': nonconsecutive CPU windows', 1e-7)
        close(metrics['global_mean_percent'], run['derived']['selected_96_cpu_util_time_weighted_mean_percent'],
              label + ': global CPU percentage differs')
        close(metrics['global_mean_cpu_equiv'], run['derived']['cluster_cpu_equivalent_total_time_weighted_mean'],
              label + ': global CPU equivalents differ')
        audit(len(scope['clusters']) == len(run['cpu_groups']) == 6, label + ': cluster count differs')
        for index, cluster in enumerate(scope['clusters']):
            cpu_group = run['cpu_groups'][index]
            audit(cluster['cluster'] == index and cluster['cpus'] == cpu_group['cpus'] == partitions[index],
                  label + ': cluster CPU membership differs')
            audit(cpu_group['capacity'] == 16 and cpu_group['numa'] == (0 if index < 3 else 2),
                  label + ': cluster capacity/NUMA differs')
            selected = [r for r in rows if int(r['cluster']) == index]
            audit(len(selected) == cluster['complete_windows'] == cluster['valid_windows'] == 159,
                  label + ': cluster window count differs')
            audit(cluster['invalid_reasons'] == {} and cluster['invalid_seconds'] == 0 and
                  cluster['outside_nominal_windows'] == 0, label + ': invalid cluster summary')
            durations = [(float(r['relative_end_ms']) - float(r['relative_start_ms'])) / 1000 for r in selected]
            util = [float(r['util_percent']) for r in selected]
            close(sum(durations), cluster['valid_seconds'], label + ': cluster valid duration differs', 1e-7)
            close(100 * sum(durations) / 16, cluster['valid_coverage_percent'],
                  label + ': cluster coverage differs')
            close(sum(t * u for t, u in zip(durations, util)) / sum(durations),
                  cluster['util_time_weighted_mean_percent'], label + ': cluster weighted mean differs')
            for suffix, expected in [('p50', quantile(util, .5)), ('p95', quantile(util, .95)),
                                     ('p99', quantile(util, .99)), ('max', max(util))]:
                close(expected, cluster['window_util_' + suffix + '_percent'],
                      label + ': cluster window percentile differs: ' + suffix)
        windows = {}
        for row in rows:
            audit(row['policy'] == run['policy'] and row['valid'] in ('1', 1) and row['invalid_reason'] == '',
                  label + ': invalid CSV row state')
            source_line = int(row['source_line'])
            audit(run['source_lines']['cpu_series'][0] <= source_line <= run['source_lines']['cpu_series'][1],
                  label + ': CPU row source line outside retained block')
            close(float(row['util_percent']), float(row['busy_cpu_equivalent']) * 100 / 16,
                  label + ': CPU row units disagree', 4e-6)
            windows.setdefault((row['relative_start_ms'], row['relative_end_ms']), []).append(row)
        for members in windows.values():
            audit(len({r['process_cpu_equivalent'] for r in members}) == 1,
                  label + ': process value differs between simultaneous cluster rows')
        process = scope['process']
        audit(process['invalid_reasons'] == {} and process['valid_windows'] == 159,
              label + ': process scope is invalid')
        close(process['cpu_seconds'] / process['valid_seconds'], process['cpu_equivalent'],
              label + ': process own-window CPU arithmetic differs')
        close(100 * process['valid_seconds'] / 16, process['valid_coverage_percent'],
              label + ': process own-window coverage differs')
        audit(run['resources'] == dict(seed=421, pool_count=6, affinity='cluster', threads_per_pool=16),
              label + ': report resources differ')
        audit(run['work'] == first_work and bool(run['same_work_audit']), label + ': work identity differs')
        audit(report['model'] == 'branch-rw' and report['continuation'] == 'parent' and
              report['completed'] == report['offered'] == 192 and report['tasks'] == 32832 and
              report['cross_numa'] == 0, label + ': request cohort/graph contract differs')
        audit(whole['completed'] == 240 and whole['rejected_or_failed'] == 0,
              label + ': whole completion/failed counts differ')
        close(whole['process_cpu_seconds'] * 1000 / whole['completed'], whole['process_cpu_per_request_ms'],
              label + ': whole process CPU/request denominator differs', .00051)
        arrival = run['arrival']
        audit(arrival['kind'] == 'regular' and arrival['burst_size_spacing_us'] == 'not applicable',
              label + ': arrival kind differs')
        audit(arrival['whole_iterations'] == 168026400000 and arrival['whole_accesses'] == 5600880000,
              label + ': fixed work counts differ')
        for field in ['iterations', 'accesses']:
            close(arrival['whole_' + field] / 240, arrival['per_request_mean_' + field],
                  label + ': work/request arithmetic differs')
        close(arrival['mean_attempt_lag_ms'] + arrival['mean_attempt_to_finish_ms'],
              arrival['mean_planned_latency_ms'], label + ': arrival mean decomposition differs', .0011)
        close(arrival['mean_planned_latency_ms'], report['mean_ms'], label + ': arrival/report means differ')
        roles = {r['role']: r for r in run['roles']}
        exact = {r['role']: r for r in run['role_csv']}
        audit(set(roles) == set(exact) == set(role_counts), label + ': role coverage differs')
        audit(sum(role['n'] for role in roles.values()) == report['tasks'] == run['audit']['role_callbacks'],
              label + ': callback role conservation failure')
        audit(sum(role['changed_pool'] for role in roles.values()) == report['migrations'],
              label + ': migration role conservation failure')
        close(100 * report['migrations'] / report['tasks'], run['derived']['routing_changed_pool_percent'],
              label + ': routing percentage differs')
        for name, count in role_counts.items():
            role, precise = roles[name], exact[name]
            audit(role['n'] == precise['n'] == count and role['changed_pool'] == precise['changed_pool'],
                  label + ': printed/exact role counts differ: ' + name)
            audit(0 <= role['back_to_home'] <= role['changed_pool'] <= count,
                  label + ': invalid migration bounds: ' + name)
            audit(sum(role['reasons'].values()) == count, label + ': routing reason conservation failure: ' + name)
            close(role['wait_mean_ms'], precise['wait_mean_ms'], label + ': role wait rounding differs: ' + name, .00051)
            close(100 * precise['changed_pool'] / count, precise['changed_pool_percent'],
                  label + ': exact role migration percentage differs: ' + name)
        audit(roles['init']['changed_pool'] == 0, label + ': init was not pinned home')
        if run['policy'] == 'home':
            audit(report['migrations'] == 0, label + ': home changed a task pool')
        audit(run['audit']['status'] == 'passed', label + ': retained upstream audit failed')
    historical = read_json(HERE / 'data/comparisons.json')['runs']
    audit(len(historical) == 97 and all(row['batch'] != '4021' for row in historical),
          'CPU supplement was mixed into the old historical population')
    print(f'CPU supplement: {len(rounds)} rounds, {row_count} CPU rows, full-window metrics and role conservation verified')


def validate_independent():
    d = read_json(HERE / "data/independent_evidence.json")
    path = ROOT / d["source_file"]
    check(digest(path) == d["source_sha256"], "Stale independent source hash", "independent")
    lines = path.read_text().splitlines()
    for row in d["rows"]:
        line = lines[row["source_line"]-1]
        check(line == row["source_table_row"], f"Changed independent source row {row['queue_bin']}", "independent")
        cells = line.strip("|").split("|")
        check(cells[0] == row["queue_bin"], "Wrong independent Q bucket", "independent")
        for i, field in enumerate(["wait_p50_ms_range", "wait_p95_ms_range", "wait_p99_ms_range", "submit_to_finish_p50_ms_range", "submit_to_finish_p99_ms_range"], 1):
            check(row[field] == [float(x) for x in cells[i].split("～")], f"Wrong independent values: {row['queue_bin']}.{field}", "independent")
        counts = [int(v) for v in cells[6].split("～")]
        if len(counts) == 1:
            counts *= 2
        check(row["tasks_per_run_range"] == counts, f"Wrong independent counts {row['queue_bin']}", "independent")
    check(len(d["rows"]) == len({r["queue_bin"] for r in d["rows"]}), "Duplicate independent bucket", "independent")
    check(d["scope"]["tasks_per_run"] == 12000 and d["scope"]["threads_per_pool"] == 256, "Wrong independent population/config", "independent")
    check("[10,60)" in d["scope"]["cohort"], "Wrong independent window", "independent")


class Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            self.sources.append(dict(attrs).get("src", ""))


def link_target(document, url):
    if url.startswith("#") or urlsplit(url).scheme:
        return None
    target = unquote(url.split("#", 1)[0].split("?", 1)[0])
    return (document.parent / target).resolve()


def validate_artifacts(reports, require):
    manifest_path = HERE / "figures/manifest.json"
    if not manifest_path.exists():
        check(not require, "Figure manifest not generated yet", "artifacts")
        manifest = []
    else:
        manifest = read_json(manifest_path)
        names = [f["name"] for f in manifest]
        check(len(names) == len(set(names)) and bool(names), "Empty/duplicate figure manifest", "artifacts")
        for item in manifest:
            for ext in ["png", "svg"]:
                path = HERE / "figures" / f"{item['name']}.{ext}"
                check(path.is_file() and path.stat().st_size > 100, f"Missing/empty figure: {path}", "artifacts")
                if path.is_file() and ext == "png":
                    check(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), f"Invalid PNG: {path}", "artifacts")
                if path.is_file() and ext == "svg":
                    check("<svg" in path.read_text(), f"Invalid SVG: {path}", "artifacts")
    found = set(reports) | set(HERE.glob("*.md")) | set(HERE.glob("*.html")) | set(HERE.glob("*.pdf"))
    markdown = [p for p in found if p.suffix.lower() == ".md"]
    html = [p for p in found if p.suffix.lower() == ".html"]
    pdf = [p for p in found if p.suffix.lower() == ".pdf"]
    if require:
        check(bool(markdown), "Final Markdown missing", "artifacts")
        check(bool(html), "Final HTML missing", "artifacts")
        check(bool(pdf), "Final PDF missing", "artifacts")
    for path in markdown:
        check(path.is_file(), f"Missing report {path}", "artifacts")
        if not path.is_file():
            continue
        content = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
        for url in re.findall(r"!?\[[^\]]*\]\(([^\n)]+)\)", content):
            url = url.strip()
            if url.startswith("<"):
                url = url[1:url.index(">")]
            else:
                url = url.split(' "', 1)[0]
            target = link_target(path, url)
            if target is not None:
                check(target.exists(), f"Broken local Markdown link: {path}: {url}", "links")
    for path in html:
        parser = Images()
        parser.feed(path.read_text())
        check(bool(parser.sources), f"HTML contains no images: {path}", "artifacts")
        for src in parser.sources:
            check(src.startswith("data:image/"), f"HTML image not embedded: {path}: {src[:100]}", "artifacts")
            if src.startswith("data:image/") and ";base64," in src:
                try:
                    payload = base64.b64decode(src.split(",", 1)[1], validate=True)
                    check(len(payload) > 100, f"Empty embedded image: {path}", "artifacts")
                except ValueError:
                    check(False, f"Invalid embedded base64: {path}", "artifacts")
    for path in pdf:
        check(path.is_file() and path.stat().st_size > 1000, f"Missing/empty PDF: {path}", "artifacts")
        if path.is_file():
            check(path.read_bytes().startswith(b"%PDF-"), f"Invalid PDF header: {path}", "artifacts")
    print(f"Publication artifacts: {len(manifest)} figures, {len(markdown)} Markdown, {len(html)} HTML, {len(pdf)} PDF")


def validate_baseline_and_trace():
    baseline = read_json(HERE / "data/affinity_baseline_evidence.json")
    source = ROOT / baseline["source_file"]
    check(digest(source) == baseline["source_sha256"], "Stale affinity baseline source", "baseline")
    for line in baseline["source_table_rows"]:
        check(line in source.read_text().splitlines(), "Affinity baseline table row differs", "baseline")
    mean_row, tail_row = baseline["source_table_rows"]
    source_means = [float(x) for x in mean_row.strip("|").split("|")[1:]]
    source_tails = [list(map(float, x.split("～"))) for x in tail_row.strip("|").split("|")[1:]]
    for index, row in enumerate(baseline["rows"]):
        check(row["affinity"] == ["none", "numa", "cluster"][index], "Affinity baseline order", "baseline")
        check(row["request_mean_ms"] == source_means[index], "Affinity mean differs from source", "baseline")
        check(row["request_p99_ms_range"] == source_tails[index], "Affinity p99 differs from source", "baseline")
    trace = read_json(HERE / "data/trace_evidence.json")
    for source in trace["sources"]:
        check((ROOT / source["file"]).is_file(), "Missing trace review source", "trace")
    pools = trace["per_pool"]
    check(sum(p["mapped_tids"] for p in pools) == trace["historical_capture"]["mapped_tids"], "Trace TID sum", "trace")
    for field in ["mean_oncpu_equivalents", "mean_observed_demand_equivalents"]:
        check(abs(sum(p[field] for p in pools) - trace["same_window_pool_sum"][field]) < .0011, "Trace same-window mean sum", "trace")
    for pool in pools:
        check(pool["mean_oncpu_equivalents"] <= pool["peak_1ms_oncpu_equivalents"] <= pool["peak_1ms_unique_oncpu_tids"] <= pool["mapped_tids"], "Trace CPU/unique/TID bounds", "trace")
    for row in trace["full_window_pearson_lag_zero_ranges_across_pools"]:
        check(-1 <= row["min"] <= row["max"] <= 1, "Trace correlation bounds", "trace")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-artifacts", action="store_true", help="Fail unless final Markdown, embedded HTML and PDF exist")
    parser.add_argument("--report", type=Path, action="append", default=[], help="Also validate this report outside the default folder; repeatable")
    parser.add_argument("--cpu-source-log", type=Path, help="Optionally verify the original 4021.log attachment hash and extent; not required for portable validation")
    args = parser.parse_args()
    validate_comparisons()
    validate_scenarios()
    validate_cpu_supplement(args.cpu_source_log)
    validate_independent()
    validate_baseline_and_trace()
    validate_artifacts([p.resolve() for p in args.report], args.require_artifacts)
    print("Checks: " + ", ".join(f"{k}={v}" for k, v in sorted(CHECKS.items())))
    for message in ERRORS:
        print("ERROR: " + message, file=sys.stderr)
    print(f"{'PASS' if not ERRORS else 'FAIL'}: {sum(CHECKS.values())} checks, {len(ERRORS)} errors")
    files = [p for p in HERE.rglob("*") if p.is_file() and p.name != "validation.json" and "__pycache__" not in p.parts]
    output = {"schema": "final-review-validation-v1", "passed": not ERRORS,
              "checks": dict(sorted(CHECKS.items())), "errors": ERRORS,
              "scope": "reviewed source/dataset consistency and offline artifact integrity; no new performance measurements",
              "artifact_sha256": {str(p.relative_to(HERE)): digest(p) for p in sorted(files)},
              "additional_evidence_sha256": {str(p.relative_to(ROOT)): digest(p) for p in
                  [ROOT / "results/review/4021_summary.json", ROOT / "results/review/4021_cpu_windows.csv"] if p.is_file()}}
    (HERE / "validation.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    return bool(ERRORS)


if __name__ == "__main__":
    raise SystemExit(main())
