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
