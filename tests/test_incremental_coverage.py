import os
import subprocess
import sys
from pathlib import Path

import pytest
from coverage import CoverageData

from testmon.incremental_coverage import (
    IncompleteCoverageCache,
    file_hash,
    merge_coverage,
)


def coverage_data(
    path: Path, contexts: dict[str, dict[str, list[tuple[int, int]]]]
) -> CoverageData:
    data = CoverageData(basename=str(path))
    for context, files in contexts.items():
        data.set_context(context)
        data.add_arcs(files)
    data.write()
    return data


def test_merge_replaces_rerun_test_and_keeps_skipped_test(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("a = 1\nb = 2\n")
    filename = str(source)
    previous = coverage_data(
        tmp_path / "previous",
        {
            "tests/test_a.py::test_a": {filename: [(1, 2)]},
            "tests/test_b.py::test_b": {filename: [(2, -1)]},
        },
    )
    fresh = coverage_data(
        tmp_path / "fresh", {"tests/test_a.py::test_a": {filename: [(1, -1)]}}
    )

    merged = merge_coverage(
        previous, fresh, {"files": {filename: file_hash(source)}}, tmp_path / "merged"
    )

    assert set(merged.arcs(filename)) == {(1, -1), (2, -1)}
    merged.set_query_context("tests/test_a.py::test_a")
    assert merged.arcs(filename) == [(1, -1)]


def test_merge_preserves_default_context_between_runs(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("a = 1\nb = 2\n")
    filename = str(source)
    previous = coverage_data(
        tmp_path / "previous", {"": {filename: [(1, 2)]}}
    )
    fresh = coverage_data(
        tmp_path / "fresh", {"": {filename: [(2, -1)]}}
    )

    merged = merge_coverage(
        previous, fresh, {"files": {filename: file_hash(source)}}, tmp_path / "merged"
    )

    assert set(merged.arcs(filename)) == {(1, 2), (2, -1)}


def test_merge_rejects_skipped_test_for_changed_file(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("a = 1\nb = 2\n")
    filename = str(source)
    previous = coverage_data(
        tmp_path / "previous",
        {
            "tests/test_a.py::test_a": {filename: [(1, 2)]},
            "tests/test_b.py::test_b": {filename: [(2, -1)]},
        },
    )
    old_hash = file_hash(source)
    source.write_text("a = 1\nb = 3\n")
    fresh = coverage_data(
        tmp_path / "fresh", {"tests/test_a.py::test_a": {filename: [(1, -1)]}}
    )

    with pytest.raises(IncompleteCoverageCache, match="Tests covering"):
        merge_coverage(
            previous, fresh, {"files": {filename: old_hash}}, tmp_path / "merged"
        )


def test_merge_discards_removed_test_context(tmp_path: Path) -> None:
    test_file = tmp_path / "test_example.py"
    test_file.write_text("def test_removed():\n    pass\n")
    source = tmp_path / "source.py"
    source.write_text("a = 1\n")
    old_hash = file_hash(test_file)
    test_file.write_text("def test_added():\n    pass\n")
    previous = coverage_data(
        tmp_path / "previous",
        {
            f"{test_file}::test_removed": {
                str(test_file): [(1, 2)],
                str(source): [(1, -1)],
            }
        },
    )
    fresh = coverage_data(
        tmp_path / "fresh", {f"{test_file}::test_added": {str(test_file): [(1, -1)]}}
    )

    merged = merge_coverage(
        previous, fresh, {"files": {str(test_file): old_hash}}, tmp_path / "merged"
    )

    assert merged.arcs(str(test_file)) == [(1, -1)]
    assert merged.arcs(str(source)) is None


def test_session_fixture_teardown_remains_covered_across_batches(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n.coverage*\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nbranch = true\ninclude = ["conftest.py"]\n'
        "[tool.coverage.report]\nfail_under = 100\n"
    )
    (tmp_path / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def session_fixture():\n"
        "    yield\n"
        "    return\n"
    )
    (tmp_path / "test_many.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('index', range(251))\n"
        "def test_many(index):\n"
        "    assert index >= 0\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", ""))
    )
    command = [
        sys.executable,
        "-m",
        "testmon.incremental_coverage",
        "--cache",
        str(tmp_path / ".coverage-cache"),
        "--include",
        "conftest.py",
        "--",
        "-p",
        "testmon.pytest_testmon",
        "test_many.py",
        "-m",
        "not slow",
        "-q",
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "251 passed" in result.stdout
    assert "100%" in result.stdout

    cached = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert cached.returncode == 0, cached.stdout + cached.stderr
    assert "251 deselected" in cached.stdout
    assert "100%" in cached.stdout


def test_incremental_coverage_preserves_pytest_order(tmp_path: Path) -> None:
    (tmp_path / "test_order.py").write_text(
        "import time\n"
        "def test_slow():\n"
        "    time.sleep(0.05)\n"
        "def test_fast():\n"
        "    pass\n"
    )
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", ""))
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "testmon.pytest_testmon",
        "--testmon-noselect",
        "-v",
        "test_order.py",
    ]
    first = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert first.returncode == 0, first.stdout + first.stderr

    reordered = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert reordered.returncode == 0, reordered.stdout + reordered.stderr
    assert reordered.stdout.index("test_order.py::test_fast") < reordered.stdout.index(
        "test_order.py::test_slow"
    )

    env["TESTMON_COVERAGE_RUN_KEY"] = "coverage-wrapper"
    second = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout.index("test_order.py::test_slow") < second.stdout.index(
        "test_order.py::test_fast"
    )


def test_changed_source_selects_affected_test_and_keeps_full_coverage(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n.coverage*\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nbranch = true\ninclude = ["app_module.py"]\n'
        "[tool.coverage.report]\nfail_under = 100\n"
    )
    source = tmp_path / "app_module.py"
    source.write_text("def answer():\n    return 1\n")
    (tmp_path / "test_source.py").write_text(
        "from app_module import answer\n"
        "def test_source():\n    assert answer() in (1, 2)\n"
        "def test_other():\n    assert True\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", ""))
    )
    command = [
        sys.executable,
        "-m",
        "testmon.incremental_coverage",
        "--cache",
        str(tmp_path / ".coverage-cache"),
        "--include",
        "app_module.py",
        "--",
        "-p",
        "testmon.pytest_testmon",
        "test_source.py",
        "-m",
        "not slow",
        "-q",
    ]
    first = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert "2 passed" in first.stdout

    source.write_text("def answer():\n    return 2\n")
    warm = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert warm.returncode == 0, warm.stdout + warm.stderr
    assert "1 passed, 1 deselected" in warm.stdout
    assert "100%" in warm.stdout
    assert "cache valid; forcing 1 test contexts" in warm.stderr

    (tmp_path / "README.md").write_text("New unmeasured file\n")
    fallback = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert fallback.returncode == 0, fallback.stdout + fallback.stderr
    assert "2 passed" in fallback.stdout
    assert "unmeasured repository files changed" in fallback.stderr


