# Ping conversations: a smaller, purpose-built "what needs me" result

Date: 2026-11-15 · Status: draft for review

## Why

`get_my_pings` returns each ping with up to 10 raw follow-up messages. In the
first live test, two weeks of pings came to about 62,000 characters. That
is over Claude Code's limit for a tool result, and it uses up the context
Claude needs to summarise across the server. A power user gets about 20 pings
a day (replies to their messages count as pings), so roughly 140 a week.
`max_items` is 100, so trimming alone never puts an upper limit on the size.

Where the size comes from today:

- About 80% is follow-up messages.
- For a ping in a plain channel, the follow-up is simply the next 10 messages
  in that channel, whatever they are about. Pings that are close together
  carry the same messages, so 122 follow-up messages held only 64 different ones.
- Every follow-up repeats `channel`, `id` and `is_bot: false`, plus a
  `reply_to` quote that is usually the ping or an earlier follow-up.

The ping data is there to answer three questions per ping:

1. What was I asked, and by whom?
2. Did I deal with it? (my last reply after it)
3. Did anything change after that? (was I asked again)

The server can answer most of 2 and 3 itself, more cheaply than Claude
reading raw messages. In the test data, most pings already had a reply
from the person, and they formed a handful of conversations.

## Use cases

1. **What still needs me, and what has been covered** (daily). Small and
   always the same shape. This design.
2. **A fuller picture of my conversations** (sometimes). Claude reads the
   summary rows and opens the few conversations it needs. This design.
3. **What happened across the server**, including where nobody pinged me
   (later). A separate channel-activity tool. Not in this design.

## Goals

- `get_my_pings` returns conversations, not raw pings with follow-ups.
- The server works out each conversation's status: needs you, reopened or handled.
- The result has a hard size budget, so the size does not keep growing with
  how many pings someone gets.
- A new tool opens one conversation in full.
- Test target: the two-week test case comes back well under 20,000
  characters, and a 140-ping week stays under the budget.

## Non-goals

- The server-wide or channel-activity view (use case 3), and the size of
  `get_channel_messages`. Both come later.
- Summarising with an AI model on the server. Claude still does all summarising.
- Storing anything between requests. The no-database rule stays.

## Anti-goals (failure even if it works)

- **Marking something handled when it is not.** The server treats any
  message from you after the ping as a reply, so a "handled" row always
  includes the text of your last reply. Claude judges whether that reply
  closed the matter, for example "will look tomorrow" does not.
- **Dropping a "needs you" conversation to save space** while "handled" rows
  are kept.
- **Weakening any guarantee in `docs/design.md`**: read-only, no stored text,
  minimal logs, fail closed, both gates.

## Approaches considered

How the server decides which messages belong to a ping's conversation. In a
thread it is the whole thread. In a plain channel:

- **A. Follow reply links only.** The ping, replies to it, replies to those.
  It is exact, but it misses answers typed into the channel without using
  "reply", which is how many people answer.
- **B. Follow the people (chosen).** The reply chain from A, plus messages
  after the ping from the people involved, until a quiet gap. It catches the
  typed-in answers that "did I respond?" depends on, and its size depends on
  your conversations rather than on how busy the channel is. It may pull in a
  different topic between the same people. That does little harm, because
  that topic would also be one of your conversations.
- **C. Remove repeats only.** Each message is sent once and pings point to
  it. It is simplest, but Claude still groups the messages, and the size
  still depends on how busy the channel is.

The user chose B.

## Design

### Conversations

- **In a thread:** the thread is the conversation. Every ping in the thread
  belongs to it.
