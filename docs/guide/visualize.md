# Visualize — the localhost dashboard

`craw visualize` serves a dashboard over the run-info surface: running pipelines, recent
runs and their status, what you've spent today, and observer events. It's plain HTML and
JS, with no build step and no framework, and it binds to loopback only.

## Command

```bash
craw visualize [--port <port>]
# dashboard on http://127.0.0.1:7878  (loopback only)
```

The server binds `127.0.0.1` only, so no other host can reach it. The default port is
`7878`. Open the URL in a browser. The page reads the same Store-backed run-info surface
that [observers](observers.md) write and [`craw manage`](manage.md) queries, so it
reflects deployed pipelines live.

## What it shows

- **Running pipelines** — name, status, uptime, next fire (from the deploy registry).
- **Recent runs** — per-run status, backend, version, cost, item count (from `RunInfo`).
- **$ today** — spend rolled up from the cost meter.
- **Observer events** — the `ObserverEvent` stream, newest first, with severity.

The dashboard is a read-only mirror. It never triggers a run or changes state.

## Worked example

With the triage bot [deployed](deploy.md) and an [observer](observers.md) attached:

```bash
craw deploy demo/triage-bot --schedule "0 8 * * *"
craw visualize
# dashboard on http://127.0.0.1:7878
```

Open `http://127.0.0.1:7878`. You'll see `crawfish/triage-bot` under **Running
pipelines**. Each 08:00 cycle shows up under **Recent runs** as it completes, the
running **$ today** total updates, and any `cost.spike` or `quality.flag` events the
observer emitted land in the **Observer events** panel.

To free the default port, or to run two dashboards at once, pick another:

```bash
craw visualize --port 9000
# dashboard on http://127.0.0.1:9000
```

## Security

The dashboard is small and closed by design:

- **Loopback-only.** It binds `127.0.0.1`, so nothing off the machine can reach it. There
  is no auth layer because there is no network surface to authenticate.
- **Scrubbed surface only.** It renders the same scrubbed run-info surface as the rest of
  the observe layer. Events and run-info are scrubbed before the Store write, so **no
  secret value** can appear in the UI.
- **Read-only.** It has no control actions. Deploy, stop, and restart live in
  [`craw manage`](manage.md).

See the [operations overview](operations.md) and [SECURITY.md](../architecture/SECURITY.md).
