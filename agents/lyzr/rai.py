"""Client for Lyzr Safe AI / Responsible AI (https://rai-prod.studio.lyzr.ai).

Endpoints used (from the published OpenAPI spec):

* ``POST /v1/rai/policies``                  - create an RAI policy (keywords, toxicity, prompt injection, PII, ...)
* ``POST /v1/rai/inference``                 - screen text through a policy -> ``blocked`` / ``processed_text``
* ``POST /v1/opa-policies``                  - register a Rego policy (our compiled procurement guardrail)
* ``POST /v1/guardrails/evaluate-tool-call`` - evaluate a ``submit_offer`` tool call against OPA policies
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from .client import LyzrError, agent_id_from_response
from .settings import LyzrSettings

if TYPE_CHECKING:
    from ..guardrails.rego import Guardrail

log = logging.getLogger(__name__)


def artefact_id(item: dict[str, Any]) -> str | None:
    return agent_id_from_response(item) or (str(item["policy_id"]) if item.get("policy_id") else None)


def ids_by_name(items: list[dict[str, Any]]) -> dict[str, str]:
    return {str(i.get("name")): artefact_id(i) for i in items if i.get("name") and artefact_id(i)}  # type: ignore[misc]


@dataclass
class RAIResult:
    processed_text: str
    blocked: bool = False
    block_reason: str | None = None
    detections: dict[str, Any] = field(default_factory=dict)
    warnings: list[Any] = field(default_factory=list)


@dataclass
class ToolCallDecision:
    allowed: bool
    denied_by: str | None = None
    reason: str | None = None


class LyzrRAIClient:
    def __init__(self, settings: LyzrSettings, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.lyzr_api_key:
            raise LyzrError("LYZR_API_KEY is not configured")
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.lyzr_rai_base_url,
            headers={"x-api-key": settings.lyzr_api_key, "Content-Type": "application/json"},
            timeout=settings.lyzr_timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.TransportError as exc:
            raise LyzrError(f"POST {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise LyzrError(f"POST {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def _get(self, url: str) -> Any:
        try:
            resp = await self._client.get(url)
        except httpx.TransportError as exc:
            raise LyzrError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise LyzrError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("policies", "items", "data", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    async def list_policies(self) -> list[dict[str, Any]]:
        return self._items(await self._get("/v1/rai/policies"))

    async def list_opa_policies(self) -> list[dict[str, Any]]:
        return self._items(await self._get("/v1/opa-policies"))

    async def create_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/rai/policies", payload)

    async def create_opa_policy(self, name: str, rego: str, description: str = "") -> dict[str, Any]:
        return await self._post("/v1/opa-policies", {"name": name, "description": description, "rego_content": rego})

    async def _put(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.put(url, json=payload)
        except httpx.TransportError as exc:
            raise LyzrError(f"PUT {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise LyzrError(f"PUT {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    async def update_opa_policy(self, policy_id: str, name: str, rego: str, description: str = "") -> dict[str, Any]:
        return await self._put(f"/v1/opa-policies/{policy_id}",
                               {"name": name, "description": description, "rego_content": rego})

    async def update_policy(self, policy_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._put(f"/v1/rai/policies/{policy_id}", payload)

    async def inference(self, policy_id: str, text: str, stage: str = "llm_output",
                        agent_id: str | None = None, session_id: str | None = None) -> RAIResult:
        data = await self._post("/v1/rai/inference", {
            "policy_id": policy_id, "input_text": text, "stage": stage,
            "agent_id": agent_id, "session_id": session_id,
        })
        return RAIResult(
            processed_text=data.get("processed_text", text),
            blocked=bool(data.get("blocked", False)),
            block_reason=data.get("block_reason"),
            detections=data.get("detections") or {},
            warnings=data.get("warnings") or [],
        )

    async def evaluate_tool_call(self, tool_name: str, tool_args: dict[str, Any], principal_id: str,
                                 opa_policy_ids: list[str], mode: str = "enforce") -> ToolCallDecision:
        data = await self._post("/v1/guardrails/evaluate-tool-call", {
            "tool_name": tool_name,
            "tool_args": tool_args,
            "principal_id": principal_id,
            "opa_guardrail": {
                "enabled": True,
                "source": "managed",
                # hooks only route llm_input/llm_output text screening; tool-call evaluation ignores them
                "managed_policies": [{"policy_id": pid} for pid in opa_policy_ids],
                "mode": mode,
                "timeout_seconds": 5.0,
            },
        })
        return ToolCallDecision(bool(data.get("allowed", False)), data.get("denied_by"), data.get("reason"))


class SafeAIGateway:
    """What the Legal Arbiter calls on top of its local rules. Local checks stay authoritative (fail-closed);
    this external layer can only add restrictions, and its outages are recorded rather than fatal."""

    def __init__(self, rai: LyzrRAIClient, settings: LyzrSettings):
        self.rai = rai
        self.settings = settings
        self._guardrail_ids: dict[str, str] = {}
        self._guardrails_listed = False
        self._guardrail_lock = asyncio.Lock()

    @property
    def screens_text(self) -> bool:
        return self.settings.any_rai_policy

    async def guardrail_id(self, guardrail: Guardrail) -> str:
        """Lyzr policy id for a content-addressed guardrail: reused by name, registered on first use."""
        async with self._guardrail_lock:
            if guardrail.name not in self._guardrail_ids:
                if not self._guardrails_listed:
                    self._guardrail_ids.update(ids_by_name(await self.rai.list_opa_policies()))
                    self._guardrails_listed = True
            if guardrail.name not in self._guardrail_ids:
                created = await self.rai.create_opa_policy(guardrail.name, guardrail.rego, guardrail.description)
                policy_id = artefact_id(created)
                if not policy_id:
                    raise LyzrError(f"OPA policy created without an id: {str(created)[:200]}")
                log.info("registered Lyzr OPA guardrail %s -> %s", guardrail.name, policy_id)
                self._guardrail_ids[guardrail.name] = policy_id
            return self._guardrail_ids[guardrail.name]

    async def screen_message(self, text: str, role: str, session_id: str) -> tuple[str, dict[str, Any]]:
        policy_id = self.settings.rai_policy_id(role)
        if not policy_id or not text:
            return text, {}
        try:
            # Screened as the counterpart's *input*: verified live, Lyzr only runs PII anonymisation at the
            # llm_input stage (keywords, injection and toxicity run at both).
            result = await self.rai.inference(policy_id, text, stage="llm_input",
                                              agent_id=self.settings.agent_id(role), session_id=session_id)
        except LyzrError as exc:
            log.warning("Lyzr RAI unavailable: %s", exc)
            return text, {"lyzr_rai": "unavailable", "error": str(exc)[:200]}
        details = {"lyzr_rai": "blocked" if result.blocked else "passed", "rai_policy": policy_id,
                   "detections": result.detections}
        if result.blocked:
            return "[withheld by Lyzr Safe AI policy]", {**details, "reason": result.block_reason}
        return result.processed_text or text, details

    async def screen_offer(self, role: str, offer: dict[str, float], principal_id: str,
                           guardrail: Guardrail | None) -> tuple[bool, dict[str, Any]]:
        """Evaluate the offer as a ``submit_offer`` tool call against the guardrail of the sender's own envelope."""
        if guardrail is None or not self.settings.lyzr_opa_guardrails:
            return True, {}
        try:
            policy_id = await self.guardrail_id(guardrail)
            decision = await self.rai.evaluate_tool_call("submit_offer", {"role": role, "offer": offer},
                                                         principal_id, [policy_id])
        except LyzrError as exc:
            log.warning("Lyzr OPA guardrail unavailable: %s", exc)
            return True, {"lyzr_opa": "unavailable", "error": str(exc)[:200]}
        return decision.allowed, {"lyzr_opa": "allowed" if decision.allowed else "denied", "opa_policy": policy_id,
                                  "guardrail": guardrail.name, "denied_by": decision.denied_by,
                                  "reason": decision.reason}
