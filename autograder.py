"""
Autograder for WorkFlow Assistant.

Runs the test suite, groups the results by the REQUIREMENTS.md section each
test file checks, and prints a score per section and overall, with the reason
for every failed test.

    python autograder.py              # grade everything
    python autograder.py -s 8.11      # grade one section (matches id or name)
    python autograder.py -v           # also list passing tests
    python autograder.py --min 90     # exit 1 if the total is under 90%
    python autograder.py --timeout 60 # seconds allowed per test file (default 300)

Exits 1 if any test fails (or the total is under --min), so CI can use it.
"""
import argparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# (section id, name, test file, points). Points are split evenly between the
# tests in that file, so adding a test never changes a section's weight.
RUBRIC = [
    ("2", "OAuth credential system (paths, Firestore, encryption)", "tests/test_credentials_path_reference.py", 10),
    ("2.1", "oauth_login.py with bad input", "tests/test_oauth_login_bad_input.py", 30),
    ("1", "Login page and navigation", "tests/test_login_navigation.py", 15),
    ("7.2", "Natural-language dates and times", "tests/test_parse_natural_language_datetime.py", 10),
    ("8", "chat_interface: calendar and agent after a refresh", "tests/test_chat_interface.py", 25),
    ("8.11", "fetch_creds requirements", "tests/test_fetch_creds_requirements.py", 10),
]


def _run_pytest(file: str, timeout: float) -> ET.Element | str:
    """The junit report for one test file, or why there isn't one. Each file runs
    in its own process, so one that hangs or crashes can't take the rest down."""
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        try:
            subprocess.run(
                [python, "-m", "pytest", file, "-q", "-p", "no:cacheprovider", f"--junitxml={report}"],
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"timed out after {timeout:.0f}s (a test is hanging)"
        if not report.exists():
            return "pytest did not produce a report (is pytest installed in .venv?)"
        return ET.parse(report).getroot()


def _reason(case: ET.Element) -> str | None:
    """None if the test passed, else a one-line reason."""
    for tag in ("failure", "error"):
        problem = case.find(tag)
        if problem is not None:
            message = (problem.get("message") or problem.text or tag).strip()
            first_line = message.splitlines()[0] if message else tag
            return first_line[:160]
    if case.find("skipped") is not None:
        return "skipped"
    return None


def _cases_by_file(root: ET.Element) -> dict[str, list[tuple[str, str | None]]]:
    results: dict[str, list[tuple[str, str | None]]] = {}
    for case in root.iter("testcase"):
        # classname is "tests.test_x" (or "tests.test_x.Class"); file is tests/test_x.py.
        parts = case.get("classname", "").split(".")
        file = "/".join(parts[:2]) + ".py" if len(parts) >= 2 else case.get("file", "")
        results.setdefault(file, []).append((case.get("name", "?"), _reason(case)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", "--section", help="only grade sections whose id or name contains this")
    parser.add_argument("-v", "--verbose", action="store_true", help="list passing tests too")
    parser.add_argument("--min", type=float, default=100.0, help="minimum passing percentage (default 100)")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds allowed per test file (default 300)")
    args = parser.parse_args()

    rubric = [
        row for row in RUBRIC
        if not args.section or args.section == row[0] or args.section.lower() in row[1].lower()
    ]
    if not rubric:
        sys.exit(f"No section matches {args.section!r}.")

    earned_total = possible_total = 0.0
    failed_any = False
    for section, name, file, points in rubric:
        possible_total += points
        if not (REPO_ROOT / file).exists():
            report = "test file missing"
        else:
            print(f"Running {file} ...", end="\r", flush=True)
            report = _run_pytest(file, args.timeout)
        cases = [] if isinstance(report, str) else _cases_by_file(report).get(file, [])
        if not cases:
            failed_any = True
            why = report if isinstance(report, str) else "no tests ran (import error?)"
            print(f"\n[{section}] {name}: 0/{points}  ({why})")
            continue
        passed = sum(1 for _, reason in cases if reason is None)
        earned = points * passed / len(cases)
        earned_total += earned
        print(f"\n[{section}] {name}: {earned:.1f}/{points}  ({passed}/{len(cases)} tests)")
        for test, reason in cases:
            if reason is not None:
                failed_any = True
                print(f"   FAIL  {test}\n         {reason}")
            elif args.verbose:
                print(f"   pass  {test}")

    percent = 100 * earned_total / possible_total if possible_total else 0
    print(f"\nTOTAL: {earned_total:.1f}/{possible_total:.0f}  ({percent:.1f}%)")
    return 1 if failed_any or percent < args.min else 0


if __name__ == "__main__":
    sys.exit(main())
