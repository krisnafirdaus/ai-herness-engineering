"""In-memory user service (stands in for an API controller).

NOTE: `create_user` performs NO request validation — malformed payloads are
silently accepted (or crash with a raw KeyError). The accompanying tests encode
the desired validation behaviour and currently FAIL, which is the task the
harness is asked to implement.
"""

_USERS = {}


def create_user(payload):
    user_id = len(_USERS) + 1
    user = {"id": user_id, "email": payload["email"], "name": payload["name"]}
    _USERS[user_id] = user
    return user


def get_user(user_id):
    return _USERS.get(user_id)
