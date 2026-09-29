"""A stand-in for the Lyzr services, shaped after the live APIs, shared by the Lyzr, CLI and doctor tests."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from agents.contract.automata_pipeline import TemplateDraftingModel
from agents.lyzr.settings import LyzrSettings

BRIEF_RE = re.compile(r"BRIEF:\n(.*)", re.DOTALL)
INJECTION_RE = re.compile(r"ignore (all )?previous instructions", re.IGNORECASE)
LEAK_RE = re.compile(r"(budget allows )\d+(?:\.\d+)?", re.IGNORECASE)
AGENT_KEYS = ("buyer", "supplier", "drafter", "reviewer", "summary", "audit")


def settings(**overrides: Any) -> LyzrSettings:
    base = dict(lyzr_api_key="test-key", lyzr_user_id="qa@hidevs.test", lyzr_max_retries=2,
                lyzr_buyer_agent_id="agent-buyer", lyzr_supplier_agent_id="agent-supplier",
                lyzr_drafter_agent_id="agent-drafter", lyzr_reviewer_agent_id="agent-reviewer",
                lyzr_summary_agent_id="agent-summary", lyzr_audit_agent_id="agent-audit",
                lyzr_rai_policy_id="rai-1", lyzr_rai_buyer_policy_id="rai-b", lyzr_rai_supplier_policy_id="rai-s",
                aims_mode="event_log")
    base.update(overrides)
    return LyzrSettings(_env_file=None, **base)


def _rego_value(rego: str, name: str, default: Any) -> Any:
    match = re.search(rf"^{name} := (.+)$", rego, re.MULTILINE)
    return json.loads(match.group(1)) if match else default


def opa_decision(rego: str, offer: dict[str, Any]) -> str | None:
    """What the compiled guardrail's deny rules say about an offer: None when allowed, else the first reason."""
    for key in _rego_value(rego, "required_issues", []):
        value = offer.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"SCHEMA: {key} missing or not numeric"
    checks = (("mandate_max", lambda v, b: v > b, "POLICY-MANDATE"), ("mandate_min", lambda v, b: v < b, "POLICY-MANDATE"),
              ("legal_max", lambda v, b: v > b, "LEGAL"), ("legal_min", lambda v, b: v < b, "LEGAL"))
    for name, breaks, rule in checks:
        for key, bound in _rego_value(rego, name, {}).items():
            if breaks(offer[key], bound):
                return f"{rule}: {key}"
    budget, quantity = _rego_value(rego, "budget_cap", None), _rego_value(rego, "quantity", None)
    if budget is not None and quantity is not None and offer.get("unit_price", 0) * quantity > budget:
        return "POLICY-BUDGET: contract value exceeds the CFO budget cap"
    return None


