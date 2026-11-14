# Privacy policy (DRAFT)

> **Draft for the repository owner's review.** It describes what the code does today and what is
> planned; it is not legal advice. It must be reviewed, adjusted for how and where the bot is
> actually run, and published at a stable URL before the bot reaches 75 servers (Discord requires a
> privacy policy and terms to verify an app). The rule about database tables, below, is enforced by a test.

Aha is an open-source Discord bot. This policy explains what it handles when you use it. If you run
your own copy, **you** are the operator of that copy, and this describes what the software does.

## What Aha receives and keeps today

| What | Why | Kept |
|------|-----|------|
| Your Discord **user ID**, the **server and channel IDs**, the **command name** and **the names (never the values) of its options**, and when you ran a command | operating and debugging the bot; an audit trail of who ran what | In the operator's **logs** only, for as long as the operator's log store keeps them (the project recommends 30 days). Not in a database. |
| **Messages that mention the bot** | so that `@Aha command` works | Not stored. Aha does not request the Message Content privileged intent, and ignores every message that does not mention it. |
| **The link** you give `/url` | to show a preview | The page title and description are cached **in memory for 5 minutes** to avoid re-fetching, then discarded. |
| Server settings (`/configure`) | to apply a server's choices (log channel, disabled commands) | The server id, the settings and the **id of the admin who last changed them**, until an admin resets them or Aha has been out of the server for **30 days**, then deleted. |
| Reminders (`/remind`) | to deliver your reminder later | Your user id, server id, channel id and the reminder text are stored **until it is delivered or you cancel it** (`/unremind`); failed deliveries are kept at most 7 days. |
| Polls and reactions | `/poll` creates a native Discord poll; "Count reactions" reads a message's reaction counts | **Discord** stores polls and votes, not Aha. |

Aha does **not** store the text of your commands, your messages, or any profile. It sends nothing to
advertisers and does not sell data.

## Planned data (not collected yet)

When the features on the roadmap are built, Aha will keep the following **in a PostgreSQL database**.
Nothing below is collected until that feature ships and this section is updated.

| Feature | Data | Kept until |
|---------|------|-----------|
| `/remind` | your user ID, the channel, the reminder text, the time | the reminder is delivered or you cancel it, then deleted |
| `/config` | a server's settings (language, disabled commands, a log channel) | the bot is removed from the server **plus 30 days** (so a short kick and re-add does not lose them), then deleted |
| `/summarize` (optional, off by default) | the text you submit is sent to the Claude API (Anthropic) to produce the summary; a per-user daily usage **count** is kept | the text is not stored by Aha; counts for 30 days |

Every table in the database must declare how to delete a user's or a server's rows
(`src/aha/datainventory.py`); a test fails if a migration adds a table without doing so.

## Who else sees data

- **Discord** processes everything you do in Discord, under its own policy.
- **The hosting provider** of the operator (for the official instance, a home server).
- **Anthropic**, only if `/summarize` is enabled for a server and you use it.

## Your choices

- **Delete your data**: run `/forgetme` and press the confirm button (it expires after 5 minutes).
  It deletes your pending reminders and any other scheduled jobs, and removes your id from server
  settings you changed (the settings stay with the server). Log lines are not in the database; ask
  the operator about those.
- **Stop**: remove the bot from your server, or stop using its commands. Its server data is deleted
  after 30 days.
- **Ask or complain**: open an issue at <https://github.com/aomarai/Aha/issues>, or contact the
  operator privately through the repository's security advisory form for anything sensitive.

## Children

Aha follows Discord's minimum age. It does not knowingly collect data from anyone below it.

## Changes

Changes are made in this file; its history is public in the repository.
