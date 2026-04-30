# Production Migration Path

The demo uses an in-memory Python dictionary for review storage and returns
generated PDFs inline as base64. When moving to production, two services
need to be introduced.

## Current Demo Architecture

| Concern | Demo Approach | Limitation |
|---------|--------------|------------|
| Review persistence | `_review_store` dict in `orchestrator.py` | Lost on restart; single-process |
| Decision storage | Same in-memory dict | Same as above |
| Generated PDFs | Base64 in JSON response | No long-term storage |
| Medical documents | Pasted into text field | No file upload |
| Audit trail | Embedded in response JSON | Not independently queryable |

## Why the Migration Is Straightforward

The store layer is abstracted behind four functions in `orchestrator.py`:

```python
store_review(request_id, request_data, response)
get_review(request_id)
list_reviews()
store_decision(request_id, decision)
```

No other module touches `_review_store` directly.

---

## PostgreSQL — Structured Data

Use PostgreSQL (or Azure Database for PostgreSQL — Flexible Server).

### Suggested Schema

```sql
CREATE TABLE reviews (
    request_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_name  TEXT NOT NULL,
    patient_dob   DATE NOT NULL,
    provider_npi  VARCHAR(10) NOT NULL,
    insurance_id  TEXT,
    diagnosis_codes TEXT[] NOT NULL,
    procedure_codes TEXT[] NOT NULL,
    clinical_notes TEXT NOT NULL,
    request_data  JSONB NOT NULL,
    response_data JSONB NOT NULL,
    recommendation VARCHAR(20) NOT NULL,
    confidence    NUMERIC(3,2),
    confidence_level VARCHAR(6),
    audit_justification TEXT,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id       UUID NOT NULL REFERENCES reviews(request_id),
    action          VARCHAR(20) NOT NULL,
    override_decision VARCHAR(20),
    override_rationale TEXT,
    auth_number     VARCHAR(30) NOT NULL,
    letter_text     TEXT NOT NULL,
    letter_pdf_key  TEXT,
    decided_by      TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT one_decision_per_review UNIQUE (review_id)
);

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    review_id   UUID NOT NULL REFERENCES reviews(request_id),
    event_type  VARCHAR(50) NOT NULL,
    event_data  JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_reviews_created ON reviews(created_at DESC);
CREATE INDEX idx_reviews_recommendation ON reviews(recommendation);
CREATE INDEX idx_reviews_provider ON reviews(provider_npi);
CREATE INDEX idx_audit_log_review ON audit_log(review_id);
```

### Migration Steps

1. Add `asyncpg` to `requirements.txt`
2. Add `DATABASE_URL` environment variable
3. Create `backend/app/services/database.py`
4. Update `orchestrator.py` imports
5. Run schema migration
6. Update `decision.py` for blob storage keys

---

## Azure Blob Storage — Unstructured Documents

### Container Layout

```
prior-auth-documents/
├── uploads/              # Original medical documents
│   └── {review_id}/
├── letters/              # Generated notification PDFs
│   └── {review_id}/
│       └── {auth_number}.pdf
└── audit/                # Archived audit justification docs
    └── {review_id}/
        └── audit-justification.md
```

### Documents Table

```sql
CREATE TABLE documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id   UUID NOT NULL REFERENCES reviews(request_id),
    doc_type    VARCHAR(30) NOT NULL,
    filename    TEXT NOT NULL,
    blob_url    TEXT NOT NULL,
    content_type TEXT,
    size_bytes  BIGINT,
    uploaded_by TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_documents_review ON documents(review_id);
```

### Integration Steps

1. Add `azure-storage-blob` to `requirements.txt`
2. Add `AZURE_STORAGE_CONNECTION_STRING`
3. Create `backend/app/services/blob_storage.py`
4. Upload PDFs after generation
5. Store blob key in `decisions.letter_pdf_key`
6. Add `GET /api/documents/{review_id}` endpoint

---

## Additional Dependencies

