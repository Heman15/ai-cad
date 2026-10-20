# Contributing

Thanks for helping improve AI CAD!

## Before you start
- Open an issue to discuss larger changes first.
- Run the checks: `.venv/bin/python -m pytest -q tests` (fast) and `tests/run_all.sh` (adds the Chrome UI suites and the desktop self-test). Please run heavy jobs one at a time on machines with little RAM.
- Keep the principle of the project: **the AI only issues validated commands; the CAD core builds, checks and measures.** No unvalidated code execution, no silent guessing of dimensions or references.

## Contributor Licence Agreement
AI CAD is offered under the AGPL v3 **and** under a commercial licence (see `COMMERCIAL.md`). To make that possible, contributors sign the **Contributor Licence Agreement** in `CLA.md` once, before their first pull request is merged. You keep the copyright to your contribution; you grant the project the right to distribute it under both licences.

## Code style
- Python: type hints, short docstrings that say *why*, user-facing error messages that say what to do.
- TypeScript/React: functional components, no new UI library without discussion.
- Every bug fix gets a regression test.
