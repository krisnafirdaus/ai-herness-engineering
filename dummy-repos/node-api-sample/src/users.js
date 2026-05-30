"use strict";

// User service (stands in for an API controller). createUser performs NO
// request validation — the tests encode the desired behaviour and fail until
// the harness adds validation.

const _users = new Map();

function createUser(payload) {
  const id = _users.size + 1;
  const user = { id, email: payload.email, name: payload.name };
  _users.set(id, user);
  return user;
}

function getUser(id) {
  return _users.get(id);
}

module.exports = { createUser, getUser };