| Package | Purpose |
|---------|---------|
| `asyncpg` | Async PostgreSQL driver |
| `sqlalchemy[asyncio]` | ORM layer (optional) |
| `alembic` | Database schema migrations |
| `azure-storage-blob` | Azure Blob Storage SDK |
| `azure-identity` | Managed identity auth |

## Environment Variables

```bash
# PostgreSQL
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/priorauth

# Azure Blob Storage — prefer managed identity (backend Container App has system-assigned identity)
AZURE_STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net
# Fall back to connection string only if managed identity is not available:
# AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;...
```

---

## Azure API Management — MCP Gateway

### Why APIM for MCP?

Currently each agent container calls its 3rd-party MCP servers directly over
the public internet. In production this creates several operational risks:

- **Scattered secrets** — API keys and workaround headers (e.g. `User-Agent: claude-code/1.0`) are duplicated across every agent's Python code and environment variables.
- **No central rate limiting** — a misbehaving or hallucinating agent can flood a 3rd-party endpoint with unlimited requests.
- **No fallback** — if a 3rd-party MCP server goes down, every agent fails independently with no circuit-breaker protection.
- **No audit trail** — MCP tool calls are invisible at the infrastructure level; you only see them in application logs.
- **Custom HTTP client overhead** — each agent creates a custom `httpx.AsyncClient` solely to inject the `User-Agent` header required by DeepSense CloudFront routing (see `_MCP_HTTP_CLIENT` in any agent `main.py`).

