# crawfish-slack

A Slack message **sink** for [Crawfish](https://github.com/Neal-Kotval/crawfish-framework) —
and the worked example every new connector copies.

It posts a message to a static Slack channel, holds its bot token **by reference**
(an env-var name, never the value), and runs **dry-run by default** so tests stay
offline and deterministic.

```python
from crawfish_slack import SlackSink

sink = SlackSink(config={"channel": "#alerts", "credential_ref": "SLACK_BOT_TOKEN"})
```

Installing this package registers the sink through a real entry point, so
`Registry.discover()` finds it as `("sink", "slack")` with zero wiring.

See the full walkthrough: **[Contributing a connector](https://github.com/Neal-Kotval/crawfish-framework/blob/main/docs/guide/contributing-a-connector.md)**.
