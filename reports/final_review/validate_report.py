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
    print(f'Scenario evidence: {len(rows)} runs, {len(extra_parsed)} retained routing excerpts, CPU series explicitly unavailable')


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
    args = parser.parse_args()
    validate_comparisons()
    validate_scenarios()
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
              "artifact_sha256": {str(p.relative_to(HERE)): digest(p) for p in sorted(files)}}
    (HERE / "validation.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    return bool(ERRORS)


if __name__ == "__main__":
    raise SystemExit(main())