Azure API Management's **native MCP Gateway** feature
([docs](https://learn.microsoft.com/en-us/azure/api-management/expose-existing-mcp-server))
solves all of these by acting as a protocol-aware proxy between the MAF
Hosted Agents and every external MCP endpoint. Because APIM natively speaks
the MCP protocol (Streamable HTTP and SSE transport), it handles the
transport lifecycle without custom buffering policies.

### Supported APIM Tiers

The MCP Gateway feature is **not** available on the Consumption tier.
Supported tiers (per [official documentation](https://learn.microsoft.com/en-us/azure/api-management/expose-existing-mcp-server)):

| Tier | MCP Gateway | Provisioning Time | Recommended For |
|---|---|---|---|
| Developer | ✅ | 30-60 min | Local dev/test (no SLA) |
| Basic v2 | ✅ | ~5-10 min | Cost-sensitive production |
| **Standard v2** | ✅ | ~5-10 min | **Recommended** — VNet support, balanced cost |
| Premium v2 | ✅ | ~5-10 min | Multi-region, high scale |
| Consumption | ❌ | — | Not supported |

### Deployment Strategy: Pre-Provision APIM

> **Important:** Standard v2 provisioning takes ~5-10 minutes. If your
> `azd up` pipeline must complete in under 10 minutes, **pre-provision
> the APIM instance separately** so subsequent deployments reference
> the existing resource and add only seconds to the pipeline.

**Step 1 — One-time APIM provisioning (run once, outside of `azd up`):**

```bash
# Create the APIM instance separately (takes ~5-10 min for Standard v2)
az apim create \
  --name <apim-name> \
  --resource-group <rg-name> \
  --location <region> \
  --sku-name StandardV2 \
  --publisher-name "Contoso Health" \
  --publisher-email admin@contoso.com
```

**Step 2 — Reference in Bicep as `existing`:**

```bicep
// infra/modules/apim.bicep — references the pre-provisioned instance
resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
  scope: resourceGroup()
}
```

This way `azd up` only creates the MCP server entries and policies on the
already-running APIM instance — no provisioning wait time.

### Architecture

```
MAF Hosted Agents (Azure AI Foundry)
  ├── agent-clinical  ──┐
  ├── agent-coverage  ──┤
  ├── agent-compliance──┤──► APIM MCP Gateway (https://<apim>.azure-api.net/)
  └── agent-synthesis ──┘         │
                                  │── /icd10-mcp/mcp      → mcp.deepsense.ai/icd10_codes/mcp
                                  │── /pubmed-mcp/mcp     → pubmed.mcp.claude.com/mcp
                                  │── /trials-mcp/mcp     → mcp.deepsense.ai/clinical_trials/mcp
                                  │── /npi-mcp/mcp        → mcp.deepsense.ai/npi_registry/mcp
                                  └── /cms-mcp/mcp        → mcp.deepsense.ai/cms_coverage/mcp
```

### What APIM MCP Gateway Adds

| Capability | How |
|---|---|
| **Native MCP protocol** | APIM speaks MCP natively — no custom streaming/buffering policies needed |
| **Centralized header injection** | `User-Agent: claude-code/1.0` and other headers managed via `<set-header>` policy — removed from Python code |
| **API key storage** | Named Values backed by Key Vault — never in Container App env vars |
| **Rate limiting** | `<rate-limit-by-key>` policy per MCP backend, keyed by `Mcp-Session-Id` |
| **Circuit breaker** | `<retry>` + mock policy fallback if 3rd-party goes down |
| **Upstream swap** | Change the APIM backend URL without redeploying agents |
| **Centralised monitoring** | All MCP call volume, latency and failures in one App Insights dashboard |
| **Network isolation** | Agents call a private APIM endpoint; no direct internet egress needed |

### Step-by-Step Setup

#### 1. Register MCP Backends in APIM

For each external MCP server, create an MCP Server entry in APIM. This can
be done via the Azure Portal or Bicep:

**Portal:**
1. Navigate to your APIM instance → **APIs** → **MCP Servers** → **+ Create MCP server**
2. Select **Expose an existing MCP server**
3. Enter the backend MCP server base URL (e.g. `https://mcp.deepsense.ai/icd10_codes/mcp`)
4. Set Transport type to **Streamable HTTP**
5. Enter a Name (e.g. `icd10-codes`) and Base path (e.g. `icd10-mcp`)
6. Click **Create**

Repeat for each MCP backend (PubMed, ClinicalTrials, NPI Registry, CMS Coverage).

**Bicep (automated via `azd up`):**

```bicep
// infra/modules/apim-mcp.bicep

// MCP Server for ICD-10 codes
resource icd10McpServer 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: 'icd10-mcp'
  properties: {
    displayName: 'ICD-10 Codes MCP'
    path: 'icd10-mcp'
    protocols: ['https']
    type: 'mcp'
    serviceUrl: 'https://mcp.deepsense.ai/icd10_codes/mcp'
  }
}

// Repeat for pubmed, clinical-trials, npi-registry, cms-coverage
```

#### 2. Configure Policies (Header Injection)

The external DeepSense MCP servers require a specific `User-Agent` header
to avoid CloudFront 301 redirects. Apply an inbound policy to inject this
header centrally:

```xml
<policies>
    <inbound>
        <base />
        <!-- DeepSense CloudFront requires this User-Agent to route MCP traffic -->
        <set-header name="User-Agent" exists-action="override">
            <value>claude-code/1.0</value>
        </set-header>
    </inbound>
    <backend>
        <base />
    </backend>
    <outbound>
        <base />
    </outbound>
    <on-error>
        <base />
    </on-error>
</policies>
```

Apply this policy to each MCP server that routes to DeepSense endpoints.

#### 3. Configure Rate Limiting (Optional but Recommended)

Add per-session rate limiting to prevent runaway agent loops:

```xml
<inbound>
    <base />
    <set-variable name="body" value="@(context.Request.Body.As<string>(preserveContent: true))" />
    <choose>
        <when condition="@(
            Newtonsoft.Json.Linq.JObject.Parse((string)context.Variables[&quot;body&quot;])[&quot;method&quot;] != null
            && Newtonsoft.Json.Linq.JObject.Parse((string)context.Variables[&quot;body&quot;])[&quot;method&quot;].ToString() == &quot;tools/call&quot;
        )">
            <rate-limit-by-key
                calls="10"
                renewal-period="60"
                counter-key="@(context.Request.Headers.GetValueOrDefault(&quot;Mcp-Session-Id&quot;, &quot;unknown&quot;))" />
        </when>
    </choose>
</inbound>
```

#### 4. Update Agent Environment Variables

In `infra/main.bicep`, update each agent Container App's `MCP_*` env vars
to point at the APIM MCP Gateway URLs:

```bicep
// Before (direct internet call):
{ name: 'MCP_ICD10_CODES', value: 'https://mcp.deepsense.ai/icd10_codes/mcp' }

// After (via APIM MCP Gateway):
{ name: 'MCP_ICD10_CODES', value: '${apim.outputs.gatewayUrl}/icd10-mcp/mcp' }
```

#### 5. Simplify Agent Python Code

Once APIM handles the `User-Agent` header injection, you can drop the
`headers=` argument and the custom `httpx.AsyncClient` from each
`MCPStreamableHTTPTool` instance in the agents' `main.py`:

```python
# BEFORE (current — custom HTTP client in every agent):
_MCP_HTTP_CLIENT = httpx.AsyncClient(
    headers={"User-Agent": "prior-auth-clinical/1.0"},
    timeout=httpx.Timeout(60.0),
)
icd10_tool = MCPStreamableHTTPTool(
    name="icd10-codes",
    url=os.environ["MCP_ICD10_CODES"],
    http_client=_MCP_HTTP_CLIENT,
    load_prompts=False,
)

# AFTER (with APIM — no custom client needed):
icd10_tool = MCPStreamableHTTPTool(
    name="icd10-codes",
    url=os.environ["MCP_ICD10_CODES"],  # now points to APIM
    load_prompts=False,
)
```

### Agent Environment Variable Mapping

| Variable | Current value (direct) | APIM MCP Gateway value |
|---|---|---|
| `MCP_ICD10_CODES` | `https://mcp.deepsense.ai/icd10_codes/mcp` | `https://<apim>.azure-api.net/icd10-mcp/mcp` |
| `MCP_PUBMED` | `https://pubmed.mcp.claude.com/mcp` | `https://<apim>.azure-api.net/pubmed-mcp/mcp` |
| `MCP_CLINICAL_TRIALS` | `https://mcp.deepsense.ai/clinical_trials/mcp` | `https://<apim>.azure-api.net/trials-mcp/mcp` |
| `MCP_NPI_REGISTRY` | `https://mcp.deepsense.ai/npi_registry/mcp` | `https://<apim>.azure-api.net/npi-mcp/mcp` |
| `MCP_CMS_COVERAGE` | `https://mcp.deepsense.ai/cms_coverage/mcp` | `https://<apim>.azure-api.net/cms-mcp/mcp` |

> All five MCP servers are wired in-container via `MCPStreamableHTTPTool`
> (URLs read from the `MCP_*` env vars above), so switching to APIM only
> requires updating the env var values in each `agents/<name>/agent.yaml`
> and `docker-compose.yml`. No code changes are needed beyond the optional
> simplification shown in step 5.

### Diagnostic Logging Caveat

> **Important:** If you enable Application Insights diagnostic logging at
> the global scope (All APIs) for your APIM instance, set the **Number of
> payload bytes to log** for **Frontend Response** to `0`. This prevents
> response body logging from interfering with MCP streaming transport.
> Configure payload logging selectively at the individual MCP server scope
> if needed.

### Limitations (as of March 2026)

- The external MCP server must conform to MCP version `2025-06-18` or later.
- APIM MCP Gateway supports MCP **tools** and **resources**, but does **not** support MCP **prompts** (which is fine — we set `load_prompts=False` in all agents).
- APIM does not display tools from the existing MCP server in the portal; tools are registered and managed on the remote server.
- MCP server capabilities are not supported in APIM [Workspaces](https://learn.microsoft.com/en-us/azure/api-management/workspaces-overview).

---

## What NOT to Change

- **Agent containers** — the four MAF Hosted Agent containers (clinical, coverage, compliance, synthesis) call the Foundry Responses API and return JSON. They are completely unaware of the backend's storage layer.
- **Frontend** — the API contract stays the same
- **MCP server configuration** — independent of storage
- **Notification letter templates** — produce same output regardless of storage
