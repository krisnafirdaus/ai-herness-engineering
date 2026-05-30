# python-api-sample (harness target)

A deliberately tiny Python "backend" used to exercise the agent harness with
**zero external dependencies** — the checks use only the standard library, so a
grader needs nothing installed beyond Python 3.10+.

* `app/users.py` — user service with **no request validation** (the work to do).
* `tests/test_users.py` — `unittest` spec; two tests fail until validation exists.
* `scripts/lint.py` — stdlib linter.

Checks the harness runs for this repo:

```bash
python -m unittest discover -s tests -t .
python scripts/lint.py
```

Suggested task:

```
Add request validation to the user creation endpoint
```
