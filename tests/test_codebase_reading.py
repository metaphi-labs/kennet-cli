import pytest

from metaphi import codebase_reading


@pytest.mark.parametrize(("printed", "counts"), [
    ("collected 9 items\n...\n========= 2 failed, 7 passed, 1 skipped in 1.24s =========\n", (9, 7)),
    (("....\n----------------------------------------------------------------------\nRan 4 tests in 0.003s\n\n"
      "FAILED (failures=1, errors=1)\n"), (4, 2)),
    ("Ran 12 tests in 0.1s\n\nOK\n", (12, 12)),
    ("    Start 1: a\n1/3 Test #1: a ....   Passed\n\n67% tests passed, 1 tests failed out of 3\n", (3, 2)),
    (("Ok:                 40\nExpected Fail:      0\nFail:               2\nUnexpected Pass:    0\n"
      "Skipped:            1\nTimeout:            0\n"), (42, 40)),
    ("# TOTAL: 10\n# PASS:  9\n# SKIP:  0\n# XFAIL: 0\n# FAIL:  1\n# TOTAL: 5\n# PASS:  5\n", (15, 14)),
    ("test result: ok. 3 passed; 0 failed; 0 ignored\ntest result: FAILED. 1 passed; 2 failed; 0 ignored\n", (6, 4)),
    ("=== RUN   TestA\n--- PASS: TestA (0.00s)\n=== RUN   TestB\n--- FAIL: TestB (0.00s)\nFAIL\n", (2, 1)),
    ("ok  \texample.com/a\t0.012s\nFAIL\texample.com/b\t0.020s\n", (2, 1)),
    ("[INFO] Results:\n[ERROR] Tests run: 20, Failures: 1, Errors: 1, Skipped: 2\n", (18, 16)),
    (("[INFO] Tests run: 200, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1 s - in a.T\n[INFO] Results:\n"
      "[INFO] \n[INFO] Tests run: 200, Failures: 0, Errors: 0, Skipped: 0\n[INFO] Tests run: 3, Failures: 1, "
      "Errors: 0, Skipped: 0, Time elapsed: 1 s - in b.T\n[INFO] Results:\n[INFO] \n"
      "[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0\n"), (203, 202)),
    ("Tests:       5 failed, 5 total\n", (5, 0)),
    ("Tests:       1 failed, 2 skipped, 5 passed, 8 total\n", (6, 5)),
    ("1..3\nok 1\nok 2\nnot ok 3\n# tests 3\n# pass 2\n# fail 1\n", (3, 2)),
    ("lein test a.core\n\nRan 12 tests containing 40 assertions.\n2 failures, 1 errors.\n", (12, 9)),
    ("Test Summary: | Pass  Fail  Total  Time\nDecFP         |  300     2    302  4.1s\n", (302, 300)),
    ("Finished in 0.4 seconds (0.1s async, 0.3s sync)\n3 doctests, 41 tests, 2 failures\n", (41, 39)),
    (("Passed!  - Failed:     0, Passed:    12, Skipped:     1, Total:    13, Duration: 1 s - A.dll\n"
      "Failed!  - Failed:     2, Passed:     5, Skipped:     0, Total:     7, Duration: 1 s - B.dll\n"), (19, 17)),
    ("Test Suite 'All tests' failed at 2026-01-01.\n\t Executed 30 tests, with 3 failures (0 unexpected)\n", (30, 27)),
    ("[ FAIL 1 | WARN 0 | SKIP 2 | PASS 88 ]\n", (89, 88)),
    ("OK (25 tests, 70 assertions)\n", (25, 25)),
    ("FAILURES!\nTests: 25, Assertions: 70, Errors: 1, Failures: 2.\n", (25, 22)),
    ("[info] Passed: Total 40, Failed 0, Errors 0, Passed 40\n[error] Failed: Total 10, Failed 2, Errors 1, Passed 7\n",
     (50, 47)),
    ("00:03 +57 -2: Some tests failed.\n", (59, 57)),
    ("00:01 +12: All tests passed!\n", (12, 12)),
    ("40 successes / 1 failure / 0 errors / 0 pending : 0.2 seconds\n", (41, 40)),
    ("make: Nothing to be done for 'check'.\n", None),
    ("========= no tests ran in 0.01s =========\n", None),
])
def test_runner_summaries(tmp_path, printed, counts):
    log = tmp_path / "test.log"
    log.write_text(printed)
    assert codebase_reading.counted(log) == counts


@pytest.mark.parametrize(("printed", "code", "why"), [
    ("anything\n", None, "timeout"),
    ("ERROR: No matching distribution found for numpy==1.9\n", 1, "dependency"),
    ("fatal: unable to access 'https://example.com/x.git/'\n", 128, "dependency"),
    ("sh: 1: autoreconf: not found\n", 127, "toolchain"),
    ("-- Could NOT find ZLIB (missing: ZLIB_LIBRARY)\nCMake Error at CMakeLists.txt:3\n", 1, "toolchain"),
    ("src/a.c:12:5: error: implicit declaration of function 'f'\nmake: *** [all] Error 1\n", 2, "compile"),
    ("something went wrong\n", 1, "other"),
])
def test_build_failure_class(tmp_path, printed, code, why):
    log = tmp_path / "build.log"
    log.write_text(printed)
    assert codebase_reading.why(code, log) == why
