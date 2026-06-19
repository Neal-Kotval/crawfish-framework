"""triage-bot — typed IO boundary. `project` is static config; `ticket_body` is
untrusted per-item fluid data (the prompt-injection boundary)."""

from __future__ import annotations

from crawfish.core import Flow, Parameter

inputs = [
    Parameter(name="project", type="str", flow=Flow.STATIC),
    Parameter(name="ticket_body", type="str"),  # fluid (per-item)
]
outputs = [Parameter(name="triage", type="str")]

lead = "lead"
