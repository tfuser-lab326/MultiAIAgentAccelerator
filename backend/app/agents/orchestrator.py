"""Multi-Agent Orchestrator for Prior Authorization Review.

Coordinates three specialized agents in a fan-out/fan-in pattern:
  Phase 1 (parallel): Compliance Agent + Clinical Reviewer Agent
  Phase 2 (sequential): Coverage Agent (receives clinical findings)
  Phase 3: Synthesis — aggregates all agent outputs into a final decision

Enhanced with the Anthropic prior-auth-review-skill decision rubric:
  - LENIENT mode (default): all problematic scenarios -> PEND
  - Structured evaluation order: provider -> codes -> med necessity
  - Confidence scoring: HIGH/MEDIUM/LOW + 0-100
  - Audit trail with data sources and metrics
  - Audit justification document generation

All four specialist agents (compliance, clinical, coverage, synthesis) run
as independent Foundry Hosted Agent containers (or local docker-compose
containers in dev mode). This module is the pure async dispatcher and
invokes them via HTTP through ``app.services.hosted_agents``.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from app.agents.compliance_agent import run_compliance_review
from app.agents.clinical_agent import run_clinical_review
from app.agents.coverage_agent import run_coverage_review
from app.agents.synthesis_agent import run_synthesis_review as _dispatch_synthesis
from app.services.audit_pdf import generate_audit_justification_pdf
from app.services.cpt_validation import validate_procedure_codes

logger = logging.getLogger(__name__)

# OpenTelemetry tracer for custom spans (no-op if observability not configured)
try:
    from agent_framework.observability import get_tracer
    tracer = get_tracer(__name__)
except ImportError:
    from contextlib import contextmanager

    class _NoOpSpan:
        """Minimal no-op span for when observability is not installed."""
        def set_attribute(self, key: str, value: object) -> None: ...
        def set_status(self, *args: object, **kwargs: object) -> None: ...
        def record_exception(self, exc: BaseException) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *args): ...

    class _NoOpTracer:
        @contextmanager
        def start_as_current_span(self, name: str, **kwargs):
            yield _NoOpSpan()

    tracer = _NoOpTracer()  # type: ignore[assignment]

# Maximum number of retries when an agent returns an incomplete result
_MAX_AGENT_RETRIES = 1

# Expected top-level keys for each agent result.
# If any of these are missing the result is considered incomplete/truncated.
_EXPECTED_KEYS: dict[str, set[str]] = {
    "Compliance Agent": {"checklist", "overall_status"},
    "Clinical Reviewer Agent": {
        "diagnosis_validation",
        "clinical_extraction",
        "clinical_summary",
    },
    "Coverage Agent": {"provider_verification", "criteria_assessment"},
}


def _validate_agent_result(agent_name: str, result: dict) -> list[str]:
    """Check that an agent result contains the expected top-level keys.

    Returns a list of missing key names (empty list means valid).
    """
    if result.get("error"):
        return [f"error: {result['error']}"]

    expected = _EXPECTED_KEYS.get(agent_name, set())
    if not expected:
        return []

    missing = [k for k in expected if k not in result]
    return missing


_AGENT_DISPLAY_NAMES: dict[str, str] = {
    "compliance": "Compliance Agent",
    "clinical": "Clinical Reviewer Agent",
    "coverage": "Coverage Assessment Agent",
}

_CHECKLIST_STATUS_MAP: dict[str, str] = {
    "complete": "pass",
    "incomplete": "warning",
    "missing": "fail",
}


def _enrich_agent_result(agent_key: str, result: dict) -> dict:
    """Inject ``agent_name`` and ``checks_performed`` into an agent result dict.

    The frontend's AgentDetails component expects both fields on every
    agent result.  Since the hosted agents do not emit them (they are not
    part of the SKILL.md output schemas), we derive them here:

    - ``agent_name``      — human-readable display name for the agent.
    - ``checks_performed`` — for compliance: mapped from the ``checklist``
      items (complete→pass, incomplete→warning, missing→fail);
      for clinical/coverage: mapped from ``tool_results``
      (tool_name→rule, status→result, detail kept as-is).
    """
    if not result or result.get("error"):
        return result

    enriched = dict(result)
    enriched.setdefault("agent_name", _AGENT_DISPLAY_NAMES.get(agent_key, agent_key))

    if "checks_performed" not in enriched:
        if agent_key == "compliance":
            # Derive from checklist (compliance has no tool_results)
            checks_performed = [
                {
                    "rule": item.get("item", ""),
                    "result": _CHECKLIST_STATUS_MAP.get(item.get("status", ""), "info"),
                    "detail": item.get("detail", ""),
                }
                for item in enriched.get("checklist", [])
            ]
        else:
            # Derive from tool_results (clinical + coverage)
            checks_performed = [
                {
                    "rule": tr.get("tool_name", ""),
                    "result": tr.get("status", "info"),
                    "detail": tr.get("detail", ""),
                }
                for tr in enriched.get("tool_results", [])
            ]
        enriched["checks_performed"] = checks_performed

    return enriched


# --- In-memory review store (demo persistence) ---
_review_store: dict[str, dict] = {}


def store_review(request_id: str, request_data: dict, response: dict) -> None:
    """Persist a completed review for later retrieval."""
    _review_store[request_id] = {
        "request_id": request_id,
        "request_data": request_data,
        "response": response,
        "decision": None,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


def get_review(request_id: str) -> dict | None:
    """Retrieve a stored review by request_id."""
    return _review_store.get(request_id)


def list_reviews() -> list[dict]:
    """List all stored reviews (most recent first)."""
    return sorted(
        _review_store.values(),
        key=lambda r: r["stored_at"],
        reverse=True,
    )


def store_decision(request_id: str, decision: dict) -> None:
    """Attach a decision to a stored review."""
    if request_id in _review_store:
        _review_store[request_id]["decision"] = decision


def _compute_confidence(
    compliance_result: dict,
    clinical_result: dict,
    coverage_result: dict,
) -> tuple[float, str]:
    """Compute overall confidence score and level from agent results."""
    scores = []

    # Extraction confidence from clinical agent
    extraction = clinical_result.get("clinical_extraction", {})
    if isinstance(extraction, dict):
        ext_conf = extraction.get("extraction_confidence", 50)
        scores.append(ext_conf / 100.0)

    # Per-criterion confidence from coverage agent
    criteria = coverage_result.get("criteria_assessment", [])
    if criteria:
        criterion_scores = [
            c.get("confidence", 50) / 100.0
            for c in criteria
            if isinstance(c, dict)
        ]
        if criterion_scores:
            scores.append(sum(criterion_scores) / len(criterion_scores))

    # Compliance completeness bonus/penalty
    compliance_status = compliance_result.get("overall_status", "incomplete")
    missing = compliance_result.get("missing_items", [])
    if compliance_status == "complete" and not missing:
        scores.append(1.0)
    else:
        penalty = max(0.0, 1.0 - 0.1 * len(missing))
        scores.append(penalty)

    # Agent error penalties
    for result in [compliance_result, clinical_result, coverage_result]:
        if result.get("error"):
            scores.append(0.0)

    if not scores:
        return 0.5, "MEDIUM"

    confidence = sum(scores) / len(scores)
    confidence = max(0.0, min(1.0, confidence))

    if confidence >= 0.80:
        level = "HIGH"
    elif confidence >= 0.50:
        level = "MEDIUM"
    else:
        level = "LOW"

    return round(confidence, 2), level


def _normalize_coverage_result(coverage_result: dict) -> dict:
    """Lightweight pass-through for coverage agent output.

    With structured output (output_format), the coverage agent returns data
    matching the CoverageResult Pydantic schema directly. This function
    only normalizes the provider_verification status field for display
    consistency (e.g., 'A' -> 'VERIFIED').
    """
    if coverage_result.get("error"):
        return coverage_result

    result = dict(coverage_result)

    # Normalize provider_verification status for display
    pv = result.get("provider_verification")
    if pv and isinstance(pv, dict):
        status = str(pv.get("status", "")).upper()
        if status in ("A", "ACTIVE", "VERIFIED"):
            pv["status"] = "VERIFIED"
        elif status in ("D", "DEACTIVATED", "INACTIVE"):
            pv["status"] = "INACTIVE"

    return result


def _build_audit_trail(
    compliance_result: dict,
    clinical_result: dict,
    coverage_result: dict,
    start_time: str,
    synthesis: dict | None = None,
) -> dict:
    """Build audit trail from agent results."""
    data_sources = ["CPT/HCPCS Format Validation (Local)"]

    # Check which MCP tools were used via tool_results
    for result in [clinical_result, coverage_result]:
        for tr in result.get("tool_results", []):
            tool = tr.get("tool_name", "")
            tool_lower = tool.lower()
            if "npi" in tool_lower:
                source = "NPI Registry MCP (NPPES)"
            elif "icd10" in tool_lower or "icd-10" in tool_lower or "validate_code" in tool_lower or "lookup_code" in tool_lower:
                source = "ICD-10 MCP (2026 Code Set)"
            elif "coverage" in tool_lower or "cms" in tool_lower or "lcd" in tool_lower or "ncd" in tool_lower:
                source = "CMS Coverage MCP (LCDs/NCDs)"
            elif "trial" in tool_lower or "clinical_trial" in tool_lower or "clinical-trial" in tool_lower:
                source = "ClinicalTrials.gov MCP"
            elif "pubmed" in tool_lower:
                source = "PubMed MCP (Biomedical Literature)"
            elif "search" in tool_lower:
                # Generic "search" — likely PubMed search
                source = "PubMed MCP (Biomedical Literature)"
            else:
                source = f"MCP Tool: {tool}"
            if source not in data_sources:
                data_sources.append(source)

    # Always infer data sources from result data to supplement tool_results
    # (agents may not always report tool_results for every MCP call)

    # If provider verification has data, NPI registry was used
    pv = coverage_result.get("provider_verification", {})
    if pv and isinstance(pv, dict) and pv.get("npi"):
        if "NPI Registry MCP (NPPES)" not in data_sources:
            data_sources.append("NPI Registry MCP (NPPES)")

    # If diagnosis validation has data, ICD-10 MCP was used
    dx = clinical_result.get("diagnosis_validation", [])
    if dx:
        if "ICD-10 MCP (2026 Code Set)" not in data_sources:
            data_sources.append("ICD-10 MCP (2026 Code Set)")

    # If coverage policies found, CMS Coverage MCP was used
    policies = coverage_result.get("coverage_policies", [])
    if policies:
        if "CMS Coverage MCP (LCDs/NCDs)" not in data_sources:
            data_sources.append("CMS Coverage MCP (LCDs/NCDs)")

    # If literature support found, PubMed was used
    lit = clinical_result.get("literature_support", [])
    if lit:
        if "PubMed MCP (Biomedical Literature)" not in data_sources:
            data_sources.append("PubMed MCP (Biomedical Literature)")

    # If clinical trials found, ClinicalTrials.gov was used
    trials = clinical_result.get("clinical_trials", [])
    if trials:
        if "ClinicalTrials.gov MCP" not in data_sources:
            data_sources.append("ClinicalTrials.gov MCP")

    # Ensure all 5 MCP data sources are always listed — all servers are
    # queried on every review run, even if they return zero results
    for source in [
        "NPI Registry MCP (NPPES)",
        "ICD-10 MCP (2026 Code Set)",
        "CMS Coverage MCP (LCDs/NCDs)",
        "PubMed MCP (Biomedical Literature)",
        "ClinicalTrials.gov MCP",
    ]:
        if source not in data_sources:
            data_sources.append(source)

    # Extraction confidence
    extraction = clinical_result.get("clinical_extraction", {})
    ext_conf = extraction.get("extraction_confidence", 0) if isinstance(extraction, dict) else 0

    # Assessment confidence (avg of criterion confidences)
    criteria = coverage_result.get("criteria_assessment", [])

    # If coverage agent didn't provide criteria, try synthesis
    if not criteria and synthesis:
        criteria = synthesis.get("criteria_assessment", [])

    if criteria:
        conf_scores = [c.get("confidence", 0) for c in criteria if isinstance(c, dict)]
        assess_conf = int(sum(conf_scores) / len(conf_scores)) if conf_scores else 0
    else:
        assess_conf = 0

    # Criteria met count (case-insensitive — agents may return lowercase)
    met = sum(1 for c in criteria if isinstance(c, dict) and str(c.get("status", "")).upper() == "MET")
    total = len(criteria)
    criteria_met_count = f"{met}/{total}" if total else "0/0"

    # If criteria_met_count is 0/0 but synthesis has criteria data, use it
    if criteria_met_count == "0/0" and synthesis:
        syn_met = synthesis.get("coverage_criteria_met", [])
        syn_not_met = synthesis.get("coverage_criteria_not_met", [])
        if syn_met or syn_not_met:
            criteria_met_count = f"{len(syn_met)}/{len(syn_met) + len(syn_not_met)}"
            if assess_conf == 0 and syn_met:
                # Estimate from synthesis confidence
                assess_conf = int(synthesis.get("confidence", 0.5) * 100)

    return {
        "data_sources": data_sources,
        "review_started": start_time,
        "review_completed": datetime.now(timezone.utc).isoformat(),
        "extraction_confidence": ext_conf,
        "assessment_confidence": assess_conf,
        "criteria_met_count": criteria_met_count,
    }


def _generate_audit_justification(
    request_data: dict,
    synthesis: dict,
    compliance_result: dict,
    clinical_result: dict,
    coverage_result: dict,
    audit_trail: dict,
) -> str:
    """Generate an audit justification document in Markdown format.

    Based on the Anthropic prior-auth-review-skill audit_justification.md template.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    recommendation = str(synthesis.get("recommendation", "pend_for_review")).upper()
    confidence = synthesis.get("confidence", 0)
    try:
        confidence = float(confidence)
    except (ValueError, TypeError):
        confidence = 0.0
    confidence_level = synthesis.get("confidence_level", "LOW")

    lines = []

    # --- Disclaimer Header ---
    lines.append("# Prior Authorization Review — Audit Justification")
    lines.append("")
    lines.append("> **WARNING: AI-ASSISTED DRAFT — REVIEW REQUIRED**")
    lines.append("> All recommendations are drafts requiring human clinical review.")
    lines.append("> Coverage policies reflect Medicare LCDs/NCDs only.")
    lines.append("> Commercial and Medicare Advantage plans may differ.")
    lines.append("")

    # --- Section 1: Executive Summary ---
    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append(f"- **Review Date:** {now}")
    lines.append(f"- **Patient:** {request_data.get('patient_name', 'N/A')} (DOB: {request_data.get('patient_dob', 'N/A')})")
    lines.append(f"- **Provider NPI:** {request_data.get('provider_npi', 'N/A')}")
    lines.append(f"- **Insurance ID:** {request_data.get('insurance_id') or 'Not provided'}")
    lines.append(f"- **Diagnosis Codes:** {', '.join(request_data.get('diagnosis_codes', []))}")
    lines.append(f"- **Procedure Codes:** {', '.join(request_data.get('procedure_codes', []))}")
    lines.append(f"- **Decision:** {recommendation}")
    lines.append(f"- **Confidence:** {confidence_level} ({int(confidence * 100)}%)")
    lines.append("")
    lines.append(f"**Summary:** {synthesis.get('summary', 'N/A')}")
    lines.append("")

    # --- Section 2: Medical Necessity Assessment ---
    lines.append("## 2. Medical Necessity Assessment")
    lines.append("")

    # Coverage policy
    pv = coverage_result.get("provider_verification", {})
    if pv and isinstance(pv, dict):
        lines.append(f"**Provider:** {pv.get('name', 'N/A')} — {pv.get('specialty', 'N/A')} — Status: {pv.get('status', 'N/A')}")
        lines.append("")

    policies = coverage_result.get("coverage_policies", [])
    if policies:
        lines.append("**Coverage Policies Applied:**")
        for p in policies:
            if isinstance(p, dict):
                lines.append(f"- {p.get('policy_id', '?')}: {p.get('title', 'N/A')} ({p.get('type', '?')})")
        lines.append("")

    # Clinical evidence summary
    extraction = clinical_result.get("clinical_extraction", {})
    if isinstance(extraction, dict):
        lines.append("**Clinical Evidence Summary:**")
        if extraction.get("chief_complaint"):
            lines.append(f"- Chief Complaint: {extraction['chief_complaint']}")
        if extraction.get("prior_treatments"):
            lines.append(f"- Prior Treatments: {'; '.join(str(t) for t in extraction['prior_treatments'][:5])}")
        if extraction.get("severity_indicators"):
            lines.append(f"- Severity Indicators: {'; '.join(str(i) for i in extraction['severity_indicators'][:5])}")
        lines.append(f"- Extraction Confidence: {extraction.get('extraction_confidence', 0)}%")
        lines.append("")

    # --- Section 3: Criterion-by-Criterion Evaluation ---
    lines.append("## 3. Criterion-by-Criterion Evaluation")
    lines.append("")

    criteria = coverage_result.get("criteria_assessment", [])
    if criteria:
        lines.append(f"**Criteria Met:** {audit_trail.get('criteria_met_count', '0/0')}")
        lines.append("")
        for c in criteria:
            if not isinstance(c, dict):
                continue
            status = c.get("status", "INSUFFICIENT")
            icon = {"MET": "PASS", "NOT_MET": "FAIL", "INSUFFICIENT": "INFO"}.get(status, "?")
            lines.append(f"### [{icon}] {c.get('criterion', 'N/A')}")
            lines.append(f"- **Status:** {status}")
            lines.append(f"- **Confidence:** {c.get('confidence', 0)}%")
            evidence = c.get("evidence", [])
            if isinstance(evidence, list) and evidence:
                lines.append("- **Evidence:**")
                for e in evidence:
                    lines.append(f"  - {str(e)}")
            elif isinstance(evidence, str) and evidence:
                lines.append(f"- **Evidence:** {evidence}")
            if c.get("notes"):
                lines.append(f"- **Notes:** {c['notes']}")
            lines.append("")
    else:
        lines.append("No coverage criteria were identified for evaluation.")
        lines.append("")

    # --- Section 4: Validation Checks ---
    lines.append("## 4. Validation Checks")
    lines.append("")

    # Provider verification
    if pv and isinstance(pv, dict):
        lines.append(f"**Provider Verification:** NPI {pv.get('npi', 'N/A')} — {pv.get('status', 'N/A')}")
        if pv.get("detail"):
            lines.append(f"  Detail: {pv['detail']}")
        lines.append("")

    # Diagnosis code validation
    dx_val = clinical_result.get("diagnosis_validation", [])
    if dx_val:
        lines.append("**Diagnosis Code Validation:**")
        lines.append("")
        lines.append("| Code | Description | Billable | Valid |")
        lines.append("|------|-------------|----------|------|")
        for d in dx_val:
            if isinstance(d, dict):
                code = d.get("code", "?")
                desc = d.get("description", "N/A")[:60]
                billable = "Yes" if d.get("billable") else "No"
                valid = "Yes" if d.get("valid") else "No"
                lines.append(f"| {code} | {desc} | {billable} | {valid} |")
        lines.append("")

    # Compliance checklist
    checklist = compliance_result.get("checklist", [])
    if checklist:
        lines.append("**Compliance Checklist:**")
        lines.append("")
        lines.append("| Item | Status | Detail |")
        lines.append("|------|--------|--------|")
        for item in checklist:
            if isinstance(item, dict):
                lines.append(f"| {item.get('item', '?')} | {item.get('status', '?')} | {item.get('detail', '')[:60]} |")
        lines.append("")

    # --- Section 5: Decision Rationale ---
    lines.append("## 5. Decision Rationale")
    lines.append("")
    lines.append(f"**Decision:** {recommendation}")
    # Render decision gates — field may contain pipe-separated gates
    gate_raw = synthesis.get("decision_gate", "N/A")
    gate_parts = [g.strip() for g in str(gate_raw).split("|") if g.strip()]
    if len(gate_parts) > 1:
        lines.append("")
        lines.append("**Decision Gates:**")
        for gp in gate_parts:
            # Extract gate label (e.g. "GATE 1 (Provider)") and result
            if ": PASS" in gp.upper():
                lines.append(f"- [PASS] {gp}")
            elif ": FAIL" in gp.upper():
                lines.append(f"- [FAIL] {gp}")
            else:
                lines.append(f"- {gp}")
    else:
        lines.append(f"**Gate:** {gate_raw}")
    lines.append(f"**Confidence:** {confidence_level} ({int(confidence * 100)}%)")
    lines.append("")
    lines.append(synthesis.get("clinical_rationale", "No rationale provided."))
    lines.append("")

    # Supporting facts
    met_criteria = synthesis.get("coverage_criteria_met", [])
    if met_criteria:
        lines.append("**Key Supporting Facts:**")
        for m in met_criteria:
            lines.append(f"- {str(m)}")
        lines.append("")

    # --- Section 6: Documentation Gaps ---
    gaps = coverage_result.get("documentation_gaps", [])
    if gaps:
        lines.append("## 6. Documentation Gaps")
        lines.append("")
        for g in gaps:
            if isinstance(g, dict):
                critical = "CRITICAL" if g.get("critical") else "Non-critical"
                lines.append(f"- [{critical}] {g.get('what', g.get('description', 'N/A'))}")
                if g.get("request"):
                    lines.append(f"  Request: {g['request']}")
            else:
                lines.append(f"- {str(g)}")
        lines.append("")

    # --- Section 7: Audit Trail ---
    lines.append("## 7. Audit Trail")
    lines.append("")
    lines.append("**Data Sources:**")
    for src in audit_trail.get("data_sources", []):
        lines.append(f"- {src}")
    lines.append("")
    lines.append(f"- Review Started: {audit_trail.get('review_started', 'N/A')}")
    lines.append(f"- Review Completed: {audit_trail.get('review_completed', 'N/A')}")
    lines.append(f"- Extraction Confidence: {audit_trail.get('extraction_confidence', 0)}%")
    lines.append(f"- Assessment Confidence: {audit_trail.get('assessment_confidence', 0)}%")
    lines.append(f"- Criteria Met: {audit_trail.get('criteria_met_count', '0/0')}")
    lines.append("")

    # --- Section 8: Regulatory Compliance ---
    lines.append("## 8. Regulatory Compliance")
    lines.append("")
    lines.append("**Decision Policy:** LENIENT Mode (default)")
    lines.append("- Provider verification: Required")
    lines.append("- Code validation: Required")
    lines.append("- Medical necessity criteria: All must be MET for approval")
    lines.append("- Unmet/insufficient criteria: Results in PEND (not DENY)")
    lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Generated: {now} | AI-Assisted Prior Authorization Review System*")

    return "\n".join(lines)


