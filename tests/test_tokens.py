from discord_mcp.tokens import ReplayGuard, TokenBox

KEY = bytes(range(32))


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_round_trip_and_expiry():
    clock = Clock()
    box = TokenBox(KEY, clock=clock)
    token = box.seal("access", {"sub": "1001"}, ttl=60)
    assert box.open("access", token)["sub"] == "1001"
    clock.t += 61
    assert box.open("access", token) is None


def test_token_of_one_kind_cannot_be_used_as_another():
    box = TokenBox(KEY)
    token = box.seal("refresh", {"sub": "1001"}, ttl=60)
    assert box.open("access", token) is None
    assert box.open("code", token) is None


def test_tampered_or_garbage_token_is_rejected():
    box = TokenBox(KEY)
    token = box.seal("access", {"sub": "1001"}, ttl=60)
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    assert box.open("access", flipped) is None
    assert box.open("access", "not-a-token") is None
    assert box.open("access", "") is None


def test_other_key_cannot_open():
    token = TokenBox(KEY).seal("access", {"sub": "1001"}, ttl=60)
    assert TokenBox(bytes(32)).open("access", token) is None


def test_token_does_not_reveal_payload():
    token = TokenBox(KEY).seal("access", {"sub": "1001-visible?"}, ttl=60)
    assert "1001" not in token


def test_seal_adds_unique_id_and_expiry():
    box = TokenBox(KEY, clock=Clock())
    a = box.open("code", box.seal("code", {}, ttl=60))
    b = box.open("code", box.seal("code", {}, ttl=60))
    assert a["jti"] != b["jti"]
    assert a["exp"] == 1_000_060


def test_replay_guard_allows_once_and_forgets_after_expiry():
    clock = Clock()
    guard = ReplayGuard(clock=clock)
    assert guard.use("abc", exp=clock.t + 10) is True
    assert guard.use("abc", exp=clock.t + 10) is False
    clock.t += 11
    guard.use("other", exp=clock.t + 10)  # triggers pruning
    assert "abc" not in guard._seen
