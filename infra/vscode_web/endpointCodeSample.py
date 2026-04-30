"""Sample: invoke a Foundry Hosted Agent via the refreshed-preview Responses API.

The agent already enforces a structured JSON `response_format` server-side
(via `default_options` on `Agent`), so `response.output_text` is a JSON string
you can `json.loads()` directly — no thread/message/run lifecycle needed.
"""

import json

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

# allow_preview=True is required to use the refreshed Hosted Agents preview
# (per-agent dedicated endpoints, no thread/message/run lifecycle).
project = AIProjectClient(
    endpoint="<%= endpoint %>",
    credential=DefaultAzureCredential(),
    allow_preview=True,
)

# get_openai_client(agent_name=...) returns an OpenAI-compatible client pre-bound
# to the agent's dedicated endpoint:
#   {endpoint}/agents/<%= agentId %>/endpoint/protocols/openai/v1/responses
openai_client = project.get_openai_client(agent_name="<%= agentId %>")

response = openai_client.responses.create(
    input=[{"type": "message", "role": "user", "content": "<%= userMessage %>"}],
)

print(f"status={response.status}")

# `output_text` is the JSON-serialised Pydantic model the agent emits.
try:
    print(json.dumps(json.loads(response.output_text), indent=2))
except (json.JSONDecodeError, TypeError):
    print(response.output_text)
