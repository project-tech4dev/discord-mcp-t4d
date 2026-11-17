from datetime import timedelta

import httpx
import pytest
import respx

from discord_mcp.discord_client import API_BASE, DiscordClient, DiscordError
from tests.fakes import NOW, msg

SECRET_BODY = "SENSITIVE-BODY-TEXT"


class Sleeper:
    def __init__(self):
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def make_client(sleeper=None) -> DiscordClient:
    return DiscordClient("tok", sleep=sleeper or Sleeper())


class Clock:
    """A sleeper that moves a fake clock, so pauses can be measured without waiting."""

    def __init__(self):
        self.t = 100.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.t += seconds


def clocked(clock: Clock, **kw) -> DiscordClient:
    return DiscordClient("tok", sleep=clock.sleep, now=clock.now, **kw)


def page(start_id: int, count: int, start_hours_ago: float) -> list[dict]:
    """Newest-first page of messages, one per hour, going back in time."""
    return [msg(str(start_id - i), "10", hours_ago=start_hours_ago + i) for i in range(count)]


@respx.mock
async def test_pages_backward_until_cutoff():
    route = respx.get(f"{API_BASE}/channels/10/messages")
    first = page(1000, 100, 1)  # 1h .. 100h ago
    second = page(900, 100, 101)  # 101h .. 200h ago (cutoff falls inside)
    route.side_effect = [httpx.Response(200, json=first), httpx.Response(200, json=second)]

    cutoff = NOW - timedelta(hours=150)
    client = make_client()
    result = await client.get_messages_since("10", cutoff)
    await client.aclose()

    assert route.call_count == 2
    assert route.calls[0].request.url.params["limit"] == "100"
    assert "before" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["before"] == "901"
    assert [m["id"] for m in result][0] == "1000"
    assert len(result) == 150  # 1h..150h ago inclusive window
    assert all(int(a["id"]) > int(b["id"]) for a, b in zip(result, result[1:]))


@respx.mock
async def test_stops_when_page_is_short():
    route = respx.get(f"{API_BASE}/channels/10/messages").mock(return_value=httpx.Response(200, json=page(50, 5, 1)))
    client = make_client()
    result = await client.get_messages_since("10", NOW - timedelta(days=7))
    await client.aclose()
    assert route.call_count == 1
    assert len(result) == 5


@respx.mock
async def test_retries_after_429_using_retry_after():
    sleeper = Clock()
    respx.get(f"{API_BASE}/channels/10").side_effect = [
        httpx.Response(429, json={"retry_after": 1.5, "global": False}, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"id": "10", "name": "general", "type": 0}),
    ]
    client = clocked(sleeper)
    ch = await client.get_channel("10")
    await client.aclose()
    assert ch["name"] == "general"
    assert sleeper.slept == [1.5]


@respx.mock
async def test_gives_up_after_repeated_429():
    respx.get(f"{API_BASE}/channels/10").mock(return_value=httpx.Response(429, json={"retry_after": 0.1}))
    client = make_client()
    with pytest.raises(DiscordError) as e:
        await client.get_channel("10")
    await client.aclose()
    assert e.value.status == 429


@respx.mock
async def test_exhausted_bucket_delays_the_next_request_to_that_path_only():
    clock = Clock()
    respx.get(f"{API_BASE}/channels/10").mock(
        return_value=httpx.Response(
            200,
            json={"id": "10"},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.75"},
        )
    )
    respx.get(f"{API_BASE}/channels/11").mock(return_value=httpx.Response(200, json={"id": "11"}))
    client = clocked(clock)
    await client.get_channel("10")
    assert clock.slept == []  # the data comes back at once
    await client.get_channel("11")
    assert clock.slept == []  # another path is not held up
    await client.get_channel("10")
    await client.aclose()
    assert clock.slept == [0.75]


@respx.mock
async def test_a_429_pauses_every_request_on_the_client():
    clock = Clock()
    respx.get(f"{API_BASE}/channels/10").mock(return_value=httpx.Response(429, json={"retry_after": 2.0}))
    respx.get(f"{API_BASE}/channels/11").mock(return_value=httpx.Response(200, json={"id": "11"}))
    client = clocked(clock, max_retries=0)
    with pytest.raises(DiscordError):
        await client.get_channel("10")
    assert clock.slept == []  # no wait after the last attempt
    ch = await client.get_channel("11")
    await client.aclose()
    assert ch["id"] == "11"
    assert clock.slept == [2.0]


