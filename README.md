<img src=https://user-images.githubusercontent.com/135344/219700265-0a9b152f-7285-4607-bbce-0c9aeddd520b.svg width=300>

This is a pytest plug-in which automatically selects and re-executes
only tests affected by recent changes. How is this possible in dynamic
language like Python and how reliable is it? Read here: [Determining
affected tests](https://testmon.org/blog/determining-affected-tests/)

## Quickstart

    pip install pytest-testmon

    # build the dependency database and save it to .testmondata
    pytest --testmon

    # change some of your code (with test coverage)

    # only run tests affected by recent changes
    pytest --testmon

To learn more about different options you can use with testmon, please
head to [testmon.org](https://testmon.org)

## Experimental incremental branch coverage

Run a full suite once to seed both testmon's dependency database and the coverage cache:

```bash
python -m testmon.incremental_coverage --cache .testmon-coverage -- tests -n 4
```

Run the same command after edits. The runner executes tests selected by testmon and adds tests that previously covered
changed files. It reuses branch coverage from skipped tests only for unchanged files. It reports coverage with the
project's coverage.py configuration and exits nonzero below 100%.

Use `--no-report --output path/to/coverage-file` for a CI matrix leg that contributes to a separate combined 100%
report. Keep a separate cache for each Python version, dependency set, and test selection. The cache includes per-test
branch arcs, so it is larger than testmon's dependency database.

The runner needs a Git worktree. A changed repository file without cached coverage forces a full test run. Changes to
external resources must use a separate cache or a full run.

## Call for opensource projects: try testmon in CI with no effort or risk.

We would like to run testmon within your project, collect data and improve!
We'll prepare the PR for you and set everything up so that no tests are deselected initially.
You can start using the full functionality whenever the reliability and time savings seem right!
Please <a href="https://www.testmon.net/">SIGN UP</a> and we'll contact you shortly.
