"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { createUser, getUser } = require("../src/users");

test("creates a valid user", () => {
  const u = createUser({ email: "ada@example.com", name: "Ada" });
  assert.strictEqual(u.id, 1);
  assert.strictEqual(getUser(1).email, "ada@example.com");
});

test("rejects missing email", () => {
  assert.throws(() => createUser({ name: "Ada" }));
});

test("rejects invalid email", () => {
  assert.throws(() => createUser({ email: "not-an-email", name: "Ada" }));
});