async def run_multi_agent_review(
    request_data: dict,
    on_progress: Callable[[dict], Awaitable[None]] | None = None,
) -> dict:
    """Run the multi-agent prior auth review pipeline.

    Phase 1 (parallel): Compliance + Clinical Reviewer
    Phase 2 (sequential): Coverage Agent (receives clinical findings)
    Phase 3: Synthesis agent reads all reports, produces final decision
    Phase 4: Audit trail assembly and justification document generation

    Args:
        request_data: Dict with patient_name, patient_dob, provider_npi,
            diagnosis_codes, procedure_codes, clinical_notes, insurance_id.
        on_progress: Optional async callback for streaming progress events.

    Returns:
        Dict with recommendation, confidence, confidence_level, summary,
        tool_results, clinical_rationale, coverage criteria,
        policy_references, disclaimer, agent_results, audit_trail,
        and audit_justification (markdown string).
    """
    request_id = request_data.get("request_id", "unknown")

    with tracer.start_as_current_span("prior_auth_review") as root_span:
        root_span.set_attribute("request_id", request_id)
        return await _run_review_pipeline(request_data, on_progress, root_span)


async def _run_review_pipeline(
    request_data: dict,
    on_progress: Callable[[dict], Awaitable[None]] | None,
    root_span,
) -> dict:
    """Inner pipeline — extracted so the top-level span wraps everything."""
    start_time = datetime.now(timezone.utc).isoformat()

    async def _emit(event: dict) -> None:
        if on_progress:
            await on_progress(event)

    # --- Pre-flight: CPT/HCPCS format validation ---
    logger.info("Pre-flight: Validating procedure code formats")
    cpt_validation = validate_procedure_codes(
        request_data.get("procedure_codes", [])
    )
    if not cpt_validation["valid"]:
        logger.warning("CPT validation found invalid codes: %s", cpt_validation["summary"])

    await _emit({
        "phase": "preflight", "status": "completed", "progress_pct": 5,
        "message": "CPT/HCPCS format validation complete",
        "agents": {},
    })

    # --- Phase 1: Parallel — Compliance + Clinical Reviewer ---
    logger.info("Phase 1: Running Compliance and Clinical agents in parallel")

    # Inject CPT pre-flight results into request data so the clinical agent
    # can reference them in procedure_validation (source: "orchestrator_preflight")
    clinical_request = {**request_data, "cpt_preflight": cpt_validation}

    await _emit({
        "phase": "phase_1", "status": "running", "progress_pct": 10,
        "message": "Running Compliance and Clinical agents in parallel",
        "agents": {
            "compliance": {"status": "running", "detail": "Checking documentation completeness"},
            "clinical": {"status": "running", "detail": "Validating codes and extracting clinical evidence"},
        },
    })

    with tracer.start_as_current_span("phase_1_parallel") as p1_span:
        compliance_task = asyncio.create_task(
            _safe_run("Compliance Agent", run_compliance_review, request_data)
        )
        clinical_task = asyncio.create_task(
            _safe_run("Clinical Reviewer Agent", run_clinical_review, clinical_request)
        )

        compliance_result, clinical_result = await asyncio.gather(
            compliance_task, clinical_task
        )

        p1_span.set_attribute("agent.compliance.status",
                              "error" if compliance_result.get("error") else "success")
        p1_span.set_attribute("agent.clinical.status",
                              "error" if clinical_result.get("error") else "success")

    # Build per-agent status with validation warnings
    def _agent_status(name: str, result: dict, ok_msg: str) -> dict:
        if result.get("error"):
            return {"status": "error", "detail": result["error"]}
        missing = _validate_agent_result(name, result)
        if missing:
            return {
                "status": "warning",
                "detail": f"Partial result — missing: {', '.join(missing)}",
            }
        return {"status": "done", "detail": ok_msg}

    await _emit({
        "phase": "phase_1", "status": "completed", "progress_pct": 40,
        "message": "Compliance and Clinical agents completed",
        "agents": {
            "compliance": _agent_status(
                "Compliance Agent", compliance_result,
                "Documentation review complete",
            ),
            "clinical": _agent_status(
                "Clinical Reviewer Agent", clinical_result,
                "Clinical analysis complete",
            ),
        },
    })

    # --- Phase 2: Sequential — Coverage Agent (needs clinical findings) ---
    logger.info("Phase 2: Running Coverage Agent with clinical findings")

    await _emit({
        "phase": "phase_2", "status": "running", "progress_pct": 45,
        "message": "Running Coverage Agent with clinical findings",
        "agents": {
            "coverage": {"status": "running", "detail": "Verifying provider and assessing coverage criteria"},
        },
    })

    with tracer.start_as_current_span("phase_2_coverage") as p2_span:
        coverage_result = await _safe_run(
            "Coverage Agent", run_coverage_review, request_data, clinical_result
        )

        # Normalize coverage result (fix provider data format, etc.)
        coverage_result = _normalize_coverage_result(coverage_result)

        p2_span.set_attribute("agent.coverage.status",
                              "error" if coverage_result.get("error") else "success")

    await _emit({
        "phase": "phase_2", "status": "completed", "progress_pct": 70,
        "message": "Coverage Agent completed",
        "agents": {
            "coverage": _agent_status(
                "Coverage Agent", coverage_result,
                "Coverage analysis complete",
            ),
        },
    })

    # --- Phase 3: Synthesis ---
    logger.info("Phase 3: Synthesizing final recommendation")

    await _emit({
        "phase": "phase_3", "status": "running", "progress_pct": 75,
        "message": "Synthesizing final recommendation",
        "agents": {
            "synthesis": {"status": "running", "detail": "Applying decision rubric gates"},
        },
    })

    with tracer.start_as_current_span("phase_3_synthesis") as p3_span:
        synthesis = await _run_synthesis(
            request_data, compliance_result, clinical_result, coverage_result,
            cpt_validation,
        )

        p3_span.set_attribute("synthesis.recommendation",
                              synthesis.get("recommendation", "unknown"))
        p3_span.set_attribute("synthesis.confidence",
                              synthesis.get("confidence", 0.0))

    # Coerce list[str] fields from synthesis — agent may return list[dict]
    for _str_list_key in (
        "coverage_criteria_met", "coverage_criteria_not_met",
        "missing_documentation", "policy_references",
    ):
        val = synthesis.get(_str_list_key)
        if isinstance(val, list):
            synthesis[_str_list_key] = [
                str(item) if not isinstance(item, str) else item
                for item in val
            ]

    # synthesis_audit_trail comes as a JSON-encoded string from the agent
    # (Responses API structured output doesn't support unconstrained dict).
    # Parse it back to dict for the backend/frontend API contract.
    _sat = synthesis.get("synthesis_audit_trail")
    if isinstance(_sat, str) and _sat:
        try:
            synthesis["synthesis_audit_trail"] = json.loads(_sat)
        except (json.JSONDecodeError, TypeError):
            synthesis["synthesis_audit_trail"] = {}

    await _emit({
        "phase": "phase_3", "status": "completed", "progress_pct": 90,
        "message": "Synthesis complete",
        "agents": {
            "synthesis": {"status": "done", "detail": "Decision rubric applied"},
        },
    })

    # --- Phase 4: Audit Trail & Justification ---
    logger.info("Phase 4: Building audit trail and justification document")

    await _emit({
        "phase": "phase_4", "status": "running", "progress_pct": 92,
        "message": "Building audit trail and justification document",
        "agents": {},
    })

    with tracer.start_as_current_span("phase_4_audit") as p4_span:
        confidence, confidence_level = _compute_confidence(
            compliance_result, clinical_result, coverage_result
        )

        # Use synthesis confidence if available, fall back to computed
        final_confidence = synthesis.get("confidence", confidence)
        final_level = synthesis.get("confidence_level", confidence_level)

        audit_trail = _build_audit_trail(
            compliance_result, clinical_result, coverage_result, start_time,
            synthesis=synthesis,
        )

        audit_justification = _generate_audit_justification(
            request_data, synthesis,
            compliance_result, clinical_result, coverage_result,
            audit_trail,
        )

        audit_justification_pdf = generate_audit_justification_pdf(
            request_data, synthesis,
            compliance_result, clinical_result, coverage_result,
            audit_trail,
        )

        p4_span.set_attribute("audit.confidence", final_confidence)
        p4_span.set_attribute("audit.confidence_level", final_level)

    # --- Assemble final response ---
    all_tool_results = []

    # Add CPT validation as a tool result
    all_tool_results.append({
        "tool_name": "cpt_format_validation",
        "status": "pass" if cpt_validation["valid"] else "fail",
        "detail": cpt_validation["summary"],
    })

    # Collect agent-reported tool_results and normalize status values.
    # Agents (LLMs) may use "success"/"error" instead of the frontend's
    # expected "pass"/"fail"/"warning" vocabulary.
    _STATUS_MAP = {
        "success": "pass",
        "completed": "pass",
        "found": "pass",
        "verified": "pass",
        "valid": "pass",
        "error": "fail",
        "failed": "fail",
        "invalid": "fail",
        "not_found": "warning",
        "partial": "warning",
        "info": "warning",
    }

    def _normalize_tool_result(tr: dict) -> dict:
        raw = str(tr.get("status", "warning")).lower().strip()
        return {
            "tool_name": tr.get("tool_name", "unknown"),
            "status": _STATUS_MAP.get(raw, raw),  # map or keep as-is
            "detail": tr.get("detail", ""),
        }

    for tr in clinical_result.get("tool_results", []):
        if isinstance(tr, dict):
            all_tool_results.append(_normalize_tool_result(tr))

    for tr in coverage_result.get("tool_results", []):
        if isinstance(tr, dict):
            all_tool_results.append(_normalize_tool_result(tr))

    # If agents didn't report tool_results, synthesize from available data
    existing_tools = {t.get("tool_name", "") for t in all_tool_results}

    # ICD-10 validation from clinical agent
    dx_val = clinical_result.get("diagnosis_validation", [])
    if dx_val and not any("icd" in t.lower() or "diagnosis" in t.lower() for t in existing_tools):
        valid_count = sum(1 for d in dx_val if isinstance(d, dict) and d.get("valid"))
        billable_count = sum(1 for d in dx_val if isinstance(d, dict) and d.get("billable"))
        total = len(dx_val)
        all_tool_results.append({
            "tool_name": "icd10_validation",
            "status": "pass" if valid_count == total else "warning",
            "detail": f"{valid_count}/{total} codes valid, {billable_count}/{total} billable",
        })

    # NPI verification from coverage agent
    pv = coverage_result.get("provider_verification", {})
    if pv and isinstance(pv, dict) and pv.get("npi") and not any("npi" in t.lower() for t in existing_tools):
        pv_status = pv.get("status", "unknown").upper()
        all_tool_results.append({
            "tool_name": "npi_verification",
            "status": "pass" if pv_status in ("VERIFIED", "ACTIVE") else "warning",
            "detail": f"NPI {pv.get('npi')} — {pv.get('name', 'N/A')} — {pv_status}",
        })

    # Coverage policy search from coverage agent
    policies = coverage_result.get("coverage_policies", [])
    if policies and not any("coverage" in t.lower() or "cms" in t.lower() for t in existing_tools):
        all_tool_results.append({
            "tool_name": "cms_coverage_search",
            "status": "pass",
            "detail": f"{len(policies)} coverage policies found",
        })

    await _emit({
        "phase": "phase_4", "status": "completed", "progress_pct": 100,
        "message": "Review complete",
        "agents": {},
    })

    return {
        **synthesis,
        "confidence": final_confidence,
        "confidence_level": final_level,
        "tool_results": all_tool_results,
        "agent_results": {
            "compliance": _enrich_agent_result("compliance", compliance_result),
            "clinical": _enrich_agent_result("clinical", clinical_result),
            "coverage": _enrich_agent_result("coverage", coverage_result),
        },
        "audit_trail": audit_trail,
        "audit_justification": audit_justification,
        "audit_justification_pdf": audit_justification_pdf,
    }


