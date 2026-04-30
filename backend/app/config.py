import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")

    # ── Docker Compose (direct HTTP) mode ──────────────────────────────────────
    # docker-compose.yml hardcodes these to Docker service names; no .env entry
    # needed for local docker-compose up. Clear/omit to use Foundry mode.
    HOSTED_AGENT_CLINICAL_URL: str = os.getenv("HOSTED_AGENT_CLINICAL_URL", "")
    HOSTED_AGENT_COMPLIANCE_URL: str = os.getenv("HOSTED_AGENT_COMPLIANCE_URL", "")
    HOSTED_AGENT_COVERAGE_URL: str = os.getenv("HOSTED_AGENT_COVERAGE_URL", "")
    HOSTED_AGENT_SYNTHESIS_URL: str = os.getenv("HOSTED_AGENT_SYNTHESIS_URL", "")

    # ── Foundry Hosted Agents mode ──────────────────────────────────────────────
    # On Azure (azd up), Bicep injects AZURE_AI_PROJECT_ENDPOINT and the 4 agent
    # name vars automatically. The backend obtains a per-agent OpenAI client via
    # AIProjectClient.get_openai_client(agent_name=...) which is bound to the
    # agent's dedicated endpoint — no direct URLs and no agent_reference body.
    AZURE_AI_PROJECT_ENDPOINT: str = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "")
    HOSTED_AGENT_CLINICAL_NAME: str = os.getenv(
        "HOSTED_AGENT_CLINICAL_NAME", "clinical-reviewer-agent"
    )
    HOSTED_AGENT_COMPLIANCE_NAME: str = os.getenv(
        "HOSTED_AGENT_COMPLIANCE_NAME", "compliance-agent"
    )
    HOSTED_AGENT_COVERAGE_NAME: str = os.getenv(
        "HOSTED_AGENT_COVERAGE_NAME", "coverage-assessment-agent"
    )
    HOSTED_AGENT_SYNTHESIS_NAME: str = os.getenv(
        "HOSTED_AGENT_SYNTHESIS_NAME", "synthesis-agent"
    )

    HOSTED_AGENT_TIMEOUT_SECONDS: float = float(
        os.getenv("HOSTED_AGENT_TIMEOUT_SECONDS", "180")
    )

    # Optional auth/header for specific direct-HTTP deployments (rarely needed;
    # Foundry mode uses DefaultAzureCredential automatically).
    HOSTED_AGENT_AUTH_HEADER: str = os.getenv("HOSTED_AGENT_AUTH_HEADER", "Authorization")
    HOSTED_AGENT_AUTH_SCHEME: str = os.getenv("HOSTED_AGENT_AUTH_SCHEME", "Bearer")
    HOSTED_AGENT_AUTH_TOKEN: str = os.getenv("HOSTED_AGENT_AUTH_TOKEN", "")

    # Azure Application Insights (observability)
    APPLICATION_INSIGHTS_CONNECTION_STRING: str = os.getenv(
        "APPLICATION_INSIGHTS_CONNECTION_STRING", ""
    )


settings = Settings()
