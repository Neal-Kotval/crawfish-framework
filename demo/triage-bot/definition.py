"""triage-bot — typed IO boundary. `project` is static config; `ticket_body` is
untrusted per-item fluid data (the prompt-injection boundary).

The output is a typed ``Triage`` **record** (CRA-172): the classifier/summarizer team
returns a structured ``{category, severity, summary}`` value, not a free-text blob — so
``Output.value`` is a validated ``dict`` and downstream metrics/diffs key off real fields.
The record type is registered on the process-wide ``default_registry`` at import so
``validate_output`` can walk it.
"""

from __future__ import annotations

from crawfish.core import Flow, Parameter
from crawfish.typesystem import default_registry

default_registry.register_record(
    "Triage",
    {"category": "str", "severity": "str", "summary": "str"},
)

inputs = [
    Parameter(name="project", type="str", flow=Flow.STATIC),
    Parameter(name="ticket_body", type="str"),  # fluid (per-item)
]
outputs = [Parameter(name="triage", type="Triage")]  # typed RECORD output

lead = "lead"