@respx.mock
async def test_repeated_429_waits_between_attempts_only():
    clock = Clock()
    route = respx.get(f"{API_BASE}/channels/10").mock(return_value=httpx.Response(429, json={"retry_after": 0.5}))
    client = clocked(clock, max_retries=3)
    with pytest.raises(DiscordError):
        await client.get_channel("10")
    await client.aclose()
    assert route.call_count == 4
    assert clock.slept == [0.5, 0.5, 0.5]


async def test_requests_in_flight_are_capped_across_callers():
    import asyncio

    state = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return httpx.Response(200, json={"id": "x"})

    client = DiscordClient("tok", max_in_flight=3, transport=httpx.MockTransport(handler))
    await asyncio.gather(*(client.get_channel(str(i)) for i in range(12)))
    await client.aclose()
    assert state["peak"] == 3


@respx.mock
async def test_get_current_user_reads_users_me():
    respx.get(f"{API_BASE}/users/@me").mock(return_value=httpx.Response(200, json={"id": "77", "username": "bot"}))
    client = make_client()
    me = await client.get_current_user()
    await client.aclose()
    assert me["id"] == "77"


@respx.mock
async def test_403_raises_with_status_and_no_body():
    respx.get(f"{API_BASE}/channels/10/messages").mock(
        return_value=httpx.Response(403, json={"message": SECRET_BODY, "code": 50001})
    )
    client = make_client()
    with pytest.raises(DiscordError) as e:
        await client.get_messages_since("10", NOW - timedelta(days=7))
    await client.aclose()
    assert e.value.status == 403
    assert SECRET_BODY not in str(e.value)
    assert SECRET_BODY not in repr(e.value)
    assert e.value.__cause__ is None and e.value.__context__ is None


@respx.mock
async def test_network_error_becomes_discord_error_without_chain():
    respx.get(f"{API_BASE}/channels/10").mock(side_effect=httpx.ConnectError("boom"))
    client = make_client()
    with pytest.raises(DiscordError) as e:
        await client.get_channel("10")
    await client.aclose()
    assert e.value.status is None
    assert e.value.__cause__ is None


@respx.mock
async def test_sends_bot_authorization_and_only_get():
    route = respx.get(f"{API_BASE}/guilds/900/roles").mock(return_value=httpx.Response(200, json=[]))
    client = make_client()
    await client.get_guild_roles("900")
    await client.aclose()
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bot tok"
    assert all(call.request.method == "GET" for call in respx.calls)


def test_client_has_no_write_methods():
    forbidden = {"post", "put", "patch", "delete", "request", "send"}
    public = {name for name in dir(DiscordClient) if not name.startswith("_")}
    assert not public & forbidden
    for name in public:
        assert not any(word in name for word in ("create", "edit", "send", "delete", "react", "post"))


@respx.mock
async def test_get_guild_returns_owner():
    respx.get(f"{API_BASE}/guilds/900").mock(return_value=httpx.Response(200, json={"id": "900", "owner_id": "1"}))
    client = make_client()
    assert (await client.get_guild("900"))["owner_id"] == "1"
    await client.aclose()


@respx.mock
async def test_thread_membership_true_false_and_error():
    respx.get(f"{API_BASE}/channels/40/thread-members/1001").mock(return_value=httpx.Response(200, json={"user_id": "1001"}))
    respx.get(f"{API_BASE}/channels/40/thread-members/1002").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{API_BASE}/channels/40/thread-members/1003").mock(return_value=httpx.Response(403, json={}))
    client = make_client()
    assert await client.is_thread_member("40", "1001") is True
    assert await client.is_thread_member("40", "1002") is False
    with pytest.raises(DiscordError):
        await client.is_thread_member("40", "1003")
    await client.aclose()


@respx.mock
async def test_messages_since_stops_at_max_count():
    route = respx.get(f"{API_BASE}/channels/10/messages")
    route.side_effect = [httpx.Response(200, json=page(1000 - 100 * i, 100, 1 + 100 * i)) for i in range(3)]
    client = make_client()
    result = await client.get_messages_since("10", NOW - timedelta(days=30), max_count=150)
    await client.aclose()
    assert len(result) == 150
    assert route.call_count == 2


