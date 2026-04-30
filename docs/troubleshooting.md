# Troubleshooting

## MAF Agent Fails to Start / "Failed to acquire Foundry auth token"

The backend logs show an auth error when trying to invoke a hosted agent.

**Cause:** `DefaultAzureCredential` cannot acquire a token for the Foundry Responses API.

**Fix:**

1. **Local dev (Docker Compose):** Ensure you are logged in to Azure CLI:
   ```bash
   az login
   az account set --subscription <your-subscription-id>
   ```

2. **Azure (production):** Verify the backend Container App's managed identity has the `CognitiveServicesOpenAIUser` role on the Foundry account, and the Foundry project's managed identity has `Cognitive Services OpenAI Contributor` and `Azure AI User` roles on the Foundry account:
   - Check `infra/modules/role-assignments.bicep`
   - Re-run `azd provision` to reapply role assignments if missing

3. **Both:** Confirm `AZURE_AI_PROJECT_ENDPOINT` is set correctly:
   ```
   https://<account>.services.ai.azure.com/api/projects/<project-name>
   ```
   The `/api/projects/<project-name>` segment is required — the bare account endpoint will not work.

---

## PubMed MCP: "Session terminated" / search_articles Fails

PubMed literature search fails with `search_articles: PubMed search failed` but
other MCP tools (ICD-10, Clinical Trials, NPI, CMS) work fine.

**Cause:** PubMed's MCP server (`pubmed.mcp.claude.com`) terminates idle sessions
after ~10 minutes. The agent container reuses the same MCP session across requests.
If the session has been idle too long between user submissions, PubMed responds
with `McpError("Session terminated")`.

**Fix (already applied):** The clinical agent uses `_ReconnectingMCPTool` — a
subclass of `MCPStreamableHTTPTool` that catches `Session terminated` errors
and automatically reconnects with a fresh session. See `agents/clinical/main.py`.

If you still see this error, check:
1. The container image was rebuilt after the fix (`azd up`)
2. The agent revision includes the `_ReconnectingMCPTool` change (check the
   image tag via `az rest --method GET --url "${BASE_URL}/agents/clinical-reviewer-agent?api-version=v1" --resource "https://ai.azure.com"`)

---

## Agent Returns "ID cannot be null or empty" / status: "failed"

All agent calls fail with `400 - ID cannot be null or empty` or return
`status: "failed"` with empty output.

**Cause (initial preview, now resolved):** In the initial Hosted Agents preview,
`MCPTool` entries in `HostedAgentDefinition.tools` triggered a
`UserInfoContextMiddleware` that called `/agents/{name}/tools/resolve`, which
failed in regions where the API was unavailable.

**Fix (current):** This accelerator wires all five MCP servers **in-container**
via `MCPStreamableHTTPTool` (read from `MCP_*` env vars in each
`agents/<name>/agent.yaml`), so the platform `tools/resolve` flow is no longer
on the hot path. If you still hit this error, confirm you are on the new
package set (`agent-framework-foundry-hosting>=1.0.0a260424`,
`azure-ai-agentserver-core>=2.0.0b3`, `azure-ai-projects>=2.1.0`) and that
`azd deploy` ran cleanly to redeploy the agent images.

---

## Agents Return Empty or Error Responses

Agents connect but return `{"error": "...", "tool_results": []}` instead of structured output.

**Cause 1 — Wrong project endpoint:** `AZURE_AI_PROJECT_ENDPOINT` points to the bare account endpoint instead of the project endpoint.

**Cause 2 — Agent not registered:** The hosted agent was not successfully deployed/registered with Foundry Agent Service.

**Cause 3 — Model deployment missing:** The `AZURE_OPENAI_DEPLOYMENT_NAME` (e.g., `gpt-5.4`) doesn't exist in the Foundry project.

**Fix:**

1. Verify agents are registered:
   ```bash
   azd ai agent list
   # or run the pre-flight health check:
   python scripts/check_agents.py
   ```

2. Confirm the endpoint format in `AZURE_AI_PROJECT_ENDPOINT` includes `/api/projects/<project>`.

3. In the Foundry portal under **Build** → **Deployments**, confirm the gpt-5.4 deployment exists and its name matches `AZURE_OPENAI_DEPLOYMENT_NAME` declared in each `agents/<name>/agent.yaml`.

---

## "Failed to proxy" / ECONNREFUSED / "Review failed"

The frontend shows an error when submitting a review.

**Cause:** The backend server is not running, or the frontend is not
configured to reach it.

**Fix:**

1. Ensure the backend is running:
   ```bash
   cd backend
   uvicorn app.main:app --reload
   ```

