from datetime import timedelta
import json
import logging

import pytest

from discord_mcp.config import ChannelEntry, Config, Limits
from discord_mcp.digest import Digest
from discord_mcp.matching import Identity
from tests.fakes import GUILD_ID, ME, NOW, ROLE_DEV, FakeClient, channel, msg, user

IDENT = Identity(user_id=ME, role_ids=frozenset({ROLE_DEV}))


def config(*ids: str, **limits) -> Config:
    return Config(
        allowlist={i: ChannelEntry(id=i, name=None, phase=1) for i in ids},
        deny=frozenset(),
        limits=Limits(**limits),
    )


def make(client: FakeClient, cfg: Config) -> Digest:
    return Digest(client, guild_id=GUILD_ID, config=cfg, clock=lambda: NOW)


def ping_ids(payload: dict) -> list[str]:
    return [p["link"].rsplit("/", 1)[1] for c in payload["conversations"] for p in c["pings"]]


def audit_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "discord_mcp.audit"]


@pytest.fixture
def client() -> FakeClient:
    c = FakeClient()
    c.add_channel(channel("1", "Team", 4), [])
    c.add_channel(
        channel("10", "general", parent_id="1"),
        [
            msg("100", "10", content=f"<@{ME}> please review", mentions=[user(ME, "Me")], hours_ago=30),
            msg("101", "10", content="sure", hours_ago=29),
        ],
    )
    c.add_channel(channel("20", "secret"), [msg("200", "20", mentions=[user(ME)], hours_ago=2)])
    return c


async def test_stage1_acceptance_test_message_appears_with_right_reason(client):
    result = await make(client, config("10")).get_my_pings(IDENT)
    payload = result.payload
    convs = payload["conversations"]
    assert len(convs) == 1
    assert convs[0]["status"] == "needs_you"
    assert convs[0]["pings"][0]["reason"] == "mention"
    assert convs[0]["pings"][0]["text"] == "@Me please review"
    assert ping_ids(payload) == ["100"]
    assert payload["counts"]["needs_you"] == 1
    assert payload["truncated"] is False
    assert result.channel_ids == ["10"]


async def test_payload_has_no_follow_up_key(client):
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "follow_up" not in json.dumps(result.payload)
    assert "pings" not in result.payload


async def test_channel_off_allowlist_is_never_read(client):
    result = await make(client, config("10")).get_my_pings(IDENT, channel_ids=["20"])
    assert result.payload["conversations"] == []
    assert "messages:20" not in client.calls


async def test_window_excludes_older_messages(client):
    result = await make(client, config("10")).get_my_pings(IDENT, days=1)
    assert result.payload["conversations"] == []


async def test_days_above_maximum_is_rejected(client):
    with pytest.raises(ValueError):
        await make(client, config("10")).get_my_pings(IDENT, days=31)


