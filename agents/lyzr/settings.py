"""Lyzr configuration, read from the environment / a local ``.env`` file."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LyzrSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    lyzr_api_key: str | None = Field(None, description="Lyzr Studio API key (x-api-key)")
    lyzr_user_id: str = "negotiator@hidevs.local"
    lyzr_agent_base_url: str = "https://agent-prod.studio.lyzr.ai"
    lyzr_rai_base_url: str = "https://rai-prod.studio.lyzr.ai"
    lyzr_timeout_s: float = 45.0
    lyzr_max_retries: int = 2
    lyzr_provider_id: str | None = None
    lyzr_model: str | None = None
    lyzr_llm_credential_id: str | None = None

    lyzr_buyer_agent_id: str | None = None
    lyzr_supplier_agent_id: str | None = None
    lyzr_drafter_agent_id: str | None = None
    lyzr_reviewer_agent_id: str | None = None
    lyzr_summary_agent_id: str | None = None
    lyzr_audit_agent_id: str | None = None

    lyzr_rai_policy_id: str | None = None  # shared fallback
    lyzr_rai_buyer_policy_id: str | None = None
    lyzr_rai_supplier_policy_id: str | None = None
    # OPA guardrails are compiled per sealed envelope and registered by content hash (see guardrails/rego.py)
    lyzr_opa_guardrails: bool = True

    aims_mode: Literal["local", "event_log", "agent_session"] = "local"

    @property
    def configured(self) -> bool:
        return bool(self.lyzr_api_key)

    @property
    def negotiators_ready(self) -> bool:
        return self.configured and bool(self.lyzr_buyer_agent_id and self.lyzr_supplier_agent_id)

    @property
    def automata_ready(self) -> bool:
        return self.configured and bool(self.lyzr_drafter_agent_id)

    def agent_id(self, role: str) -> str | None:
        return {
            "buyer": self.lyzr_buyer_agent_id,
            "supplier": self.lyzr_supplier_agent_id,
            "drafter": self.lyzr_drafter_agent_id,
            "reviewer": self.lyzr_reviewer_agent_id or self.lyzr_drafter_agent_id,
            "summary": self.lyzr_summary_agent_id or self.lyzr_drafter_agent_id,
            "audit": self.lyzr_audit_agent_id,
        }.get(role)

    def rai_policy_id(self, role: str) -> str | None:
        """Per-party Safe AI policy (each side's guardrail knows only its own leak patterns), else the shared one."""
        own = {"buyer": self.lyzr_rai_buyer_policy_id, "supplier": self.lyzr_rai_supplier_policy_id}.get(role)
        return own or self.lyzr_rai_policy_id

    @property
    def any_rai_policy(self) -> bool:
        return bool(self.lyzr_rai_policy_id or self.lyzr_rai_buyer_policy_id or self.lyzr_rai_supplier_policy_id)

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "negotiator_agents": self.negotiators_ready,
            "automata_agents": self.automata_ready,
            "rai_policy": self.configured and self.any_rai_policy,
            "rai_per_party": self.configured and bool(self.lyzr_rai_buyer_policy_id and self.lyzr_rai_supplier_policy_id),
            "opa_policies": self.configured and self.lyzr_opa_guardrails,
            "aims_mode": self.aims_mode if self.configured else "local",
            "agent_base_url": self.lyzr_agent_base_url,
            "rai_base_url": self.lyzr_rai_base_url,
        }


@lru_cache(maxsize=1)
def get_lyzr_settings() -> LyzrSettings:
    return LyzrSettings()
