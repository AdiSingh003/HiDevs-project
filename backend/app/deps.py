from __future__ import annotations

from fastapi import Request

from .services import RunService


def get_service(request: Request) -> RunService:
    return request.app.state.service
