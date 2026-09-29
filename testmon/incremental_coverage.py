"""Experimental, fail-closed branch coverage cache for testmon runs."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import coverage
from coverage import Coverage, CoverageData


class IncompleteCoverageCache(Exception):
    pass


def file_hash(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def repository_files(cache_path):
    try:
        paths = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    excluded = {
        cache_path,
        cache_path.with_name(cache_path.name + ".json"),
        cache_path.with_name(cache_path.name + ".testmondata"),
        cache_path.with_name(cache_path.name + ".testmondata-shm"),
        cache_path.with_name(cache_path.name + ".testmondata-wal"),
    }
    files = {}
    for path in os.fsdecode(paths).split("\0"):
        if path:
            filename = Path(path).resolve()
            if filename not in excluded:
                files[str(filename)] = file_hash(filename)
    return files


def environment_hash():
    packages = sorted(
        (distribution.metadata["Name"], distribution.version)
        for distribution in importlib.metadata.distributions()
    )
    config = [
        file_hash(path) for path in ("pyproject.toml", ".coveragerc", "setup.cfg")
    ]
    implementation = [
        file_hash(Path(__file__).with_name(filename))
        for filename in (
            "incremental_coverage.py",
            "pytest_testmon.py",
            "testmon_core.py",
        )
    ]
    value = [sys.version, coverage.__version__, packages, config, implementation]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def read_cache(path):
    path = Path(path)
    manifest_path = path.with_name(path.name + ".json")
    if not path.is_file() or not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest["data_hash"] != file_hash(path):
            return None
        if manifest["environment_hash"] != environment_hash():
            return None
        if manifest["run_key"] != os.environ.get("TESTMON_COVERAGE_RUN_KEY"):
            return None
        if "repository_files" not in manifest:
            return None
        data = CoverageData(basename=str(path))
        data.read()
        if not data.has_arcs():
            return None
        return data, manifest
    except (KeyError, OSError, ValueError):
        return None


def changed_files(manifest):
    return {
        filename
        for filename, old_hash in manifest["files"].items()
        if file_hash(filename) != old_hash
    }


def tests_to_force(path):
    cached = read_cache(path)
    if cached is None:
        return set(), True

    data, manifest = cached
    current_repository_files = repository_files(Path(path).resolve())
    if current_repository_files is None:
        return set(), True
    if any(
        manifest["repository_files"].get(filename)
        != current_repository_files.get(filename)
        and filename not in manifest["files"]
        for filename in manifest["repository_files"].keys()
        | current_repository_files.keys()
    ):
        return set(), True
    changed = changed_files(manifest)
    forced = set()
    for filename in changed:
        if not Path(filename).exists() or Path(filename).name == "conftest.py":
            return set(), True
        contexts = {
            context
            for line_contexts in data.contexts_by_lineno(filename).values()
            for context in line_contexts
            if context
        }
        if not contexts:
            return set(), True
        forced.update(contexts)
        forced.update(
            context
            for context in data.measured_contexts()
            if context and Path(context.split("::", 1)[0]).resolve() == Path(filename)
        )
    return forced, False


def merge_coverage(previous, fresh, manifest, output_path):
    changed = changed_files(manifest) if manifest else set()
    fresh_contexts = fresh.measured_contexts()
    stale_tests = set()
    if previous:
        stale_tests = {
            context
            for context in previous.measured_contexts()
            if context
            and any(
                Path(context.split("::", 1)[0]).resolve() == Path(filename)
                for filename in changed
            )
        }
        for filename in changed:
            contexts = {
                context
                for line_contexts in previous.contexts_by_lineno(filename).values()
                for context in line_contexts
                if context
            }
            if contexts - fresh_contexts - stale_tests:
                raise IncompleteCoverageCache(f"Tests covering {filename} were skipped")

    output = CoverageData(basename=str(output_path))
    for source in ([previous] if previous else []) + [fresh]:
        source.set_query_contexts(None)
        files_by_context = defaultdict(set)
        for filename in source.measured_files():
            for contexts in source.contexts_by_lineno(filename).values():
                for context in contexts:
                    files_by_context[context].add(filename)
        for context in source.measured_contexts():
            if source is previous and (
                context in stale_tests or (context and context in fresh_contexts)
            ):
                continue
            source.set_query_context(context)
            arcs = {
                filename: source.arcs(filename)
                for filename in files_by_context[context]
                if source.arcs(filename)
                and (source is fresh or filename not in changed)
            }
            if arcs:
                output.set_context(context)
                output.add_arcs(arcs)
    output.write()
    return output


def run_tests(arguments, cache_path, select, forced_tests=None):
    with tempfile.TemporaryDirectory(prefix="testmon-coverage-") as tempdir:
        coverage_file = str(Path(tempdir) / ".coverage")
        env = os.environ.copy()
        env["COVERAGE_FILE"] = coverage_file
        env.setdefault(
            "TESTMON_DATAFILE",
            str(cache_path.with_name(cache_path.name + ".testmondata")),
        )
        if select:
            env["TESTMON_COVERAGE_CACHE"] = str(cache_path)
            forced_file = Path(tempdir) / "forced-tests.json"
            forced_file.write_text(json.dumps(sorted(forced_tests or [])))
            env["TESTMON_COVERAGE_FORCED_TESTS_FILE"] = str(forced_file)
        else:
            env.pop("TESTMON_COVERAGE_CACHE", None)
            env.pop("TESTMON_COVERAGE_FORCED_TESTS_FILE", None)
        option = "--testmon" if select else "--testmon-noselect"
        command = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "-p",
            "-m",
            "pytest",
            option,
            *arguments,
        ]
        status = subprocess.call(command, env=env)
        if status:
            return status, None
        result = Coverage(data_file=coverage_file)
        result.combine()
        result.load()
        return 0, result.get_data().dumps()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--include", action="append")
    parser.add_argument("--min-coverage", type=float, default=100)
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    pytest_args = (
        args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    )
    run_settings = {"pytest_args": pytest_args, "include": args.include}
    os.environ["TESTMON_COVERAGE_RUN_KEY"] = hashlib.sha256(
        json.dumps(run_settings, sort_keys=True).encode()
    ).hexdigest()
    args.cache = args.cache.resolve()
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    repository_before = repository_files(args.cache)
    if repository_before is None:
        parser.error("incremental coverage requires a Git worktree")
    cached = read_cache(args.cache)
    hashes_before = (
        {filename: file_hash(filename) for filename in cached[1]["files"]}
        if cached
        else {}
    )
    forced_tests = set()
    if cached:
        forced_tests, force_all = tests_to_force(args.cache)
        if force_all:
            cached = None
    status, fresh_bytes = run_tests(
        pytest_args, args.cache, select=cached is not None, forced_tests=forced_tests
    )
    if status:
        return status
    if repository_files(args.cache) != repository_before:
        print("Repository files changed while tests were running", file=sys.stderr)
        return 1
    if any(file_hash(filename) != value for filename, value in hashes_before.items()):
        print("Source files changed while tests were running", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="testmon-coverage-merged-") as tempdir:
        output_path = Path(tempdir) / ".coverage"
        fresh_path = Path(tempdir) / "fresh.coverage"
        fresh = CoverageData(basename=str(fresh_path))
        fresh.loads(fresh_bytes)
        try:
            if (
                cached
                and changed_files(cached[1]) == set()
                and fresh.measured_contexts() <= {""}
            ):
                shutil.copyfile(args.cache, output_path)
            elif cached:
                merge_coverage(cached[0], fresh, cached[1], output_path)
            else:
                shutil.copyfile(fresh_path, output_path)
        except IncompleteCoverageCache:
            status, fresh_bytes = run_tests(pytest_args, args.cache, select=False)
            if status:
                return status
            fresh.loads(fresh_bytes)
            shutil.copyfile(fresh_path, output_path)

        result = Coverage(data_file=str(output_path))
        result.load()
        if not args.no_report:
            percent = result.report(include=args.include, skip_covered=False)
            if percent < args.min_coverage:
                return 1
        files = {
            filename: file_hash(filename)
            for filename in result.get_data().measured_files()
        }
        if any(value is None for value in files.values()):
            return 1
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        os.replace(output_path, args.cache)
        manifest = {
            "data_hash": file_hash(args.cache),
            "environment_hash": environment_hash(),
            "run_key": os.environ["TESTMON_COVERAGE_RUN_KEY"],
            "files": files,
            "repository_files": repository_before,
        }
        manifest_path = args.cache.with_name(args.cache.name + ".json")
        temporary_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
        temporary_manifest.write_text(json.dumps(manifest, sort_keys=True))
        os.replace(temporary_manifest, manifest_path)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args.cache, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
