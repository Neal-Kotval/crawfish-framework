"""Project scaffolding — the zero-key 5-minute wow.

``craw init`` writes a **self-contained project** (root = the project; ``.crawfish/``
is generated state only) with a genuinely useful hero example — the triage-bot
Definition — so ``craw dev`` produces an impressive result in one command, no API key.
"""
# ruff: noqa: E501 - this module is template data; some embedded lines exceed the limit

from __future__ import annotations

from pathlib import Path

__all__ = ["scaffold_project", "FILES"]

# path (relative to project root) -> contents
FILES: dict[str, str] = {
    "crawfish.toml": """\
[project]
name = "crawfish-app"
version = "0.1.0"
default_profile = "dev"

[profiles.dev]
runtime = "command"   # claude -p — zero API key

[profiles.prod]
runtime = "managed"   # CMA

[models]
default = "claude-opus-4-8"   # fallback for unpinned agents — change freely

[models.aliases]
# Name a model once, reuse it across agents (any provider). Each alias must map
# to a concrete provider:model id, never to another alias.
fast = "claude-haiku-4-5"

[capabilities]
secrets = []
egress = []
""",
    ".env.example": "# Copy to .env (gitignored). Secrets are referenced by name, never inline.\n# GITHUB_TOKEN=...\n# LINEAR_API_KEY=...\n",
    ".gitignore": ".env\n.env.*\n!.env.example\n.crawfish/\n__pycache__/\n*.pyc\n",
    "README.md": """\
# crawfish-app

A Crawfish project — agents for bulk work over your data.

```bash
craw dev definitions/triage-bot -i project=acme -i "ticket_body=login is broken"
craw test definitions/triage-bot --fixtures fixtures
craw build         # generate a deployable Containerfile
```

See the framework docs for the directory model, pipelines, and the security spine.
""",
    "definitions/triage-bot/instructions.md": """\
---
role: lead
delegates_to: [classifier, summarizer]
---
You triage an incoming support ticket. Delegate classification and summarization to
your subagents, then combine their typed results into a single triage decision.
""",
    "definitions/triage-bot/agents/classifier.md": "You classify a support ticket as exactly one of: bug, question, feature_request.\n",
    "definitions/triage-bot/agents/summarizer.md": "You write a single-sentence summary of a support ticket for a triage queue.\n",
    "definitions/triage-bot/definition.py": """\
\"\"\"Typed IO boundary. `project` is static config; `ticket_body` is untrusted fluid data.\"\"\"

from __future__ import annotations

from crawfish.core import Flow, Parameter

inputs = [
    Parameter(name="project", type="str", flow=Flow.STATIC),
    Parameter(name="ticket_body", type="str"),
]
outputs = [Parameter(name="triage", type="str")]

lead = "lead"
""",
    "definitions/triage-bot/pyproject.toml": '[project]\nname = "triage-bot"\nversion = "0.1.0"\n',
    "fixtures/login-bug.json": '{"inputs": {"project": "acme", "ticket_body": "the login button does nothing"}}\n',
    "sources/.gitkeep": "",
    "sinks/.gitkeep": "",
}


def scaffold_project(name: str = "crawfish-app") -> Path:
    """Create a self-contained project directory and return its path."""
    root = Path(name)
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root