2. Ensure `frontend/.env.local` has the correct backend URL:
   ```
   NEXT_PUBLIC_API_BASE=http://localhost:8000/api
   ```

3. Restart the frontend dev server after changing `.env.local`:
   ```bash
   cd frontend
   npm run dev
   ```

---

## Agent phase fails immediately

The review fails as soon as a specialist phase starts.

**Cause:** One or more hosted agent endpoint URLs are missing or unreachable.

**Fix:** Check which dispatch mode is active:

**Docker Compose (local dev):** Verify all URL variables are set in `backend/.env` or `docker-compose.yml`:

- `HOSTED_AGENT_COMPLIANCE_URL`
- `HOSTED_AGENT_CLINICAL_URL`
- `HOSTED_AGENT_COVERAGE_URL`
- `HOSTED_AGENT_SYNTHESIS_URL`

Make sure `docker-compose.yml` is running all four agent containers and that their ports match the URLs above.

**Foundry Hosted Agents (production / `azd up`):** Verify these variables are set (injected automatically by Bicep):

- `AZURE_AI_PROJECT_ENDPOINT`
- `HOSTED_AGENT_CLINICAL_NAME`
- `HOSTED_AGENT_COMPLIANCE_NAME`
- `HOSTED_AGENT_COVERAGE_NAME`
- `HOSTED_AGENT_SYNTHESIS_NAME`

If set manually, confirm the project endpoint format:
`https://<account>.services.ai.azure.com/api/projects/<project-name>`

