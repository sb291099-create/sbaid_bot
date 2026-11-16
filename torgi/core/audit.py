from __future__ import annotations

from typing import Any

from ..models import AuditLog
from .context import Ctx


def audit(ctx: Ctx, actor_id: int | None, action: str, **data: Any) -> None:
    """Запись в неизменяемый журнал аудита: все действия админа и смены статусов."""
    ctx.s.add(AuditLog(actor_id=actor_id, action=action, data=_jsonable(data), created_at=ctx.now))


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[k] = v
    return out
