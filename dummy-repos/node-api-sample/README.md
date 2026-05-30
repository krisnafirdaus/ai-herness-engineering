# node-api-sample (harness target)

A dependency-free Node.js "backend" target for the harness — no `npm install`
required (uses the built-in `node:test` runner and a stdlib linter).

* `src/users.js` — user service with **no request validation** (the work to do).
* `test/users.test.js` — `node:test` spec; two tests fail until validation exists.
* `scripts/lint.js` — dependency-free linter.

Checks the harness runs for this repo:

```bash
node --test
node scripts/lint.js
```

Suggested task:

```
Add validation to the user creation endpoint
```