async def test_bot_403_drops_channel_and_logs_only_its_id(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    client.fail["20"] = 403
    result = await make(client, config("10", "20")).get_my_pings(IDENT)
    assert ping_ids(result.payload) == ["100"]
    assert result.payload["skipped_channels"] == 1
    lines = audit_lines(caplog)
    assert lines == [
        {
            "user_id": ME,
            "tool": "get_my_pings",
            "channel_ids": ["20"],
            "item_count": 0,
            "duration_ms": 0,
            "status": "dropped:http_403",
        }
    ]


async def test_allowlisted_channel_missing_from_guild_is_dropped(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    result = await make(client, config("10", "99")).get_my_pings(IDENT)
    assert result.payload["skipped_channels"] == 1
    assert audit_lines(caplog)[0]["channel_ids"] == ["99"]
    assert audit_lines(caplog)[0]["status"] == "dropped:not_found"


async def test_unrecognised_channel_type_is_dropped(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    client.add_channel(channel("30", "voice", 2), [])
    result = await make(client, config("10", "30")).get_my_pings(IDENT)
    assert result.payload["skipped_channels"] == 1
    assert audit_lines(caplog)[0]["status"] == "dropped:unknown_type"


async def test_public_threads_of_allowlisted_channel_are_read(client):
    t = channel("40", "deploy", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("400", "40", content="<@&R> can you deploy?", mention_roles=[ROLE_DEV], hours_ago=3)])
    client.active_threads = [t, channel("41", "other", 11, parent_id="20")]
    client.add_channel(client.active_threads[1], [msg("410", "41", mentions=[user(ME)], hours_ago=1)])
    result = await make(client, config("10")).get_my_pings(IDENT)
    ids = ping_ids(result.payload)
    assert ids == ["400", "100"]
    assert result.payload["conversations"][0]["where"] == "general › deploy"
    assert "messages:41" not in client.calls


async def test_archived_public_threads_are_read(client):
    t = channel("42", "old", 11, parent_id="10", thread_metadata={"archived": True})
    client.add_channel(t, [msg("420", "42", mentions=[user(ME)], hours_ago=10)])
    client.archived_threads["10"] = [t]
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "420" in ping_ids(result.payload)


async def test_private_thread_needs_membership(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    t = channel("43", "hush", 12, parent_id="10")
    client.add_channel(t, [msg("430", "43", mentions=[user(ME)], hours_ago=1)])
    client.active_threads = [t]
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "430" not in ping_ids(result.payload)
    assert "messages:43" not in client.calls
    assert audit_lines(caplog)[0]["channel_ids"] == ["43"]
    assert audit_lines(caplog)[0]["status"] == "dropped:no_access"

    client.thread_members["43"] = {ME}
    result = await make(client, config("10")).get_my_pings(IDENT)
    assert "430" in ping_ids(result.payload)


async def test_list_my_channels_returns_id_name_category(client):
    result = await make(client, config("10", "20")).list_my_channels(IDENT)
    assert result.payload["channels"] == [
        {"id": "10", "name": "general", "category": "Team"},
        {"id": "20", "name": "secret", "category": None},
    ]


async def test_get_channel_messages_newest_first_and_limited(client):
    result = await make(client, config("10")).get_channel_messages(IDENT, "10", days=3, limit=1)
    assert [m["id"] for m in result.payload["messages"]] == ["101"]
    assert result.payload["truncated"] is True


async def test_get_channel_messages_refuses_channel_off_allowlist(client):
    with pytest.raises(PermissionError):
        await make(client, config("10")).get_channel_messages(IDENT, "20")
    assert "messages:20" not in client.calls


async def test_get_channel_messages_limit_above_maximum_is_rejected(client):
    with pytest.raises(ValueError):
        await make(client, config("10")).get_channel_messages(IDENT, "10", limit=301)


async def test_get_channel_messages_limit_true_is_rejected(client):
    # bool is an int in Python; like days, limit must be a real number.
    with pytest.raises(ValueError, match="limit must be between 1 and 300"):
        await make(client, config("10")).get_channel_messages(IDENT, "10", limit=True)
    assert not any(c.startswith("messages:") for c in client.calls)


async def test_resolve_identity_reads_member_roles(client):
    ident = await make(client, config()).resolve_identity(ME)
    assert ident == Identity(user_id=ME, role_ids=frozenset({ROLE_DEV}))


async def test_fifteen_ping_fixture_is_small():
    """Modelled on the first live test: 8 pings in one channel that I answered, 4 threads, 2 unanswered."""
    from tests.fakes import OTHER, THIRD

    c = FakeClient()
    grants = []
    for i in range(8):
        who = OTHER if i % 2 else THIRD
        grants.append(msg(str(1000 + 2 * i), "60", who, f"<@{ME}> point {i} " + "x" * 300, mentions=[user(ME)], hours_ago=100 - 8 * i))
        grants.append(msg(str(1001 + 2 * i), "60", ME, f"answer {i} " + "y" * 300, hours_ago=99 - 8 * i))
    c.add_channel(channel("60", "grants"), grants)
    threads = []
    for n, answered in [("61", True), ("62", True), ("63", False), ("64", False)]:
        t = channel(n, f"topic {n}", 11, parent_id="60", thread_metadata={"archived": False})
        body = [msg(f"{n}0", n, OTHER, f"<@{ME}> look " + "z" * 300, mentions=[user(ME)], hours_ago=20)]
        body += [msg(f"{n}{k}", n, THIRD, "chat " + "w" * 300, hours_ago=19 - k) for k in range(1, 6)]
        if answered:
            body.append(msg(f"{n}9", n, ME, "done", hours_ago=10))
        c.add_channel(t, body)
        threads.append(t)
    c.active_threads = threads
    result = await make(c, config("60")).get_my_pings(IDENT, days=14)
    payload = result.payload
    assert payload["counts"]["needs_you"] == 2
    assert sorted(r["where"] for r in payload["conversations"] if r["status"] == "needs_you") == [
        "grants › topic 63",
        "grants › topic 64",
    ]
    assert len(json.dumps(payload)) < 20000


# --- get_conversation -----------------------------------------------------------

from discord_mcp.errors import AccessDenied, InputError  # noqa: E402

LINK = f"https://discord.com/channels/{GUILD_ID}"


def conv_ids(result) -> list[str]:
    return [m["link"].rsplit("/", 1)[1] for m in result.payload["messages"]]


async def test_get_conversation_by_ping_link_returns_all_its_messages(client):
    result = await make(client, config("10")).get_conversation(IDENT, f"{LINK}/10/100")
    assert conv_ids(result) == ["100", "101"]
    assert result.payload["where"] == "general"
    assert result.payload["truncated"] is False
    assert result.channel_ids == ["10"]


async def test_get_conversation_by_id_matches_link(client):
    d = make(client, config("10"))
    row = (await d.get_my_pings(IDENT)).payload["conversations"][0]
    by_id = await d.get_conversation(IDENT, row["id"])
    assert conv_ids(by_id) == ["100", "101"]


async def test_get_conversation_by_my_reply_link_finds_same_conversation(client):
    client.add_channel(
        channel("10", "general", parent_id="1"),
        [*client.messages["10"], msg("102", "10", ME, "done", hours_ago=28)],
    )
    result = await make(client, config("10")).get_conversation(IDENT, f"{LINK}/10/102")
    assert conv_ids(result) == ["100", "101", "102"]


async def test_link_to_channel_off_allowlist_is_denied_without_fetch(client):
    with pytest.raises(AccessDenied):
        await make(client, config("10")).get_conversation(IDENT, f"{LINK}/20/200")
    assert "messages:20" not in client.calls


async def test_link_to_private_thread_not_a_member_is_denied(client):
    t = channel("43", "hush", 12, parent_id="10")
    client.add_channel(t, [msg("430", "43", mentions=[user(ME)], hours_ago=1)])
    client.active_threads = [t]
    with pytest.raises(AccessDenied):
        await make(client, config("10")).get_conversation(IDENT, f"{LINK}/43/430")
    assert "messages:43" not in client.calls


async def test_link_from_other_guild_is_input_error(client):
    with pytest.raises(InputError):
        await make(client, config("10")).get_conversation(IDENT, "https://discord.com/channels/999/10/100")


@pytest.mark.parametrize("bad", ["hello", f"{LINK}/10", "10/abc", ""])
async def test_malformed_link_is_input_error(client, bad):
    with pytest.raises(InputError):
        await make(client, config("10")).get_conversation(IDENT, bad)


async def test_message_outside_30_days_is_input_error(client):
    client.add_channel(
        channel("10", "general", parent_id="1"), [*client.messages["10"], msg("90", "10", hours_ago=800)]
    )
    with pytest.raises(InputError):
        await make(client, config("10")).get_conversation(IDENT, f"{LINK}/10/90")


async def test_get_channel_messages_fetches_only_up_to_limit(client):
    await make(client, config("10")).get_channel_messages(IDENT, "10", limit=5)
    assert client.max_counts["10"] == 6  # limit plus one, to know whether more exist


async def test_every_ping_fetch_is_capped(client):
    d = make(client, config("10", overview_max_messages=777))
    await d.get_my_pings(IDENT)
    assert client.max_counts["10"] == 777
    client.max_counts.clear()
    await d.get_conversation(IDENT, f"{LINK}/10/100")
    assert client.max_counts["10"] == 777


async def test_ping_fetch_cap_hit_reports_truncated(client):
    result = await make(client, config("10", overview_max_messages=1)).get_my_pings(IDENT)
    assert result.payload["truncated"] is True


# --- channel overview and search --------------------------------------------------

from tests.fakes import OTHER, THIRD  # noqa: E402


def overview_client(client):
    t = channel("40", "acme proposal", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [
        msg("401", "40", OTHER, "draft at https://docs.google.com/d/x", hours_ago=3),
        msg("402", "40", THIRD, "looks good", hours_ago=2),
    ])
    client.active_threads = [t]
    return client


async def test_overview_lists_threads_and_main_topics(client):
    result = await make(overview_client(client), config("10")).get_channel_overview(IDENT, "10", days=7)
    p = result.payload
    assert p["channel"] == "general"
    assert p["counts"] == {"thread": 1, "main": 1}
    assert [t["kind"] for t in p["topics"]] == ["thread", "main"]  # the thread has a strong signal
    assert p["topics"][0]["id"] == "t:40"
    assert set(result.channel_ids) == {"10", "40"}


async def test_overview_thread_older_than_window_shows_true_starter(client):
    old = msg("5", "10", OTHER, "kickoff: new grant", hours_ago=900, thread={"id": "5"})
    client.add_channel(channel("10", "general", parent_id="1"), [*client.messages["10"], old])
    t = channel("5", "old grant", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("501", "5", THIRD, "any update?", hours_ago=4)])
    client.active_threads = [t]
    p = (await make(client, config("10")).get_channel_overview(IDENT, "10", days=30)).payload
    row = next(r for r in p["topics"] if r["id"] == "t:5")
    assert row["opening"]["link"].endswith("/10/5")
    assert row["opening"]["text"] == "kickoff: new grant"
    assert "message:10/5" in client.calls


async def test_overview_thread_not_from_parent_uses_oldest(client):
    t = channel("6", "standalone", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("601", "6", OTHER, "first post", hours_ago=900), msg("602", "6", THIRD, "later", hours_ago=4)])
    client.active_threads = [t]
    p = (await make(client, config("10")).get_channel_overview(IDENT, "10", days=30)).payload
    row = next(r for r in p["topics"] if r["id"] == "t:6")
    assert row["opening"]["text"] == "first post"
    assert "oldest:6" in client.calls


async def test_overview_only_mine(client):
    overview_client(client)
    p = (await make(client, config("10")).get_channel_overview(IDENT, "10", only_mine=True)).payload
    assert [t["kind"] for t in p["topics"]] == ["main"]  # pinged in main; not in the thread
    assert p["topics"][0]["your_part"] == "pinged"


async def test_overview_watch_words_from_config_are_strong(client):
    client.add_channel(channel("10", "general", parent_id="1"), [
        msg("110", "10", OTHER, "start", hours_ago=10), msg("111", "10", OTHER, "LOI sent", hours_ago=9),
        msg("112", "10", OTHER, "a", hours_ago=8), msg("113", "10", OTHER, "b", hours_ago=7),
    ])
    cfg = Config(allowlist={"10": ChannelEntry("10", None, 1, watch=("LOI",))}, deny=frozenset(), limits=Limits())
    p = (await make(client, cfg).get_channel_overview(IDENT, "10")).payload
    assert p["topics"][0]["key_messages"][0]["matched"] == ["word:LOI"]


async def test_overview_rejects_channel_off_allowlist_without_fetch(client):
    with pytest.raises(AccessDenied):
        await make(client, config("10")).get_channel_overview(IDENT, "20")
    assert "messages:20" not in client.calls


async def test_overview_days_above_30_rejected(client):
    with pytest.raises(InputError):
        await make(client, config("10")).get_channel_overview(IDENT, "10", days=31)


async def test_overview_forum_channel_threads_only(client):
    forum = channel("70", "ideas", 15)
    client.add_channel(forum, [])
    t = channel("71", "idea one", 11, parent_id="70", thread_metadata={"archived": False})
    client.add_channel(t, [msg("711", "71", OTHER, "what about this", hours_ago=2)])
    client.active_threads = [t]
    p = (await make(client, config("70")).get_channel_overview(IDENT, "70")).payload
    assert [r["id"] for r in p["topics"]] == ["t:71"]
    assert "messages:70" not in client.calls


async def test_overview_bad_look_for_is_input_error(client):
    with pytest.raises(InputError):
        await make(client, config("10")).get_channel_overview(IDENT, "10", look_for=[f"w{i:02d}" for i in range(21)])


async def test_search_finds_thread_by_title(client):
    overview_client(client)
    p = (await make(client, config("10")).search(IDENT, "10", ["acme"])).payload
    assert [r["id"] for r in p["results"]] == ["t:40"]
    assert p["results"][0]["title_match"] is True
    assert p["match_count"] == 0


async def test_search_words_not_in_audit_log(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    overview_client(client)
    client.fail["40"] = 403  # forces a drop line during the search
    await make(client, config("10")).search(IDENT, "10", ["zebrafish"])
    assert "zebrafish" not in caplog.text


async def test_search_requires_words(client):
    for bad in [[], ["x"], "acme"]:
        with pytest.raises(InputError):
            await make(client, config("10")).search(IDENT, "10", bad)


# --- get_conversation opens overview and search rows ------------------------------


async def test_get_conversation_thread_id_returns_thread_with_starter(client):
    old = msg("5", "10", OTHER, "kickoff: new grant", hours_ago=900, thread={"id": "5"})
    client.add_channel(channel("10", "general", parent_id="1"), [*client.messages["10"], old])
    t = channel("5", "old grant", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("501", "5", THIRD, "any update?", hours_ago=4)])
    client.active_threads = [t]
    p = (await make(client, config("10")).get_conversation(IDENT, "t:5")).payload
    assert [m["text"] for m in p["messages"]] == ["kickoff: new grant", "any update?"]
    assert p["id"] == "t:5"


async def test_get_conversation_main_topic_id(client):
    p = (await make(client, config("10")).get_conversation(IDENT, "m:10/100")).payload
    assert conv_ids_payload(p) == ["100", "101"]
    assert p["id"] == "m:10/100"


async def test_get_conversation_link_not_in_ping_conversation_falls_back_to_main_topic(client):
    client.add_channel(
        channel("10", "general", parent_id="1"),
        [*client.messages["10"], msg("105", "10", THIRD, "unrelated", hours_ago=2)],
    )
    p = (await make(client, config("10")).get_conversation(IDENT, f"{LINK}/10/105")).payload
    assert conv_ids_payload(p) == ["105"]
    assert p["id"] == "m:10/105"


async def test_get_conversation_link_in_unpinged_thread_falls_back_to_thread(client):
    overview_client(client)
    p = (await make(client, config("10")).get_conversation(IDENT, f"{LINK}/40/402")).payload
    assert p["id"] == "t:40"


async def test_get_conversation_topic_id_under_hidden_channel_denied_without_fetch(client):
    t = channel("21", "hidden thread", 11, parent_id="20", thread_metadata={"archived": False})
    client.add_channel(t, [msg("211", "21", mentions=[user(ME)], hours_ago=1)])
    client.active_threads = [t]
    d = make(client, config("10"))
    for bad in ["t:21", "m:20/200"]:
        with pytest.raises(AccessDenied):
            await d.get_conversation(IDENT, bad)
    assert "messages:21" not in client.calls
    assert "messages:20" not in client.calls


@pytest.mark.parametrize("bad", ["t:", "t:abc", "m:10", "m:10/x", "x:1", "t:5/6"])
async def test_get_conversation_malformed_topic_ids(client, bad):
    with pytest.raises(InputError):
        await make(client, config("10")).get_conversation(IDENT, bad)


def conv_ids_payload(p: dict) -> list[str]:
    return [m["link"].rsplit("/", 1)[1] for m in p["messages"]]


# --- final review fixes -----------------------------------------------------------


def busy_main(client, n=150, every_h=5.0):
    msgs = [msg(str(1000 + i), "10", OTHER, f"m{i}", hours_ago=n * every_h - i * every_h) for i in range(n)]
    client.add_channel(channel("10", "general", parent_id="1"), msgs)


async def test_main_topic_id_opens_exactly_the_rows_messages(client):
    busy_main(client)  # a message every 5h for ~31 days: cut by the 72h cap only
    d = make(client, config("10"))
    ov = (await d.get_channel_overview(IDENT, "10", days=7)).payload
    row = next(r for r in ov["topics"] if r["kind"] == "main")
    opened = (await d.get_conversation(IDENT, row["id"])).payload
    assert opened["messages"][0]["link"] == row["opening"]["link"]
    assert opened["messages"][-1]["link"] == row["latest"][-1]["link"]
    assert len(opened["messages"]) == row["message_count"]


async def test_opening_a_thread_fetches_only_that_thread(client):
    overview_client(client)
    other = channel("41", "other", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(other, [msg("411", "41", OTHER, "x", hours_ago=1)])
    client.active_threads.append(other)
    await make(client, config("10")).get_conversation(IDENT, "t:40")
    assert "messages:41" not in client.calls
    assert "messages:10" not in client.calls


async def test_link_into_thread_idle_20_days_opens_it(client):
    sent = int((NOW.timestamp() - 480 * 3600) * 1000) - 1420070400000
    t = channel("44", "quiet", 11, parent_id="10", thread_metadata={"archived": True}, last_message_id=str(sent << 22))
    client.add_channel(t, [msg("441", "44", OTHER, "waiting on the draft", hours_ago=480)])
    client.archived_threads["10"] = [t]
    p = (await make(client, config("10")).get_conversation(IDENT, f"{LINK}/44/441")).payload
    assert p["id"] == "t:44"


async def test_get_channel_messages_truncated_when_system_messages_fill_the_cap(client):
    msgs = [msg(str(300 + i), "10", OTHER, f"m{i}", hours_ago=50 - i * 0.1) for i in range(10)]
    msgs += [msg(str(400 + i), "10", OTHER, "", hours_ago=1 - i * 0.01, mtype=7) for i in range(3)]
    client.add_channel(channel("10", "general", parent_id="1"), msgs)
    p = (await make(client, config("10")).get_channel_messages(IDENT, "10", days=3, limit=5)).payload
    assert p["truncated"] is True


async def test_overview_caps_threads_and_marks_truncated(client):
    threads = []
    for i in range(6):
        t = channel(str(80 + i), f"t{i}", 11, parent_id="10", thread_metadata={"archived": False})
        client.add_channel(t, [msg(str(800 + i), str(80 + i), OTHER, "hi", hours_ago=10 - i)])
        threads.append(t)
    client.active_threads = threads
    p = (await make(client, config("10", overview_max_threads=4)).get_channel_overview(IDENT, "10")).payload
    assert p["counts"]["thread"] == 4
    assert p["truncated"] is True
    assert "messages:80" not in client.calls  # the oldest threads are not fetched


# --- allow all channels, with a channel deny list ---------------------------------

from discord_mcp.permissions import VIEW_CHANNEL  # noqa: E402


def allow_all(*deny: str, **limits) -> Config:
    return Config(
        allowlist={}, deny=frozenset(), limits=Limits(**limits), allow_all_channels=True, deny_channels=frozenset(deny)
    )


async def test_allow_all_reads_every_visible_channel(client):
    d = make(client, allow_all())
    names = [c["name"] for c in (await d.list_my_channels(IDENT)).payload["channels"]]
    assert sorted(names) == ["general", "secret"]
    assert sorted(ping_ids((await d.get_my_pings(IDENT)).payload)) == ["100", "200"]


async def test_denied_channel_is_never_read(client):
    d = make(client, allow_all("20"))
    assert [c["name"] for c in (await d.list_my_channels(IDENT)).payload["channels"]] == ["general"]
    assert ping_ids((await d.get_my_pings(IDENT)).payload) == ["100"]
    for call in (
        d.get_channel_overview(IDENT, "20"),
        d.search(IDENT, "20", ["hello"]),
        d.get_channel_messages(IDENT, "20"),
        d.get_conversation(IDENT, f"{LINK}/20/200"),
        d.get_conversation(IDENT, "m:20/200"),
    ):
        with pytest.raises(AccessDenied):
            await call
    assert "messages:20" not in client.calls


async def test_denied_category_denies_its_channels(client):
    d = make(client, allow_all("1"))
    assert [c["name"] for c in (await d.list_my_channels(IDENT)).payload["channels"]] == ["secret"]
    with pytest.raises(AccessDenied):
        await d.get_channel_overview(IDENT, "10")
    assert "messages:10" not in client.calls


async def test_threads_under_denied_channel_and_denied_threads_are_not_read(client):
    t = channel("40", "deploy", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("400", "40", mentions=[user(ME)], hours_ago=3)])
    u = channel("41", "other", 11, parent_id="20", thread_metadata={"archived": False})
    client.add_channel(u, [msg("410", "41", mentions=[user(ME)], hours_ago=3)])
    client.active_threads = [t, u]
    await make(client, allow_all("10", "41")).get_my_pings(IDENT)
    assert "messages:40" not in client.calls
    assert "messages:41" not in client.calls
    with pytest.raises(AccessDenied):
        await make(client, allow_all("41")).get_conversation(IDENT, "t:41")


async def test_allow_all_does_not_count_or_log_channels_the_person_cannot_see(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    hidden = channel("30", "hidden", permission_overwrites=[{"id": GUILD_ID, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)}])
    client.add_channel(hidden, [msg("300", "30", mentions=[user(ME)], hours_ago=1)])
    result = await make(client, allow_all()).get_my_pings(IDENT)
    assert result.payload["skipped_channels"] == 0
    assert "messages:30" not in client.calls
    assert all("30" not in line["channel_ids"] for line in audit_lines(caplog))


async def test_overview_works_for_channel_without_allowlist_entry(client):
    p = (await make(client, allow_all()).get_channel_overview(IDENT, "20")).payload
    assert p["channel"] == "secret"


# --- thread started on its own (type 18 "thread created" message in the parent) ----


async def test_thread_created_on_its_own_uses_its_oldest_message(client):
    created = msg("7", "10", OTHER, "", hours_ago=900, mtype=18)
    client.add_channel(channel("10", "general", parent_id="1"), [*client.messages["10"], created])
    t = channel("7", "standalone", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("701", "7", OTHER, "real first post", hours_ago=899), msg("702", "7", THIRD, "recent", hours_ago=3)])
    client.active_threads = [t]
    p = (await make(client, config("10")).get_channel_overview(IDENT, "10", days=30)).payload
    row = next(r for r in p["topics"] if r["id"] == "t:7")
    assert row["opening"]["text"] == "real first post"
    assert "oldest:7" in client.calls