Also confirm the agents were successfully deployed by `azd deploy` (which
invokes the `azd ai agent` extension's `create_version()` flow) — check the
`azd deploy` output for registration errors, or run `azd ai agent list`.

You can verify all deployment health at any time with:

```bash
python scripts/check_agents.py
```

This checks agent registration, App Insights connectivity, MCP tool connections,
backend health, and frontend availability.

---

## Hosted-agent authentication returns 401 or 403

The backend reaches the hosted endpoint but receives an authorization failure.

**Cause:** The outbound auth header configuration does not match the hosted
agent deployment.

**Fix:** Depends on the dispatch mode:

**Docker Compose:** Direct HTTP to containers — no auth configured by default. If containers are behind a proxy requiring auth, set `HOSTED_AGENT_AUTH_HEADER`, `HOSTED_AGENT_AUTH_SCHEME`, and `HOSTED_AGENT_AUTH_TOKEN` in `.env`.

**Foundry Hosted Agents (production):** Credentials come from `DefaultAzureCredential`. Common causes:

- The backend ACA managed identity is missing the `CognitiveServicesOpenAIUser` role on the Foundry account — check `infra/modules/role-assignments.bicep` and re-run `azd provision`
- The Foundry project managed identity is missing `Cognitive Services OpenAI Contributor` or `Azure AI User` on the Foundry account — these roles are required for hosted agent containers to call gpt-5.4 and use Agent Service data actions
- The deployer user is missing the `Azure AI User` role on the Foundry project (required by the `azd ai agent` extension to call `create_version()`) — this role is auto-assigned by `az role assignment create` in the postprovision hook; re-run `azd up` to fix
- `AZURE_AI_PROJECT_ENDPOINT` is pointing to the wrong project or account
- One or more per-agent instance identities are missing `Azure AI User` on the Foundry account — the postdeploy hook (`scripts/grant_agent_rbac.py`) grants this; re-run `azd hooks run postdeploy` to retry
- The agents were not successfully deployed — check the `azd deploy` output for `create_version` errors

---

## Agent Registration Fails with PermissionDenied on First Run

`azd deploy` (or `azd ai agent` directly) fails with:
```
ERROR: (PermissionDenied) The principal ... lacks the required data action
Microsoft.CognitiveServices/accounts/AIServices/agents/write
```
or
```
ERROR: (AuthorizationFailed) ... does not have authorization to perform action
'Microsoft.CognitiveServices/accounts/AIServices/projects/agents/deployments/write'
```

**Cause:** Either RBAC propagation delay, or the deployer is missing the
**Azure AI Project Manager** role. The refreshed Hosted Agents preview
(Apr 2026) requires Project Manager at the project scope to call
`create_version()` on `HostedAgentDefinition` / `PromptAgentDefinition` —
`Azure AI User` only covers invoking an existing agent, not deploying one.
See the [official permissions reference](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent#required-permissions).

**Automatic handling:** The postprovision hook assigns both `Azure AI User`
and `Azure AI Project Manager` to the deployer at the project scope before
`azd deploy` runs the `azd ai agent` create_version flow, so RBAC is in place
by the time agents are registered. The postdeploy hook
(`scripts/grant_agent_rbac.py`) then grants `Azure AI User` to each per-agent
instance identity on the Foundry account. On first deployment, ~60 seconds of
RBAC propagation may be needed before the first runtime call succeeds — if you
see a 403 from a hosted agent immediately after deploy, wait ~60s and retry.

**If retries fail:** Simply re-run `azd up` (or `azd deploy && azd hooks run
postdeploy`) — the roles already exist so subsequent runs proceed without
additional propagation delay. If failures persist, manually verify the
assignments:
```bash
az role assignment list \
  --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<foundry>/projects/<project>" \
  --query "[].roleDefinitionName" -o tsv
```
Both `Azure AI User` and `Azure AI Project Manager` should appear.

---

## Hosted agent returns an unexpected payload shape

The backend reaches the hosted agent, but parsing or downstream validation
fails.

**Expected payload (Foundry Responses API envelope):**

```json
{
  "output": [{"content": [{"text": "{\"field\": \"value\", ...}"}]}]
}
```

The backend uses `response.output_text` from the OpenAI SDK and parses the JSON result directly.

**Fix:** Confirm the agent container is returning the standard Foundry Responses
API envelope. The Microsoft Agent Framework `ResponsesHostServer(agent).run()`
produces this format automatically.

---

## Port Stuck After Killing Server (Windows)

After killing a server process, the port remains in LISTENING state.

**Cause:** Windows TCP socket lingering.

**Fix:** Wait 2-4 minutes for the socket to clear, or use a different port.

---

## Agent Returns Truncated/Incomplete Response

One or more agents return partial data with missing top-level keys.

**Cause:** The agent's `response_format` structured output was not fully populated by the model response, typically due to token limits or a model timeout.

**Symptoms in server logs:**

```
WARNING app.agents.orchestrator: Clinical Reviewer Agent returned incomplete result (attempt 1/2). Missing keys: clinical_extraction, clinical_summary. Retrying...
INFO app.agents.orchestrator: Clinical Reviewer Agent succeeded on retry (attempt 2/2)
```

A normal Clinical result has 3 expected top-level keys (`diagnosis_validation`, `clinical_extraction`, `clinical_summary`). Additional fields like `procedure_validation`, `tool_results`, and `clinical_trials` are also present but not checked by the validation gate.

**Mitigations (in place):**

1. **Result validation** — checks for expected top-level keys via `_EXPECTED_KEYS` in `orchestrator.py`
2. **Automatic retry** — retries once (`_MAX_AGENT_RETRIES = 1`) if validation fails
3. **SSE warnings** — surfaces validation warnings to the frontend

**If retries consistently fail:**
- The agent's `HOSTED_AGENT_TIMEOUT_SECONDS` (default `180`) may be too low — increase it in `backend/.env`
- Check the agent container logs in Foundry portal for model errors or context overflow

---

## Troubleshooting Foundry Traces

If traces don't appear in Foundry (Trace ID = "--", Duration = "--", Tokens = "--"):

- Verify the Foundry project has Application Insights configured
- If App Insights was added after agent registration, unregister and re-register
- Verify your backend sends traces to the **same** Application Insights resource
- **Verify `gen_ai.agent.id` is populated in spans.** The refreshed Hosted
  Agents preview populates `gen_ai.agent.id` and `gen_ai.agent.name` natively
  from the platform-injected `FOUNDRY_AGENT_NAME` env var — the previous
  `_patch_trace_agent_id()` monkey-patch is no longer needed and has been
  removed from all four agents.
  You can verify by querying App Insights:
  ```kql
  traces
  | where cloud_RoleName == 'azure.ai.agentserver'
  | where message has 'agent_run'
  | extend agentId = tostring(parse_json(customDimensions)['gen_ai.agent.id'])
  | project timestamp, agentId
  ```
  If `agentId` is empty, confirm `FOUNDRY_AGENT_NAME` is set in the agent
  container env (it is injected automatically by the Foundry runtime).
- **Check the env var name:** The Foundry agentserver adapter expects
  `APPLICATIONINSIGHTS_CONNECTION_STRING` (no underscore between APPLICATION
  and INSIGHTS). This is different from `APPLICATION_INSIGHTS_CONNECTION_STRING`
  used by the `azure-monitor-opentelemetry` SDK. Both must be set. See
  [technical-notes.md](technical-notes.md#enabling-observability) for details.
- If the Foundry Operate tab shows "0/3 monitoring features enabled," the
  adapter-expected env var is missing or empty
