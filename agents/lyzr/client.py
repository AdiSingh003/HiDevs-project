"""Thin, retrying client for the Lyzr Agent API (https://agent-prod.studio.lyzr.ai).

Endpoints used (from the published OpenAPI spec):

* ``POST /v3/inference/chat/``  - chat with an agent (``agent_id``, ``session_id``, ``user_id``, ``message``)
* ``POST /v3/agents/``          - create an agent (``name``, ``provider_id``, ``model``, ``temperature``, ``top_p`` ...)
* ``GET  /v3/agents/``          - list agents for the API key
* ``POST /log/{session_id}``    - append an event to a session log (used as the AIMS audit sink)
* ``GET  /v1/sessions/{session_id}/conversation`` - read back a session transcript
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .settings import LyzrSettings

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class LyzrError(RuntimeError):
    pass


@dataclass
class LyzrReply:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


def extract_text(data: Any) -> str:
    """Pull the assistant text out of a Lyzr inference response."""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("response", "message", "output", "content", "text", "answer", "result"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, (dict, list)):
                nested = extract_text(value)
                if nested:
                    return nested
    if isinstance(data, list):
        for item in data:
            nested = extract_text(item)
            if nested:
                return nested
    return ""


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of the first JSON object from an LLM reply."""
    if not text:
        return None
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for chunk in candidates:
        start = chunk.find("{")
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(chunk)):
                ch = chunk[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(chunk[start:i + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(parsed, dict):
                            return parsed
                        break
            start = chunk.find("{", start + 1)
    return None


class LyzrAgentClient:
    def __init__(self, settings: LyzrSettings, transport: httpx.AsyncBaseTransport | None = None,
                 sync_transport: httpx.BaseTransport | None = None):
        if not settings.lyzr_api_key:
            raise LyzrError("LYZR_API_KEY is not configured")
        self.settings = settings
        headers = {"x-api-key": settings.lyzr_api_key, "Content-Type": "application/json", "accept": "application/json"}
        self._client = httpx.AsyncClient(base_url=settings.lyzr_agent_base_url, headers=headers,
                                         timeout=settings.lyzr_timeout_s, transport=transport)
        self._sync = httpx.Client(base_url=settings.lyzr_agent_base_url, headers=headers,
                                  timeout=settings.lyzr_timeout_s, transport=sync_transport)

    async def aclose(self) -> None:
        await self._client.aclose()
        self._sync.close()

    # ------------------------------------------------------------------ transport helpers

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempts = self.settings.lyzr_max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = await self._client.request(method, url, **kwargs)
                if resp.status_code in RETRY_STATUS and attempt < attempts - 1:
                    await asyncio.sleep(0.4 * 2 ** attempt)
                    continue
                if resp.status_code >= 400:
                    raise LyzrError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
                return resp
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(0.4 * 2 ** attempt)
                    continue
        raise LyzrError(f"{method} {url} failed: {last_exc}")

    def _request_sync(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempts = self.settings.lyzr_max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._sync.request(method, url, **kwargs)
                if resp.status_code in RETRY_STATUS and attempt < attempts - 1:
                    time.sleep(0.4 * 2 ** attempt)
                    continue
                if resp.status_code >= 400:
                    raise LyzrError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
                return resp
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(0.4 * 2 ** attempt)
                    continue
        raise LyzrError(f"{method} {url} failed: {last_exc}")

    def _chat_payload(self, agent_id: str, session_id: str, message: str,
                      system_prompt_variables: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "user_id": self.settings.lyzr_user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "message": message,
            "system_prompt_variables": system_prompt_variables or {},
            "filter_variables": {},
            "features": [],
        }

    # ------------------------------------------------------------------ inference

    async def chat(self, agent_id: str, session_id: str, message: str,
                   system_prompt_variables: dict[str, Any] | None = None) -> LyzrReply:
        started = time.perf_counter()
        resp = await self._request("POST", "/v3/inference/chat/",
                                   json=self._chat_payload(agent_id, session_id, message, system_prompt_variables))
        data = resp.json()
        return LyzrReply(extract_text(data), data if isinstance(data, dict) else {"data": data},
                         int((time.perf_counter() - started) * 1000))

    def chat_sync(self, agent_id: str, session_id: str, message: str,
                  system_prompt_variables: dict[str, Any] | None = None) -> LyzrReply:
        started = time.perf_counter()
        resp = self._request_sync("POST", "/v3/inference/chat/",
                                  json=self._chat_payload(agent_id, session_id, message, system_prompt_variables))
        data = resp.json()
        return LyzrReply(extract_text(data), data if isinstance(data, dict) else {"data": data},
                         int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ management

    async def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("POST", "/v3/agents/", json=payload)
        return resp.json()

    async def update_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("PUT", f"/v3/agents/{agent_id}", json=payload)
        return resp.json() if resp.content else {}

    async def list_agents(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/v3/agents/")
        data = resp.json()
        return data if isinstance(data, list) else data.get("agents", [])

    async def log_event(self, session_id: str, message: str) -> bool:
        await self._request("POST", f"/log/{session_id}", params={"message": message})
        return True

    async def session_conversation(self, session_id: str) -> Any:
        resp = await self._request("GET", f"/v1/sessions/{session_id}/conversation")
        return resp.json()


def agent_id_from_response(data: dict[str, Any]) -> str | None:
    for key in ("agent_id", "_id", "id"):
        if isinstance(data.get(key), str):
            return data[key]
    if isinstance(data.get("data"), dict):
        return agent_id_from_response(data["data"])
    return None
