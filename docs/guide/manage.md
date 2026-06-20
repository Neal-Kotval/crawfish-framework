# Manage deployed pipelines

`craw manage` shows you every [deployed](deploy.md) pipeline in one view. It reads three
things: the deploy registry (what is running), the execution ledger (run history), and the
cost meter (what you've spent). From there you can stop, restart, or tail any pipeline by
name.

## Command

```bash
craw manage                       # list every deployed pipeline
craw manage stop    <name>        # stop the supervisor
craw manage restart <name>        # restart the supervisor (re-reads schedule)
craw manage logs    <name>        # tail the supervisor + run logs
```

Run `craw manage` with no arguments and each pipeline gets a row: name, status, uptime,
last run, next fire (for a cron deploy), and $ today.

## Worked example

After deploying the triage bot (see [deploy](deploy.md)):

```bash
craw manage
# NAME                     STATUS    UPTIME    LAST RUN        NEXT FIRE   $ TODAY
# crawfish/triage-bot      running   06:14:02  08:00 (ok)      08:00       $0.42
# triage-drain             running   01:09:55  18:21 (ok)      —           $0.07
```

`$ TODAY` comes from the cost meter, so you can watch spend add up per pipeline without
opening the dashboard. `NEXT FIRE` is blank for a continuous deploy.

Tail a pipeline to watch cycles fire and observer events land:

```bash
craw manage logs crawfish/triage-bot
# 08:00:01  cycle start  run=01HZ…  items=3
# 08:00:04  observer cost.spike  severity=warn  detail="run cost $0.31 > 2x median"
# 08:00:05  cycle ok     run=01HZ…  cost=$0.31
```

Stop a pipeline and the supervisor exits cleanly, leaving its registry row marked stopped.
Restart it to pick up a changed schedule:

```bash
craw manage stop    crawfish/triage-bot
craw manage restart crawfish/triage-bot
```

## Where the numbers come from

`craw manage` only reads. It pulls from three Store-backed surfaces and never re-runs a
pipeline to answer a query:

| Column           | Source                                                  |
| ---------------- | ------------------------------------------------------- |
| status, uptime   | deploy registry (the supervisor's PID entry)            |
| last run, next fire | execution ledger + the supervisor's schedule         |
| $ today          | cost meter (`CostMeter`, the live spend accumulator)    |

This is the same ledger that backs `craw inspect` / `craw logs`, so deployed and
foreground runs share one history.

## Security

`craw manage` reads scrubbed surfaces only. The registry, ledger, and cost meter hold no
secret values, because secrets are resolved by reference at run time and never stored.
`logs` tails the supervisor and run logs, which are already scrubbed. Every row is scoped
by `org_id`. See the [operations overview](operations.md) and
[SECURITY.md](../architecture/SECURITY.md).
