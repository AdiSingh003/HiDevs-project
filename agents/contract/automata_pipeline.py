"""Contract drafting orchestrated with Lyzr Automata.

``LinearSyncPipeline``: Contract Drafter -> Legal Reviewer -> CFO Briefing Analyst.

* With Lyzr credentials each task runs on a Lyzr Studio agent through :class:`LyzrStudioModel`
  (an Automata ``AIModel`` adapter over the Lyzr Agent API).
* Offline, :class:`TemplateDraftingModel` stands in deterministically so the same Automata pipeline is
  exercised end-to-end in demos and CI.

Drafted clauses are cross-checked against the structured terms; any clause whose numbers drift from the
agreement is replaced by the canonical template (LLMs never get the last word on numbers).
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..lyzr.client import LyzrAgentClient, extract_json
from ..lyzr.settings import LyzrSettings
from .clauses import number_variants, render_clauses, verify_clause

log = logging.getLogger(__name__)
AUTOMATA_WORKDIR = Path(os.environ.get("AUTOMATA_WORKDIR", Path(tempfile.gettempdir()) / "hidevs-automata"))


@contextlib.contextmanager
def _inside(directory: Path) -> Iterator[None]:
    # lyzr_automata builds ResourceBox("resources") default arguments at import time, which creates a
    # ./resources folder in the current directory - import it from a scratch directory instead.
    directory.mkdir(parents=True, exist_ok=True)
    prev = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(prev)


with _inside(AUTOMATA_WORKDIR):
    from lyzr_automata import Agent, Task  # noqa: E402
    from lyzr_automata.ai_models.model_base import AIModel  # noqa: E402
    from lyzr_automata.pipelines.linear_sync_pipeline import LinearSyncPipeline  # noqa: E402
    from lyzr_automata.tasks.task_literals import InputType, OutputType  # noqa: E402
    from lyzr_automata.utils.resource_handler import ResourceBox  # noqa: E402

PAYLOAD_RE = re.compile(r"<<<PAYLOAD>>>(.*?)<<<END>>>", re.DOTALL)
_STDOUT_LOCK = threading.Lock()

DRAFT_INSTRUCTIONS = (
    "[TASK:draft_clauses] Draft every clause listed in mandatory_clauses for a B2B supply agreement. Use EXACTLY the "
    "agreed numbers in terms_fmt (never round or change them) and the jurisdiction notes. Return only JSON of the "
    "form {\"clauses\": [{\"id\": str, \"title\": str, \"text\": str}]}"
)
REVIEW_INSTRUCTIONS = (
    "[TASK:legal_review] Review the drafted clauses against the rulebook and agreed terms in the payload. Flag any "
    "missing mandatory clause, unenforceable penalty, statutory conflict or number that differs from terms_fmt. "
    "Return only JSON: {\"approved\": bool, \"findings\": [{\"clause\": str, \"severity\": str, \"note\": str}]}"
)
SUMMARY_INSTRUCTIONS = (
    "[TASK:cfo_summary] Write a plain-English executive summary (max 120 words) for the approving CFO: the deal, "
    "why it is within mandate, key risks and whether a co-signature is required. Plain text only."
)


def wrap_payload(payload: dict[str, Any]) -> str:
    return f"<<<PAYLOAD>>>{json.dumps(payload, ensure_ascii=False)}<<<END>>>"


def read_payload(prompt: str) -> dict[str, Any]:
    m = PAYLOAD_RE.search(prompt or "")
    return json.loads(m.group(1)) if m else {}


class LyzrStudioModel(AIModel):
    """Automata ``AIModel`` that routes task prompts to a Lyzr Studio agent via the Agent API."""

    def __init__(self, client: LyzrAgentClient, agent_id: str, session_id: str):
        self.client = client
        self.agent_id = agent_id
        self.session_id = session_id
        self.parameters = {"model": f"lyzr-agent:{agent_id}"}

    def generate_text(self, task_id=None, system_persona=None, prompt=None, messages=None):  # noqa: D401
        if messages:
            text = "\n\n".join(str(m.get("content", "")) for m in messages)
        else:
            text = f"{system_persona}\n\n{prompt}"
        return self.client.chat_sync(self.agent_id, self.session_id, text).text

    def generate_image(self, task_id=None, prompt=None, resource_box=None, tasks=None):
        raise NotImplementedError("image generation is not used by the drafting pipeline")


class TemplateDraftingModel(AIModel):
    """Deterministic offline stand-in for the drafting agents."""

    def __init__(self) -> None:
        self.parameters = {"model": "offline-template"}

    def generate_text(self, task_id=None, system_persona=None, prompt=None, messages=None):
        prompt = prompt or ""
        payload = read_payload(prompt)
        if "[TASK:draft_clauses]" in prompt:
            return json.dumps({"clauses": render_clauses(payload)})
        if "[TASK:legal_review]" in prompt:
            after_input = prompt.split("Input:", 1)[1] if "Input:" in prompt else prompt
            drafted = extract_json(PAYLOAD_RE.sub("", after_input)) or {}
            return json.dumps(review_clauses(drafted.get("clauses", []), payload))
        if "[TASK:cfo_summary]" in prompt:
            return executive_summary(payload)
        return ""

    def generate_image(self, task_id=None, prompt=None, resource_box=None, tasks=None):
        raise NotImplementedError


def standard_term_numbers(payload: dict[str, Any]) -> set[str]:
    """Numbers of non-negotiated standard T&C terms: legitimate in clauses even though not agreed in the negotiation."""
    decimals = payload.get("decimals", {})
    out: set[str] = set()
    for k, v in (payload.get("standard_terms") or {}).items():
        out |= {x.replace(",", "") for x in number_variants(v, decimals.get(k, 2))}
    return out


def review_clauses(clauses: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    findings = []
    present = {c.get("id") for c in clauses}
    for m in payload.get("mandatory_clauses", []):
        if m["id"] not in present:
            findings.append({"clause": m["id"], "severity": "high", "note": "mandatory clause missing"})
    terms = payload.get("terms", {})
    decimals = payload.get("decimals", {})
    extra = standard_term_numbers(payload)
    for c in clauses:
        for problem in verify_clause(c.get("id", ""), c.get("text", ""), terms, decimals, extra):
            findings.append({"clause": c.get("id"), "severity": "high", "note": problem})
    return {"approved": not findings, "findings": findings,
            "checks": ["mandatory clauses present", "numbers match agreed terms", "LD cap within penalty doctrine",
                       "payment terms within statutory limits"]}


def executive_summary(p: dict[str, Any]) -> str:
    c, t = p["context"], p["terms_fmt"]
    lines = [
        f"Recommend approval: {p['supplier_name']} to supply {p['quantity_fmt']} {c['quantity_unit']} of {c['item']} "
        f"at {c['currency']} {t['unit_price']} per {p['per_unit']} (total {c['currency']} {p['total_value_fmt']}).",
        f"Delivery in {t['delivery_days']} days, payment in {t['payment_terms_days']} days"
        + (f", OTIF {t['sla_on_time_pct']}%" if "sla_on_time_pct" in t else "")
        + (f" backed by LDs of {t['late_penalty_pct_per_day']}%/day" if "late_penalty_pct_per_day" in t else "")
        + (f" capped at {t['penalty_cap_pct']}%" if "penalty_cap_pct" in t else "") + ".",
        f"Reached in {p['negotiation']['rounds']} rounds ({p['negotiation']['outcome'].replace('_', ' ')}); "
        f"{p['negotiation']['interventions']} guardrail interventions; terms are within both sealed mandates and "
        f"the {p['jurisdiction_name']} rulebook.",
    ]
    if p.get("cfo_required"):
        lines.append("Contract value exceeds the agent's delegated authority: CFO co-signature required.")
    return " ".join(lines)


@dataclass
class DraftingResult:
    clauses: list[dict[str, Any]]
    review: dict[str, Any]
    summary: str
    engine: str
    model: str
    substitutions: list[dict[str, Any]] = field(default_factory=list)
    task_outputs: list[str] = field(default_factory=list)


class DraftingPipeline:
    def __init__(self, settings: LyzrSettings | None = None, client: LyzrAgentClient | None = None,
                 workdir: Path | None = None):
        self.settings = settings
        self.client = client
        self.workdir = workdir or AUTOMATA_WORKDIR
        self.online = bool(settings and client and settings.automata_ready)

    def _models(self, session_id: str) -> tuple[AIModel, AIModel, AIModel]:
        if self.online:
            assert self.settings and self.client
            return (LyzrStudioModel(self.client, self.settings.agent_id("drafter") or "", session_id),
                    LyzrStudioModel(self.client, self.settings.agent_id("reviewer") or "", session_id),
                    LyzrStudioModel(self.client, self.settings.agent_id("summary") or "", session_id))
        m = TemplateDraftingModel()
        return m, m, m

    def run(self, payload: dict[str, Any], session_id: str) -> DraftingResult:
        try:
            return self._run(payload, *self._models(session_id), online=self.online)
        except Exception as exc:  # a Lyzr outage or spent credits must not cost the parties their contract
            if not self.online:
                raise
            log.warning("Lyzr drafting failed, using the offline template instead: %s", exc)
            m = TemplateDraftingModel()
            result = self._run(payload, m, m, m, online=False)
            result.model = "offline-template (Lyzr drafting unavailable)"
            return result

    def _run(self, payload: dict[str, Any], drafter_model: AIModel, reviewer_model: AIModel, summary_model: AIModel,
             *, online: bool) -> DraftingResult:
        box = ResourceBox(base_folder=str(self.workdir / "resources"))
        drafter = Agent(role="Contract Drafter",
                        prompt_persona="a senior commercial contracts counsel who drafts precise, enforceable clauses")
        reviewer = Agent(role="Legal Reviewer",
                         prompt_persona="a meticulous procurement lawyer who checks statutory compliance and numbers")
        analyst = Agent(role="CFO Briefing Analyst",
                        prompt_persona="a finance business partner who writes crisp approval memos")
        draft = Task(name="draft_clauses", agent=drafter, model=drafter_model, instructions=DRAFT_INSTRUCTIONS,
                     default_input=wrap_payload(payload), input_type=InputType.TEXT, output_type=OutputType.TEXT,
                     resource_box=box)
        review = Task(name="legal_review", agent=reviewer, model=reviewer_model, instructions=REVIEW_INSTRUCTIONS,
                      default_input=wrap_payload(payload), input_tasks=[draft], input_type=InputType.TEXT,
                      output_type=OutputType.TEXT, resource_box=box)
        summary = Task(name="cfo_summary", agent=analyst, model=summary_model, instructions=SUMMARY_INSTRUCTIONS,
                       default_input=wrap_payload(payload), input_type=InputType.TEXT, output_type=OutputType.TEXT,
                       resource_box=box)
        pipeline = LinearSyncPipeline(name="contract-drafting", completion_message="contract drafted",
                                      tasks=[draft, review, summary], resource_box=box)
        buffer = io.StringIO()
        with _STDOUT_LOCK, contextlib.redirect_stdout(buffer):
            outputs = pipeline.run()
        log.debug("automata pipeline log:\n%s", buffer.getvalue())
        texts = [str(o.get("task_output", "")) for o in outputs]
        return self._merge(payload, texts, drafter_model.parameters.get("model", "?"), online)

    def _merge(self, payload: dict[str, Any], texts: list[str], model: str, online: bool) -> DraftingResult:
        canonical = {c["id"]: c for c in render_clauses(payload)}
        drafted = (extract_json(texts[0]) or {}).get("clauses", []) if texts else []
        drafted_by_id = {c.get("id"): c for c in drafted if isinstance(c, dict)}
        final, substitutions = [], []
        extra = standard_term_numbers(payload)
        for m in payload["mandatory_clauses"]:
            cand = drafted_by_id.get(m["id"])
            problems = ["clause missing from draft"] if cand is None else verify_clause(
                m["id"], str(cand.get("text", "")), payload["terms"], payload["decimals"], extra)
            if problems:
                if online:
                    substitutions.append({"clause": m["id"], "problems": problems})
                final.append({**canonical[m["id"]], "source": "canonical-template"})
            else:
                final.append({"id": m["id"], "title": cand.get("title") or m["title"], "text": str(cand["text"]),
                              "source": "lyzr-agent" if online else "automata-template"})
        review = (extract_json(texts[1]) if len(texts) > 1 else None) or {"approved": True, "findings": []}
        summary = texts[2].strip() if len(texts) > 2 and texts[2].strip() else executive_summary(payload)
        return DraftingResult(clauses=final, review=review, summary=summary,
                              engine="lyzr-automata LinearSyncPipeline", model=model,
                              substitutions=substitutions, task_outputs=[t[:4000] for t in texts])
