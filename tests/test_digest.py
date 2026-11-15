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
        await make(client, config("10")).get_my_pings(IDENT, days=15)


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
    client.add_channel(t, [msg("400", "40", mention_roles=[ROLE_DEV], hours_ago=3)])
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

from discord_mcp.digest import AccessDenied, InputError  # noqa: E402

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