def test_incomplete_cached_coverage_retries_full_run(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n.coverage*\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nbranch = true\ninclude = ["app_module.py"]\n'
        "[tool.coverage.report]\nfail_under = 100\n"
    )
    source = tmp_path / "app_module.py"
    source.write_text("def answer():\n    return 1\n")
    (tmp_path / "test_source.py").write_text(
        "import os\nimport pytest\nfrom app_module import answer\n"
        "@pytest.mark.skipif(os.environ.get('SKIP_TEST_A') == '1', reason='conditional')\n"
        "def test_a():\n    assert answer() in (1, 2)\n"
        "def test_b():\n    assert answer() in (1, 2)\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", ""))
    )
    command = [
        sys.executable,
        "-m",
        "testmon.incremental_coverage",
        "--cache",
        str(tmp_path / ".coverage-cache"),
        "--include",
        "app_module.py",
        "--",
        "-p",
        "testmon.pytest_testmon",
        "test_source.py",
        "-q",
    ]
    first = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert "2 passed" in first.stdout

    source.write_text("def answer():\n    return 2\n")
    env["SKIP_TEST_A"] = "1"
    warm = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert warm.returncode == 0, warm.stdout + warm.stderr
    assert "retrying full run" in warm.stderr
    assert "1 passed, 1 skipped" in warm.stdout
    assert "100%" in warm.stdout