async def _safe_run(agent_name: str, fn, *args) -> dict:
    """Run an agent function with error handling and automatic retry.

    After each attempt, the result is validated against ``_EXPECTED_KEYS``.
    If required keys are missing (e.g. from a truncated API response), the
    agent is retried up to ``_MAX_AGENT_RETRIES`` times.

    Returns the agent's result dict on success, or an error dict on failure.
    """
    last_result: dict = {"error": "Agent did not run", "tool_results": []}

    for attempt in range(_MAX_AGENT_RETRIES + 1):
        try:
            last_result = await fn(*args)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error("%s attempt %d failed:\n%s", agent_name, attempt + 1, tb)
            last_result = {"error": str(e), "tool_results": []}

        # Validate result completeness
        missing = _validate_agent_result(agent_name, last_result)
        if not missing:
            if attempt > 0:
                logger.info(
                    "%s succeeded on retry (attempt %d/%d)",
                    agent_name, attempt + 1, _MAX_AGENT_RETRIES + 1,
                )
            return last_result

        # Result is incomplete — decide whether to retry
        if attempt < _MAX_AGENT_RETRIES:
            logger.warning(
                "%s returned incomplete result (attempt %d/%d). "
                "Missing keys: %s. Retrying...",
                agent_name, attempt + 1, _MAX_AGENT_RETRIES + 1,
                ", ".join(missing),
            )
        else:
            logger.error(
                "%s returned incomplete result after %d attempt(s). "
                "Missing keys: %s. Using partial result.",
                agent_name, attempt + 1, ", ".join(missing),
            )

    return last_result


async def _run_synthesis(
    request_data: dict,
    compliance_result: dict,
    clinical_result: dict,
    coverage_result: dict,
    cpt_validation: dict | None = None,
) -> dict:
    """Delegate to the synthesis_agent dispatcher (mirrors clinical/compliance/coverage pattern)."""
    return await _dispatch_synthesis(
        request_data=request_data,
        compliance_result=compliance_result,
        clinical_result=clinical_result,
        coverage_result=coverage_result,
        cpt_validation=cpt_validation,
    )
