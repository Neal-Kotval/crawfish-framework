"""pr-drafter — typed IO boundary for the Batch stage of the linear-to-pr demo.

Every input is fluid (untrusted per-item Linear data): the issue's fields reach the
model as *data*, never as instructions (the prompt-injection boundary). The single
output is the drafted Markdown body for the pull request.
"""

from __future__ import annotations

from crawfish.core import Parameter

inputs = [
    Parameter(name="identifier", type="str"),  # e.g. "CRA-201" (fluid)
    Parameter(name="title", type="str"),  # fluid
    Parameter(name="description", type="str"),  # fluid
    Parameter(name="branch", type="str"),  # ticket-provided head branch (fluid)
]
outputs = [Parameter(name="markdown", type="str")]
