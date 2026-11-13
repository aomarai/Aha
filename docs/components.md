# Message components: buttons and select menus

Aha runs as several processes (ADR 0001) and restarts on every deploy. A component that lives only
in the memory of the process that sent it breaks the moment the next click lands on another
process or after a rollout: Discord shows "This interaction failed". These rules prevent that.

## Rules

1. **State lives in the `custom_id`, not in a `View`.** Build the component as a
   `discord.ui.DynamicItem` whose `custom_id` matches a regular-expression template, and register
   the class once in the cog's `setup` with `bot.add_dynamic_items(MyItem)`. A click then arrives at
   whichever process is running, which rebuilds the item from the `custom_id` (`from_custom_id`) and
   calls `callback`. No process remembers anything between clicks.
   - A `custom_id` holds at most 100 characters: an action, ids and small numbers fit. Anything
     bigger goes in the database (#84) keyed by an id in the `custom_id`.
   - Recompute what you display on each click from live data (the way `/help` rebuilds its pages
     from the current commands) rather than trusting a stale copy, and clamp values that may no
     longer exist.
2. **Check who clicked on every click.** Being able to see a message does not mean being allowed to
   press its buttons. Call `aha.components.require_invoker(interaction, owner_id)` from
   `interaction_check`; it also binds the log context, which button clicks otherwise lack because
   they never pass the command tree's check. Encode the owner's id in the `custom_id`.
3. **Destructive actions need a confirm step**: a second, invoker-only button that expires, never
   "assume you meant it".
4. **Prefer ephemeral messages** for menus that belong to one user, so strangers cannot click them
   in the first place (rule 2 still applies).
5. **Short-lived, in-memory views are allowed only by exception.** A `discord.ui.View` with its own
   callbacks and a timeout (120 s or less) is acceptable for a throwaway interaction the user will
   finish right away, because it is lost on restart. Say so in a comment. Never use that pattern for
   state that must survive.

## Example: `/help`

`aha.cogs.help.HelpButton` is the reference implementation. Its `custom_id` is
`aha:help:<prev|next>:<page to show>:<owner id>`; clicking rebuilds the pages from the bot's
current commands, edits the message, and returns a new set of buttons carrying the new page
numbers. `tests/test_help.py` builds a button from a `custom_id` alone and clicks it on a fresh
handler to prove it survives a restart.

## Native polls and context menus

Prefer Discord's own features over a custom component when one fits: `/poll` uses a native poll
(Discord stores the votes and closes it), and "Count reactions" is a message context menu, which
receives the message without the privileged Message Content intent. Neither keeps any state.

## Naming

Prefix every `custom_id` with `aha:<feature>:` so templates never collide.