@respx.mock
async def test_get_message_path():
    route = respx.get(f"{API_BASE}/channels/10/messages/55").mock(return_value=httpx.Response(200, json=msg("55", "10")))
    client = make_client()
    assert (await client.get_message("10", "55"))["id"] == "55"
    await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_get_oldest_message_uses_after_zero():
    route = respx.get(f"{API_BASE}/channels/10/messages").mock(return_value=httpx.Response(200, json=[msg("7", "10")]))
    client = make_client()
    assert (await client.get_oldest_message("10"))["id"] == "7"
    await client.aclose()
    assert route.calls[0].request.url.params["after"] == "0"
    assert route.calls[0].request.url.params["limit"] == "1"


@respx.mock
async def test_get_oldest_message_empty_channel_is_none():
    respx.get(f"{API_BASE}/channels/10/messages").mock(return_value=httpx.Response(200, json=[]))
    client = make_client()
    assert await client.get_oldest_message("10") is None
    await client.aclose()


# --- review fixes: pause re-checked, waits capped, odd values, shared buckets ---------


async def test_requests_queued_for_a_slot_wait_out_a_pause():
    import asyncio
    import time

    sent: dict[str, float] = {}
    refused_at: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/0") and not refused_at:
            await asyncio.sleep(0.02)
            refused_at.append(time.monotonic())
            return httpx.Response(429, json={"retry_after": 0.2})
        sent.setdefault(path, time.monotonic())
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"id": "x"})

    client = DiscordClient("tok", max_in_flight=1, transport=httpx.MockTransport(handler))
    await asyncio.gather(*(client.get_channel(str(i)) for i in range(4)))
    await client.aclose()
    resume = refused_at[0] + 0.2
    assert all(t >= resume - 0.005 for p, t in sent.items() if not p.endswith("/0"))


@respx.mock
async def test_a_very_long_pause_fails_fast_instead_of_hanging():
    clock = Clock()
    respx.get(f"{API_BASE}/channels/10").mock(return_value=httpx.Response(429, json={"retry_after": 3600}))
    respx.get(f"{API_BASE}/channels/11").mock(return_value=httpx.Response(200, json={"id": "11"}))
    client = clocked(clock)
    with pytest.raises(DiscordError) as e:
        await client.get_channel("10")
    assert e.value.status == 429
    with pytest.raises(DiscordError):
        await client.get_channel("11")  # still paused: refused at once, nothing sent
    await client.aclose()
    assert clock.slept == []


@pytest.mark.parametrize("body", [{"retry_after": None}, {"retry_after": [1]}, {"retry_after": "Infinity"}])
@respx.mock
async def test_odd_retry_after_values_are_bounded(body):
    clock = Clock()
    respx.get(f"{API_BASE}/channels/10").side_effect = [
        httpx.Response(429, json=body),
        httpx.Response(200, json={"id": "10"}),
    ]
    client = clocked(clock)
    await client.get_channel("10")
    await client.aclose()
    assert len(clock.slept) == 1 and 0 < clock.slept[0] <= 30


@respx.mock
async def test_message_lookups_in_one_channel_share_a_bucket():
    clock = Clock()
    respx.get(f"{API_BASE}/channels/10/messages/1").mock(
        return_value=httpx.Response(
            200, json={"id": "1"}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.5"}
        )
    )
    respx.get(f"{API_BASE}/channels/10/messages/2").mock(return_value=httpx.Response(200, json={"id": "2"}))
    client = clocked(clock)
    await client.get_message("10", "1")
    await client.get_message("10", "2")
    await client.aclose()
    assert clock.slept == [0.5]


@respx.mock
async def test_messages_can_start_before_a_given_message():
    # Issue #7: a second read continues from the oldest message of the first.
    route = respx.get(f"{API_BASE}/channels/10/messages").mock(return_value=httpx.Response(200, json=page(50, 5, 1)))
    client = make_client()
    result = await client.get_messages_since("10", NOW - timedelta(days=7), max_count=5, before="51")
    await client.aclose()
    assert route.calls[0].request.url.params["before"] == "51"
    assert len(result) == 5
