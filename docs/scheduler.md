# The job scheduler

Timed work (reminders, poll results, scheduled follow-ups) must fire at a time, survive restarts,
and fire **once** however many pods run. Doing it in memory fails all three, so due jobs live in
PostgreSQL (`scheduled_jobs`) and **any pod** may run any job (ADR 0001, section 5). It exists only
when `DATABASE_URL` is set.

## Using it from a cog

```python
# Enqueue (needs a user or a guild: that is how a person's data is found and deleted).
await JobStore(bot.db).enqueue(
    "reminder", run_at, user_id=ctx.author.id, payload={"channel": ctx.channel.id, "text": text}
)


# Handle: register once, in the cog's setup. The handler gets a Job.
async def deliver(job: Job) -> None: ...


bot.scheduler.register("reminder", deliver)
```

`bot.scheduler` is the poller; the enqueueing side is `aha.jobstore.JobStore` over `bot.db`
(a cog that enqueues keeps its own `JobStore(bot.db)`).

## Guarantees and what a handler must do

- **At-least-once.** A pod that dies after Discord accepted a message but before the row was deleted
  leaves a lease that expires, and another pod runs the job again. A handler must therefore be
  **idempotent where Discord allows**: send with a deterministic `nonce` derived from `job.id` and
  `enforce_nonce=True`, and accept a rare duplicate where it cannot be suppressed.
- **Time-boxed.** A handler that overruns `handler_timeout` (30 s) is cancelled and counted as a
  failure. The lease (60 s) is required to be longer, or a job could run twice at once.
- **Retries with back-off, then dead.** A failure retries after `30 s x 2^(attempts-1)` (at most an
  hour). `attempts` is counted when a job is **claimed**, so a job that crashes the whole process
  still reaches `dead` after 5 attempts instead of looping. Dead jobs are kept 7 days (`last_error`
  says why), then deleted.
- **No handler yet?** A job whose `kind` this revision does not know is retried, not dropped: a
  rolling deploy may have a newer pod that does.
- **Cancellation is not a failure.** On shutdown the lease simply expires.

## Operating it

`aha_scheduler_lag_seconds` (seconds the oldest due, unclaimed job has waited) feeds the
`AhaSchedulerLag` alert (over 120 s for 5 minutes). The scheduler runs as a critical, restartable
task, so a crashed poller makes `/readyz` report degraded until it has run stably again.

Privacy: `JobStore.purge_user` and `purge_guild` delete a person's or a server's jobs; the
`scheduled_jobs` entry in `aha.datainventory` documents retention.