- **In a plain channel:** start from a ping and add messages after it that
  either
  - reply (through Discord's `referenced_message`) to a message already in
    the conversation, or
  - come from a participant: the pinger, you, or anyone the ping names
    directly (users, not roles).

  The conversation ends at the first quiet gap of `conversation_gap_hours`
  (default 12) between its messages, or `conversation_max_hours` (default 24)
  after its first message, whichever comes first.
- **Pings fall into one conversation.** A ping that already belongs to a
  conversation (for example, a reply-ping inside one) joins it instead of
  starting a new one. Every message belongs to at most one conversation.
- **When a message fits more than one conversation** (common: you count as a
  participant in every open conversation in a channel), it is assigned in
  this order:
  1. If it uses Discord "reply", it goes to the conversation that holds the
     message it replies to.
  2. Otherwise, it goes to the conversation with the most recent ping
     before it.

  The other conversations do not count it. A wrong guess here leaves a
  conversation marked `needs_you` when it was really handled. That is the
  safe way to be wrong, and the anti-goal is the opposite.
- **`@everyone` conversations do not take your messages by participation.**
  For an `everyone` ping, the participants are the pinger and anyone it
  names directly, not you. Your message joins it only by replying to a
  message in it. `here` and `role` pings count as direct pings, and you are
  a participant.
- **A ping whose thread was started from it** keeps today's behaviour: the
  conversation is that thread.

### Status

Per conversation, using its latest ping and your messages in it:

| Status | Rule |
| --- | --- |
| `needs_you` | You have no message in the conversation after its first ping |
| `reopened` | You replied, but there is a ping after your last message |
| `handled` | Your last message comes after the latest ping |

**`@everyone` pings** are rarely used and are not asked of you personally.
They form their own conversations with status `announcement`, unless they
fall inside a conversation that already has a direct ping.

**`@here` and role pings** count as direct pings: they get needs_you,
reopened or handled. `@here` is often used in small private channels to
mean "you lot, please". When someone else answered, the row's `latest`
field shows it, so Claude can report "covered by someone else" rather than
"needs you".

### Output of `get_my_pings`

Same inputs as today (`days`, `channel_ids`). Shape:

```json
{
  "conversations": [
    {
      "status": "needs_you",
      "where": "grants › grant-application",
      "people": ["alice", "bob"],
      "started": "2026-11-12T09:10",
      "last_activity": "2026-11-13T14:02",
      "message_count": 6,
      "pings": [
        {"reason": "mention", "author": "alice", "time": "2026-11-12T09:10",
         "text": "…", "link": "…"}
      ],
      "your_last_reply": null,
      "latest": {"author": "bob", "time": "…", "text": "…", "link": "…"},
      "id": "<conversation id for get_conversation>"
    }
  ],
  "counts": {"needs_you": 2, "reopened": 0, "handled": 4, "announcement": 0},
  "omitted": {"handled": 0, "announcement": 0},
  "truncated": false,
  "days": 7,
  "skipped_channels": 0
}
```

How much each row carries depends on its status:

| Field | needs_you / reopened | handled | announcement |
| --- | --- | --- | --- |
| `pings` | the first ping plus the newest pings, up to `row_max_pings` (default 5) in total, text up to `text_chars` | latest ping only, text up to `summary_chars` | one ping, text up to `summary_chars` |
| `your_last_reply` | for reopened, up to `summary_chars` | up to `summary_chars` | — |
| `latest` (latest message that is not yours, if newer than the last ping) | up to `summary_chars` | — | — |
| `ping_count` | number of pings in the conversation | number of pings in the conversation | — |

`summary_chars` defaults to 200. Times are trimmed to minutes. `is_bot`
appears only when true. Links stay complete, because Claude cites them.

**Order:** needs_you, reopened, handled, announcement; within each, the
newest activity first.

**Size budget:** `max_result_chars` (default 40,000) of serialised JSON.
When the result would go over it, rows are dropped from the end of the
order: oldest announcements first, then oldest handled. needs_you and
reopened rows are never dropped while lower rows remain. If those alone go
over the budget, the oldest of them are dropped too. `omitted` has a count
for every status, and `truncated` is true whenever any row was dropped.
Because rows are capped at `row_max_pings`, a single conversation cannot
fill the budget on its own.

`max_items` is removed. Every ping in the window is matched and grouped
before anything is cut, so no cut can remove a conversation's first ping and
change its status. The size budget is the only limit on the result.
`truncated` now means only "rows were dropped for the budget".

### New tool: `get_conversation`

- **Input:** `link`, the Discord link of any message in the conversation
  (from `pings[].link`, `latest.link` or `your_last_reply.link`). It also
  accepts a conversation `id`.
- **Access:** the channel or thread must pass both gates (allowlist and the
  person's permissions), the same check the other tools use. Otherwise it
  returns "That channel is not available to you." The guild ID in the link
  must match.
- **Content:** it rebuilds the conversation around that message with the same
  rules as above, and returns every message in it in time order, text up to
  `text_chars`, up to `conversation_max_messages` (default 50). Keep the
  first ping and the newest messages; set `truncated` when messages in the
  middle are cut.
- **Audit:** logged like the other tools: user ID, tool, channel IDs,
  count, duration, status.

### Server instructions and tool descriptions

Update `INSTRUCTIONS` and the tool descriptions to match the new flow. Start
with `get_my_pings`, report needs_you and reopened first, and use the text
of `your_last_reply` to decide whether a handled conversation is really
done. Call `get_conversation` for the few conversations where detail is
needed. Keep the warning that message text is untrusted.

### Config

New `[limits]` keys with defaults: `conversation_gap_hours = 12`,
`conversation_max_hours = 24`, `summary_chars = 200`,
`max_result_chars = 40000`, `conversation_max_messages = 50`,
`row_max_pings = 5`. `follow_up_count`, `follow_up_chars` and `max_items`
are removed, and `load_config` rejects them with a clear message, the same
way it rejects other unknown keys.

### Docs

Update `docs/design.md` ("What counts as a ping" and the Tools table) and the
README tool list.

## Decided by the user

- Two separate use cases: "what needs me / what has been covered", and a
  fuller picture. The fuller picture means your own conversations first and
  the server-wide view later.
- Grouping approach B: follow the people.
- `@here` counts as a direct ping, because it's used in small private
  channels. `@everyone` is rare and goes in the `announcement` group.

## My calls (correct if wrong)

- Keep the name `get_my_pings` but change its output, rather than adding a
  new tool next to it. That's one tool for the daily question, and the
  server instructions already point Claude to it.
- The status names and rules above, including that any message from you
  counts as a reply, with Claude judging the reply text.
- Role pings are treated like `@here` (as direct pings). The user decided
  this for `@here` and `@everyone`. Role pings are my call.
- The default numbers: 12-hour gap, 24-hour cap, 200 summary characters,
  40,000-character budget, 50 messages per opened conversation.
- `get_conversation` takes a link, so Claude can pass a link it already
  has. Conversation `id` is also accepted.
- When a message fits more than one conversation: a Discord reply goes to
  the conversation of the message it replies to, otherwise to the one with
  the most recent ping before it. `@everyone` conversations do not take your
  messages by participation. Wrong guesses fall on the safe side (a false `needs_you`).
- `max_items` is removed. All pings are grouped first, and the size budget
  is the only limit.
- A busy thread's row keeps the first ping plus the newest pings (5 in
  total) and a `ping_count`, so one conversation cannot fill the budget.
  `omitted` counts every status.

## Left to the builder

- How conversation `id` is encoded (for example, the channel ID and first
  ping ID). It must not let anyone get past the access check: access is
  checked again on every call.
- How `get_conversation` fetches messages around an older message within
  `max_days` (for example, Discord's `after=` parameter on the message list).
- Edge cases in grouping, for example a reply into a conversation that has
  already ended.
- Test structure. Add a made-up fixture of a busy fortnight: most
  pings in one channel as one back-and-forth, and a couple with no reply
  from the person.