class FakeLyzr:
    """Agents follow the policy engine's recommended move; artefacts are stored and listable; anchor messages land
    in readable sessions; RAI blocks prompt injection and redacts budget leaks; OPA evaluates each offer against
    the compiled guardrail it was registered with."""

    def __init__(self, rai_blocks: bool = False, opa_denies: bool = False, fail_chat: int = 0,
                 provisioned: bool = False):
        self.calls: list[tuple[str, str, dict]] = []
        self.rai_blocks = rai_blocks
        self.opa_denies = opa_denies
        self.fail_chat = fail_chat
        self.created = 0
        self.agents: list[dict] = ([{"_id": f"agent-{k}", "name": k} for k in AGENT_KEYS] if provisioned else [])
        self.rai_policies: list[dict] = []
        self.opa_policies: list[dict] = []
        self.sessions: dict[str, list[dict]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path, method = request.url.path, request.method
        self.calls.append((method, path, body))
        assert request.headers["x-api-key"] == "test-key"
        if path == "/v3/inference/chat/":
            return self._chat(body)
        if path.startswith("/log/"):
            return httpx.Response(200, json={"status": "success"})
        if path.startswith("/v3/sessions/") and path.endswith("/messages"):
            sid = path.split("/")[3]
            if sid not in self.sessions:
                return httpx.Response(404, json={"detail": "Session not found"})
            return httpx.Response(200, json={"messages": self.sessions[sid]})
        if path == "/v3/agents/":
            if method == "GET":
                return httpx.Response(200, json=self.agents)
            self.created += 1
            self.agents.append({"_id": f"created-{self.created}", "name": body["name"]})
            return httpx.Response(200, json={"agent_id": f"created-{self.created}"})
        if path.startswith("/v3/agents/") and method == "PUT":
            return httpx.Response(200, json={"message": "Agent updated successfully."})
        if path == "/v1/rai/inference":
            return self._rai(body["input_text"])
        if path == "/v1/guardrails/evaluate-tool-call":
            return self._opa(body)
        for prefix, store, id_prefix in (("/v1/rai/policies", self.rai_policies, "rai"),
                                         ("/v1/opa-policies", self.opa_policies, "opa")):
            if path == prefix and method == "GET":
                return httpx.Response(200, json=store)
            if path == prefix:
                item = {"_id": f"{id_prefix}-{len(store) + 1}", **body}
                store.append(item)
                return httpx.Response(200, json=item)
            if path.startswith(prefix + "/") and method == "PUT":
                return httpx.Response(200, json={"_id": path.rsplit("/", 1)[1], **body})
        return httpx.Response(404, json={"detail": "not mocked"})

    def _chat(self, body: dict) -> httpx.Response:
        if self.fail_chat:
            self.fail_chat -= 1
            return httpx.Response(503, json={"detail": "busy"})
        message = body["message"]
        if "[TASK:" in message:
            return httpx.Response(200, json={"response": TemplateDraftingModel().generate_text(prompt=message)})
        if message.startswith(("AUDIT_EVENT", "AUDIT_ANCHOR")):
            msgs = self.sessions.setdefault(body["session_id"], [])
            msgs.append({"_id": f"m{len(msgs)}", "role": "user", "content": message, "created_at": "2026-09-24T00:00:00"})
            msgs.append({"_id": f"m{len(msgs)}", "role": "assistant", "content": "ACK"})
            return httpx.Response(200, json={"response": "ACK"})
        brief = json.loads(BRIEF_RE.search(message).group(1))
        move = brief["recommended_move"]
        reply = {"action": move["action"], "offer": move["offer"] or {},
                 "message": f"[{brief['your_role']} via Lyzr] Let's keep moving towards a deal."}
        return httpx.Response(200, json={"response": f"```json\n{json.dumps(reply)}\n```", "module_outputs": {}})

    def _rai(self, text: str) -> httpx.Response:
        if self.rai_blocks or INJECTION_RE.search(text):
            return httpx.Response(200, json={"input_text": text, "processed_text": "", "blocked": True,
                                             "block_reason": "prompt_injection"})
        processed = LEAK_RE.sub(r"\1[confidential]", text).replace("Lyzr", "L***")
        return httpx.Response(200, json={"input_text": text, "processed_text": processed, "blocked": False})

    def _opa(self, body: dict) -> httpx.Response:
        ids = [p["policy_id"] for p in body["opa_guardrail"]["managed_policies"]]
        policies = {p["_id"]: p for p in self.opa_policies}
        if not all(pid in policies for pid in ids):
            return httpx.Response(404, json={"detail": "OPA policy not found"})
        if self.opa_denies:
            return httpx.Response(200, json={"allowed": False, "denied_by": "opa", "reason": "mandate breach"})
        offer = body["tool_args"]["offer"]
        for pid in ids:
            reason = opa_decision(policies[pid]["rego_content"], offer)
            if reason:
                return httpx.Response(200, json={"allowed": False, "denied_by": pid, "reason": reason})
        return httpx.Response(200, json={"allowed": True})


def paths(fake: FakeLyzr, prefix: str) -> list[dict]:
    return [b for _, p, b in fake.calls if p.startswith(prefix)]