# --- allow-all review fixes -----------------------------------------------------------

from discord_mcp.discord_client import DiscordError  # noqa: E402


async def test_allow_all_server_lookup_failure_is_an_error_not_an_empty_inbox(client):
    client.fail_guild = 500
    with pytest.raises(DiscordError):
        await make(client, allow_all()).get_my_pings(IDENT)


async def test_allowlist_server_lookup_failure_is_an_error_not_an_empty_inbox(client):
    # Issue #6: the allowlist mode must fail the call too, not answer "nothing needs you".
    client.fail_guild = 429
    with pytest.raises(DiscordError):
        await make(client, config("10")).get_my_pings(IDENT)


async def test_allow_all_private_thread_person_cannot_see_is_not_counted(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    t = channel("43", "hush", 12, parent_id="10")
    client.add_channel(t, [msg("430", "43", mentions=[user(ME)], hours_ago=1)])
    client.active_threads = [t]
    result = await make(client, allow_all()).get_my_pings(IDENT)
    assert result.payload["skipped_channels"] == 0
    assert all("43" not in line["channel_ids"] for line in audit_lines(caplog))


async def test_allow_all_person_deny_on_hidden_channel_is_not_revealed(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    hidden = channel("30", "hidden", permission_overwrites=[{"id": GUILD_ID, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)}])
    client.add_channel(hidden, [])
    cfg = Config(allowlist={}, deny=frozenset({(ME, "30")}), limits=Limits(), allow_all_channels=True)
    result = await make(client, cfg).get_my_pings(IDENT)
    assert result.payload["skipped_channels"] == 0
    assert all("30" not in line["channel_ids"] for line in audit_lines(caplog))


async def test_deny_list_hit_has_its_own_audit_code(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    await make(client, Config(allowlist={"10": ChannelEntry("10", None, 1)}, deny=frozenset(), limits=Limits(), deny_channels=frozenset({"10"}))).get_my_pings(IDENT)
    assert [line["status"] for line in audit_lines(caplog) if "10" in line["channel_ids"]][0] == "dropped:deny_list"


async def test_allowlist_mode_with_denied_category(client):
    cfg = Config(allowlist={"10": ChannelEntry("10", None, 1), "20": ChannelEntry("20", None, 1)}, deny=frozenset(), limits=Limits(), deny_channels=frozenset({"1"}))
    result = await make(client, cfg).get_my_pings(IDENT)
    assert ping_ids(result.payload) == ["200"]
    assert "messages:10" not in client.calls


async def test_opening_a_link_looks_up_only_its_own_channel(client):
    for i in range(5):
        client.add_channel(channel(str(60 + i), f"c{i}"), [])
    await make(client, allow_all()).get_conversation(IDENT, f"{LINK}/10/100")
    assert [c for c in client.calls if c.startswith("archived:")] == ["archived:10"]


async def test_starter_404_falls_back_to_oldest(client):
    t = channel("8", "solo", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("801", "8", OTHER, "first", hours_ago=900), msg("802", "8", THIRD, "now", hours_ago=2)])
    client.active_threads = [t]
    p = (await make(client, config("10")).get_channel_overview(IDENT, "10", days=30)).payload
    assert next(r for r in p["topics"] if r["id"] == "t:8")["opening"]["text"] == "first"
    assert "message:10/8" in client.calls and "oldest:8" in client.calls


async def test_denied_channel_is_not_named_in_mentions(client):
    client.add_channel(channel("10", "general", parent_id="1"), [
        msg("150", "10", OTHER, f"<@{ME}> see <#20> and <#10>", mentions=[user(ME)], hours_ago=1),
    ])
    p = (await make(client, allow_all("20")).get_my_pings(IDENT)).payload
    text = p["conversations"][0]["pings"][0]["text"]
    assert "#secret" not in text
    assert "<#20>" in text and "#general" in text


async def test_direct_only_channel_turns_role_pings_into_announcements():
    c = FakeClient()
    c.add_channel(channel("30", "leave"), [msg("300", "30", content="<@&R> out today", mention_roles=[ROLE_DEV], hours_ago=2)])
    cfg = Config(
        allowlist={"30": ChannelEntry(id="30", name=None, phase=1, pings="direct_only")},
        deny=frozenset(),
    )
    payload = (await make(c, cfg).get_my_pings(IDENT)).payload
    assert payload["counts"]["announcement"] == 1
    assert payload["counts"]["needs_you"] == 0


async def test_allow_all_direct_only_covers_threads_and_spares_other_channels(client):
    client.add_channel(channel("30", "leave", parent_id="1"), [msg("300", "30", content="<@&R> out", mention_roles=[ROLE_DEV], hours_ago=2)])
    t = channel("31", "trip", 11, parent_id="30", thread_metadata={"archived": False})
    client.add_channel(t, [msg("310", "31", content="<@&R> back monday", mention_roles=[ROLE_DEV], hours_ago=1)])
    client.add_channel(channel("40", "dev", parent_id="1"), [msg("400", "40", content="<@&R> review", mention_roles=[ROLE_DEV], hours_ago=1)])
    client.active_threads = [t]
    cfg = Config(
        allowlist={"30": ChannelEntry("30", None, 1, pings="direct_only")},
        deny=frozenset(),
        allow_all_channels=True,
        deny_channels=frozenset({"10", "20"}),
    )
    convs = (await make(client, cfg).get_my_pings(IDENT)).payload["conversations"]
    status = {c["id"].split("/")[0]: c["status"] for c in convs}
    assert status == {"30": "announcement", "31": "announcement", "40": "needs_you"}


async def test_overview_counts_a_thread_it_could_not_fetch(client):
    overview_client(client)
    client.fail["40"] = 500
    p = (await make(client, config("10")).get_channel_overview(IDENT, "10", days=7)).payload
    assert p["skipped_channels"] == 1
    assert all(t["id"] != "t:40" for t in p["topics"])


async def test_allow_all_failed_membership_lookup_does_not_reveal_private_thread(client, caplog):
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    t = channel("43", "hush", 12, parent_id="10")
    client.add_channel(t, [msg("430", "43", mentions=[user(ME)], hours_ago=1)])
    client.active_threads = [t]
    client.fail["43"] = 500
    result = await make(client, allow_all("20")).get_my_pings(IDENT)
    assert result.payload["skipped_channels"] == 0
    assert all("43" not in line["channel_ids"] for line in audit_lines(caplog))


async def test_overview_counts_a_thread_that_failed_the_access_step(client):
    overview_client(client)
    t = channel("43", "hush", 12, parent_id="10")
    client.add_channel(t, [msg("430", "43", hours_ago=1)])
    client.active_threads = [*client.active_threads, t]
    client.fail["43"] = 500
    p = (await make(client, config("10")).get_channel_overview(IDENT, "10", days=7)).payload
    assert p["skipped_channels"] == 1


async def test_overview_counts_a_main_channel_it_could_not_fetch(client):
    overview_client(client)
    original = client.get_messages_since

    async def flaky(channel_id, cutoff, max_count=None, before=None):
        if channel_id == "10":
            raise DiscordError(500)
        return await original(channel_id, cutoff, max_count, before)

    client.get_messages_since = flaky
    p = (await make(client, config("10")).get_channel_overview(IDENT, "10", days=7)).payload
    assert p["skipped_channels"] == 1
    assert [t["id"] for t in p["topics"]] == ["t:40"]


async def test_overview_direct_only_channel_ignores_group_pings_in_your_part(client):
    client.add_channel(channel("30", "leave", parent_id="1"), [
        msg("300", "30", content="<@&R> out", mention_roles=[ROLE_DEV], hours_ago=2),
    ])
    cfg = Config(allowlist={"30": ChannelEntry("30", None, 1, pings="direct_only")}, deny=frozenset())
    p = (await make(client, cfg).get_channel_overview(IDENT, "30", days=7)).payload
    assert p["topics"][0]["your_part"] == "none"


async def test_overview_here_ping_in_normal_channel_counts_as_pinged(client):
    client.add_channel(channel("30", "dev", parent_id="1"), [
        msg("300", "30", content="@here review please", mention_everyone=True, hours_ago=2),
    ])
    p = (await make(client, config("30")).get_channel_overview(IDENT, "30", days=7)).payload
    assert p["topics"][0]["your_part"] == "pinged"


async def test_search_reports_skipped_and_ignores_old_matches(client):
    old = msg("5", "10", OTHER, "acme kickoff", hours_ago=900, thread={"id": "5"})
    client.add_channel(channel("10", "general", parent_id="1"), [*client.messages["10"], old])
    t = channel("5", "old", 11, parent_id="10", thread_metadata={"archived": False})
    client.add_channel(t, [msg("501", "5", THIRD, "acme update", hours_ago=4)])
    client.active_threads = [t]
    p = (await make(client, config("10")).search(IDENT, "10", ["acme"], days=30)).payload
    assert p["match_count"] == 1
    assert p["skipped_channels"] == 0


# --- server-wide reads share one message budget ----------------------------------


async def test_pings_accept_30_days(client):
    result = await make(client, config("10")).get_my_pings(IDENT, days=30)
    assert result.payload["days"] == 30


async def test_pings_share_a_request_budget(client):
    await make(client, config("10", "20", server_request_messages=400)).get_my_pings(IDENT)
    assert client.max_counts["10"] == 200 and client.max_counts["20"] == 200


async def test_pings_budget_is_a_hard_total_shared_by_channels(client):
    # The per-channel share gives way to the request total (security review: no 100-message floor).
    await make(client, config("10", "20", server_request_messages=50)).get_my_pings(IDENT)
    assert client.max_counts["10"] == 25


async def test_pings_truncated_when_a_channel_hits_its_share(client):
    payload = (await make(client, config("10", overview_max_messages=2)).get_my_pings(IDENT, days=3)).payload
    assert payload["truncated"] is True


# --- server-wide reads: a hard request budget, handed back to busy channels ---------------


def _busy_server(main_msgs: int, threads: int) -> FakeClient:
    c = FakeClient()
    c.add_channel(channel("1", "Team", 4), [])
    c.add_channel(channel("10", "busy", parent_id="1"), [msg(str(10_000 + i), "10", hours_ago=1 + i * 0.01) for i in range(main_msgs)])
    for t in range(threads):
        tid = str(500 + t)
        recent = ((int(NOW.timestamp() * 1000) - 1420070400000 - 3_600_000) << 22) + t  # an hour ago, newer for higher t
        th = channel(tid, f"t{t}", 11, parent_id="10", thread_metadata={"archived": False}, last_message_id=str(recent))
        c.add_channel(th, [msg(str(30_000 + t), tid, hours_ago=2)])
        c.active_threads = [*c.active_threads, th]
    return c


async def test_unused_share_goes_back_to_a_busy_channel():
    c = _busy_server(1000, 100)
    d = make(c, Config(allowlist={"10": ChannelEntry("10", None, 1)}, deny=frozenset(), limits=Limits(server_request_messages=3000)))
    read = await d._reader.read_server(IDENT, "get_my_pings", NOW - timedelta(days=30))
    main = next(cm for cm in read.fetched if cm.channel["id"] == "10")
    assert len(main.messages) == 1000
    assert read.capped is False


async def test_handed_back_budget_continues_where_the_first_read_stopped():
    # Issue #7: a busy channel's second read starts below its first, so no message is read twice.
    c = FakeClient()
    c.add_channel(channel("1", "Team", 4), [])
    for cid, n in (("10", 30), ("20", 30), ("30", 1)):
        c.add_channel(channel(cid, f"c{cid}", parent_id="1"), [msg(str(int(cid) * 1000 + i), cid, hours_ago=2 - i * 0.01) for i in range(n)])
    cfg = Config(allowlist={k: ChannelEntry(k, None, 1) for k in ("10", "20", "30")}, deny=frozenset(),
                 limits=Limits(server_request_messages=30))
    read = await make(c, cfg)._reader.read_server(IDENT, "get_my_pings", NOW - timedelta(days=30))
    for cid in ("10", "20"):
        assert len(c.returned[cid]) == len(set(c.returned[cid])), "a message was read twice"
        got = [m["id"] for m in next(cm for cm in read.fetched if cm.channel["id"] == cid).messages]
        assert got == sorted(got, key=int)  # oldest first
        assert len(got) == 14  # share of 10, plus half of the 9 the quiet channel left
        assert got[-1] == str(int(cid) * 1000 + 29)  # the newest message is kept
    assert sum(len(cm.messages) for cm in read.fetched) <= 30


async def test_failed_continuation_keeps_the_first_read_and_counts_as_busy(caplog):
    # A busy channel whose second read hits Discord's rate limit still shows its first read,
    # so it is logged as partial (not dropped) and counted in rate_limited.
    caplog.set_level(logging.INFO, logger="discord_mcp.audit")
    c = FakeClient()
    c.add_channel(channel("1", "Team", 4), [])
    for cid, n in (("10", 30), ("30", 1)):
        c.add_channel(channel(cid, f"c{cid}", parent_id="1"), [msg(str(int(cid) * 1000 + i), cid, hours_ago=2 - i * 0.01) for i in range(n)])
    real = c.get_messages_since

    async def busy_later(channel_id, cutoff, max_count=None, before=None):
        if before is not None:
            raise DiscordError(429)
        return await real(channel_id, cutoff, max_count, before)

    c.get_messages_since = busy_later
    cfg = Config(allowlist={k: ChannelEntry(k, None, 1) for k in ("10", "30")}, deny=frozenset(),
                 limits=Limits(server_request_messages=20))
    read = await make(c, cfg)._reader.read_server(IDENT, "get_my_pings", NOW - timedelta(days=30))
    assert len(next(cm for cm in read.fetched if cm.channel["id"] == "10").messages) == 10
    assert read.rate_limited == 1
    assert read.capped is True
    statuses = [line["status"] for line in audit_lines(caplog) if line["channel_ids"] == ["10"]]
    assert statuses == ["dropped:partial_http_429"]


async def test_request_budget_is_a_hard_total():
    c = _busy_server(50, 300)
    for t in range(300):
        tid = str(500 + t)
        c.messages[tid] = [msg(str(40_000 + t * 100 + i), tid, hours_ago=2) for i in range(50)]
    d = make(c, Config(allowlist={"10": ChannelEntry("10", None, 1)}, deny=frozenset(),
                       limits=Limits(server_request_messages=3000, server_max_threads=1000)))
    read = await d._reader.read_server(IDENT, "get_my_pings", NOW - timedelta(days=30))
    assert sum(len(cm.messages) for cm in read.fetched) <= 3000
    assert read.capped is True


async def test_thread_cap_keeps_the_newest_threads():
    c = _busy_server(5, 10)
    d = make(c, Config(allowlist={"10": ChannelEntry("10", None, 1)}, deny=frozenset(), limits=Limits(server_max_threads=3)))
    read = await d._reader.read_server(IDENT, "get_my_pings", NOW - timedelta(days=30))
    got = sorted(cm.channel["id"] for cm in read.fetched if cm.channel["id"] != "10")
    assert got == ["507", "508", "509"]
    assert read.capped is True


async def test_pings_use_their_own_size_limit(client):
    small = await make(client, config("10", pings_max_chars=400)).get_my_pings(IDENT)
    big = await make(client, config("10", max_result_chars=400, pings_max_chars=100000)).get_my_pings(IDENT)
    assert small.payload["truncated"] is True
    assert big.payload["truncated"] is False


# --- rate limits and the bot's own access ---------------------------------------------


async def test_rate_limited_channels_are_counted_apart_from_other_skips(client):
    client.fail["20"] = 429
    payload = (await make(client, config("10", "20")).get_my_pings(IDENT)).payload
    assert payload["skipped_channels"] == 1
    assert payload["rate_limited_channels"] == 1


async def test_no_rate_limited_key_when_nothing_was_rate_limited(client):
    client.fail["20"] = 403
    payload = (await make(client, config("10", "20")).get_my_pings(IDENT)).payload
    assert payload["skipped_channels"] == 1
    assert "rate_limited_channels" not in payload


@pytest.mark.parametrize("tool", ["get_server_overview", "get_open_loops"])
async def test_brief_tools_report_rate_limited_channels(client, tool):
    client.fail["20"] = 429
    payload = (await getattr(make(client, config("10", "20")), tool)(IDENT)).payload
    assert payload["rate_limited_channels"] == 1


async def test_rate_limited_thread_lookup_is_reported(client):
    client.fail["10"] = 429  # the archived-thread lookup for the parent fails
    payload = (await make(client, config("10", "20")).get_server_overview(IDENT)).payload
    assert payload["rate_limited_channels"] == 1


async def test_channel_overview_on_a_rate_limited_channel_says_try_again(client):
    from discord_mcp.discord_client import DiscordError

    client.fail["10"] = 429
    with pytest.raises(DiscordError):
        await make(client, config("10")).get_channel_overview(IDENT, "10")


async def test_channel_overview_reports_rate_limited_threads(client):
    thread = channel("15", "plan", 11, parent_id="10", thread_metadata={"archive_timestamp": NOW.isoformat()})
    client.add_channel(thread, [msg("150", "15", hours_ago=3)])
    client.active_threads = [thread]
    client.fail["15"] = 429
    payload = (await make(client, config("10")).get_channel_overview(IDENT, "10")).payload
    assert payload["rate_limited_channels"] == 1


def bot_member(client: FakeClient, *roles: str) -> None:
    from tests.fakes import BOT_USER

    client.members[BOT_USER] = {"user": user(BOT_USER, "bot", bot=True), "roles": list(roles)}


def bot_blocked_channel() -> dict:
    """Readable by the dev role (the person), not by @everyone (the bot has no roles)."""
    from discord_mcp.permissions import READ_MESSAGE_HISTORY, VIEW_CHANNEL

    read = str(VIEW_CHANNEL | READ_MESSAGE_HISTORY)
    return channel(
        "30",
        "dev-only",
        permission_overwrites=[
            {"id": GUILD_ID, "type": 0, "allow": "0", "deny": read},
            {"id": ROLE_DEV, "type": 0, "allow": read, "deny": "0"},
        ],
    )


async def test_channels_the_bot_cannot_read_are_skipped_without_a_request(client):
    client.add_channel(bot_blocked_channel(), [msg("300", "30", mentions=[user(ME)], hours_ago=2)])
    bot_member(client)
    payload = (await make(client, config("10", "30")).get_my_pings(IDENT)).payload
    assert "messages:30" not in client.calls
    assert "archived:30" not in client.calls
    assert payload["skipped_channels"] == 1
    assert "rate_limited_channels" not in payload


async def test_bot_without_access_is_skipped_quietly_counted_in_allow_all(client):
    client.add_channel(bot_blocked_channel(), [msg("300", "30", mentions=[user(ME)], hours_ago=2)])
    bot_member(client)
    cfg = Config(allowlist={}, deny=frozenset(), limits=Limits(), allow_all_channels=True)
    payload = (await make(client, cfg).get_my_pings(IDENT)).payload
    assert "messages:30" not in client.calls
    assert payload["skipped_channels"] == 1  # the person can see it, so counting it reveals nothing


async def test_bot_roles_unknown_falls_back_to_trying_the_channel(client):
    client.add_channel(bot_blocked_channel(), [msg("300", "30", mentions=[user(ME)], hours_ago=2)])
    # no bot member entry: the lookup fails, so channels are read as before
    payload = (await make(client, config("10", "30")).get_my_pings(IDENT)).payload
    assert "messages:30" in client.calls
    assert "300" in ping_ids(payload)


async def test_bot_user_is_looked_up_once_per_digest(client):
    bot_member(client, ROLE_DEV)
    d = make(client, config("10"))
    await d.get_my_pings(IDENT)
    await d.get_my_pings(IDENT)
    assert client.calls.count("users_me") == 1


async def test_get_conversation_on_a_rate_limited_channel_says_try_again(client):
    from discord_mcp.discord_client import DiscordError

    client.fail["10"] = 429
    with pytest.raises(DiscordError):
        await make(client, config("10")).get_conversation(IDENT, f"https://discord.com/channels/{GUILD_ID}/10/100")


async def test_opening_a_main_stretch_when_rate_limited_says_try_again(client):
    from discord_mcp.discord_client import DiscordError

    client.fail["10"] = 429
    with pytest.raises(DiscordError):
        await make(client, config("10")).get_conversation(IDENT, "m:10/100")


async def test_opening_a_rate_limited_thread_says_try_again(client):
    from discord_mcp.discord_client import DiscordError

    thread = channel("15", "plan", 11, parent_id="10", thread_metadata={"archive_timestamp": NOW.isoformat()})
    client.add_channel(thread, [msg("150", "15", hours_ago=3)])
    client.active_threads = [thread]
    d = make(client, config("10"))
    real = client.get_messages_since

    async def busy(channel_id, cutoff, max_count=None, before=None):
        if channel_id == "15":
            raise DiscordError(429)
        return await real(channel_id, cutoff, max_count, before)

    client.get_messages_since = busy
    with pytest.raises(DiscordError):
        await d.get_conversation(IDENT, "t:15")


async def test_rate_limited_private_thread_lookup_is_counted(client):
    recent = str((int(NOW.timestamp() * 1000) - 1420070400000 - 3_600_000) << 22)  # a message an hour ago
    thread = channel("16", "private", 12, parent_id="10", last_message_id=recent)
    client.add_channel(thread, [])
    client.active_threads = [thread]
    client.fail["16"] = 429
    payload = (await make(client, config("10")).get_my_pings(IDENT)).payload
    assert payload["rate_limited_channels"] == 1


# --- issue #4: a link never makes the bot ask Discord about a channel it must not read -----------

from discord_mcp.errors import AccessDenied  # noqa: E402


def no_channel_lookups(client: FakeClient) -> bool:
    return not any(c.startswith("channel:") for c in client.calls)


@pytest.mark.parametrize("link", [f"{LINK}/20/200", "20/200"])
async def test_link_to_a_channel_off_the_allowlist_is_refused_without_a_lookup(client, link):
    with pytest.raises(AccessDenied):
        await make(client, config("10")).get_conversation(IDENT, link)
    assert no_channel_lookups(client)
    assert not any(c.startswith(("messages:", "archived:")) for c in client.calls)


async def test_link_to_a_denied_channel_is_refused_without_a_lookup(client):
    with pytest.raises(AccessDenied):
        await make(client, allow_all("20")).get_conversation(IDENT, "20/200")
    assert no_channel_lookups(client)


async def test_link_to_a_channel_the_bot_cannot_read_is_refused_without_a_lookup(client):
    client.add_channel(bot_blocked_channel(), [msg("300", "30", mentions=[user(ME)], hours_ago=2)])
    bot_member(client)
    with pytest.raises(AccessDenied):
        await make(client, allow_all()).get_conversation(IDENT, f"{LINK}/30/300")
    assert no_channel_lookups(client)
    assert "messages:30" not in client.calls


async def test_link_into_an_active_thread_opens_without_a_lookup(client):
    overview_client(client)
    p = (await make(client, config("10")).get_conversation(IDENT, f"{LINK}/40/401")).payload
    assert p["id"] == "t:40"
    assert no_channel_lookups(client)


async def test_thread_topic_id_opens_without_a_lookup(client):
    overview_client(client)
    p = (await make(client, config("10")).get_conversation(IDENT, "t:40")).payload
    assert p["id"] == "t:40"
    assert no_channel_lookups(client)


async def test_active_thread_under_a_channel_off_the_allowlist_is_refused_without_a_lookup(client):
    t = channel("45", "hidden plan", 11, parent_id="20", thread_metadata={"archived": False})
    client.add_channel(t, [msg("451", "45", OTHER, "x", hours_ago=1)])
    client.active_threads = [t]
    with pytest.raises(AccessDenied):
        await make(client, config("10")).get_conversation(IDENT, "45/451")
    assert no_channel_lookups(client)
    assert "messages:45" not in client.calls


async def test_archived_thread_link_still_opens_by_looking_it_up_once(client):
    sent = int((NOW.timestamp() - 480 * 3600) * 1000) - 1420070400000
    t = channel("44", "quiet", 11, parent_id="10", thread_metadata={"archived": True}, last_message_id=str(sent << 22))
    client.add_channel(t, [msg("441", "44", OTHER, "waiting on the draft", hours_ago=480)])
    client.archived_threads["10"] = [t]
    p = (await make(client, config("10")).get_conversation(IDENT, "44/441")).payload
    assert p["id"] == "t:44"
    assert [c for c in client.calls if c.startswith("channel:")] == ["channel:44"]


# --- allowed_roles: only members holding one of these roles may use the server --------------------

from discord_mcp.errors import NotAMember, RoleNotAllowed  # noqa: E402
from tests.fakes import ROLE_OPS  # noqa: E402


async def test_member_without_an_allowed_role_is_refused(client):
    cfg = Config(allowlist={}, deny=frozenset(), allowed_roles=frozenset({ROLE_OPS}))
    with pytest.raises(RoleNotAllowed) as e:
        await make(client, cfg).resolve_identity(ME)
    assert isinstance(e.value, NotAMember)  # sign-in and every call refuse it like a non-member


async def test_member_with_an_allowed_role_is_let_in(client):
    cfg = Config(allowlist={}, deny=frozenset(), allowed_roles=frozenset({ROLE_OPS, ROLE_DEV}))
    ident = await make(client, cfg).resolve_identity(ME)
    assert ident.user_id == ME


async def test_no_allowed_roles_lets_every_member_in(client):
    client.members[ME]["roles"] = []
    assert (await make(client, config()).resolve_identity(ME)).user_id == ME


async def test_link_to_a_channel_the_person_cannot_see_is_refused_without_a_lookup(client):
    from discord_mcp.permissions import READ_MESSAGE_HISTORY, VIEW_CHANNEL

    read = str(VIEW_CHANNEL | READ_MESSAGE_HISTORY)
    hidden = channel("31", "ops-only", permission_overwrites=[{"id": GUILD_ID, "type": 0, "allow": "0", "deny": read}])
    client.add_channel(hidden, [msg("310", "31", hours_ago=1)])
    with pytest.raises(AccessDenied):
        await make(client, allow_all()).get_conversation(IDENT, "31/310")
    assert no_channel_lookups(client)
    assert "messages:31" not in client.calls


async def test_active_thread_link_reads_the_active_thread_list_once(client):
    overview_client(client)
    calls = []
    real = client.get_active_threads

    async def counted(guild_id):
        calls.append(guild_id)
        return await real(guild_id)

    client.get_active_threads = counted
    await make(client, config("10")).get_conversation(IDENT, "t:40")
    assert len(calls) == 1
