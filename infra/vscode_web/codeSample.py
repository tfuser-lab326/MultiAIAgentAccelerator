"""Sample: invoke a Foundry Hosted Agent via the refreshed-preview Responses API.

The agent enforces a structured JSON `response_format` server-side, so
`response.output_text` is a JSON string you can parse directly — no
thread/message/run lifecycle is needed in the refreshed preview.
"""

import json

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

project = AIProjectClient(
    endpoint="<%= endpoint %>",
    credential=DefaultAzureCredential(),
    allow_preview=True,
)

openai_client = project.get_openai_client(agent_name="<%= agentId %>")

response = openai_client.responses.create(
    input=[{"type": "message", "role": "user", "content": "<%= userMessage %>"}],
)

print(f"status={response.status}")
try:
    print(json.dumps(json.loads(response.output_text), indent=2))
except (json.JSONDecodeError, TypeError):
    print(response.output_text)
