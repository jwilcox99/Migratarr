"""Read-only comparison of the pinned pre-target-id manifest builder and candidate.

Runs the baseline (pre-target-id) and candidate build_execution_manifest.py
source against copies of the same frozen run snapshot, each under its own
throwaway scratch base path, and confirms the emitted execution_manifest.csv and manifest_metadata.json are
byte-identical, aside from the manifest's own wall-clock created_utc stamp and
its scratch-path-derived source_snapshot field, neither of which either
version can hold fixed across two isolated runs. Neither run touches the real
deployment's runs/ or manifests/ directories, and no Arr calls are made; the
manifest builder never contacts live services.
"""
import argparse
import ast
import hashlib
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

MANIFEST_BUILDER = Path(__file__).resolve().parents[1] / "build_execution_manifest.py"
PREAMBLE_NAMES = {"RUNTIME", "BASE", "RUNS", "MANIFESTS", "TARGETS"}


def _assigned_name(node):
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    return target.id if isinstance(target, ast.Name) else None


def _run_source(source, run_id, runtime, targets, base):
    """Execute the builder's script body, substituting its config preamble."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    body = [node for node in tree.body if _assigned_name(node) not in PREAMBLE_NAMES]
    code = compile(ast.Module(body=body, type_ignores=[]), str(source), "exec")
    namespace = {
        "__name__": "__main__",
        "RUNTIME": runtime,
        "BASE": base,
        "RUNS": base / "runs",
        "MANIFESTS": base / "manifests",
        "TARGETS": targets,
    }
    argv = sys.argv
    sys.argv = [str(source), run_id]
    output = StringIO()
    try:
        with redirect_stdout(output):
            exec(code, namespace)
    finally:
        sys.argv = argv
    return output.getvalue()


def _build(source, run_dir, runtime, targets):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dest_run = base / "runs" / run_dir.name
        shutil.copytree(run_dir, dest_run)
        report = _run_source(source, run_dir.name, runtime, targets, base)
        manifest_dir = base / "manifests" / run_dir.name
        csv_bytes = (manifest_dir / "execution_manifest.csv").read_bytes()
        metadata = json.loads((manifest_dir / "manifest_metadata.json").read_text())
        # Both fields are artifacts of this harness's isolated scratch base path
        # and wall clock, not of the manifest builder's own decision logic.
        metadata.pop("created_utc", None)
        metadata.pop("source_snapshot", None)
        return csv_bytes, metadata, report


def compare_manifests(baseline, run_dir, runtime, targets):
    baseline_hash = hashlib.sha256(baseline.read_text(encoding="utf-8").encode()).hexdigest()
    if baseline_hash != "a1e3dcb8c2e716916b9948a66c1dd525bcf806513ec32d1beaad99d86bb8b872":
        raise ValueError("Baseline must be the manifest builder before target IDs")
    baseline_csv, baseline_meta, baseline_report = _build(baseline, run_dir, runtime, targets)
    candidate_csv, candidate_meta, candidate_report = _build(
        MANIFEST_BUILDER, run_dir, runtime, targets
    )
    digest = lambda value: hashlib.sha256(value).hexdigest()
    meta_digest = lambda value: digest(json.dumps(value, sort_keys=True).encode())
    return {
        "byte_identical": baseline_csv == candidate_csv and baseline_meta == candidate_meta,
        "baseline_csv_sha256": digest(baseline_csv),
        "candidate_csv_sha256": digest(candidate_csv),
        "baseline_metadata_sha256": meta_digest(baseline_meta),
        "candidate_metadata_sha256": meta_digest(candidate_meta),
        "baseline_code_sha256": baseline_hash,
        "candidate_code_sha256": digest(MANIFEST_BUILDER.read_bytes()),
        "baseline_report": baseline_report,
        "candidate_report": candidate_report,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "run-dir", "runtime-config", "targets"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    from runtime_config import load_config
    from storage_targets import load_targets

    try:
        runtime = load_config(args.runtime_config, environ={})
        targets = load_targets(args.targets)
        result = compare_manifests(args.baseline, args.run_dir, runtime, targets)
        for name in ("runtime_config", "targets"):
            path = getattr(args, name)
            result[name + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Manifest parity could not complete: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0 if result["byte_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
