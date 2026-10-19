"""Persist events before broadcasting, so browsers can replay missed updates."""

import asyncio
from collections import defaultdict

from ..database import Event, SessionLocal
from ..security import redact


def event_dict(event: Event) -> dict:
    return {
        "id": event.id,
        "run_id": event.run_id,
        "agent": event.agent,
        "kind": event.kind,
        "message": event.message,
        "details": event.details,
        "created_at": event.created_at.isoformat(),
    }


class EventBus:
    def __init__(self):
        self.clients = defaultdict(set)

    async def emit(
        self,
        project_id: int,
        agent: str,
        kind: str,
        message: str,
        run_id: int | None = None,
        details: dict | None = None,
    ) -> dict:
        with SessionLocal() as db:
            row = Event(
                project_id=project_id,
                run_id=run_id,
                agent=agent,
                kind=kind,
                message=redact(message),
                details=details or {},
            )
            db.add(row)
            db.commit()
            payload = event_dict(row)
        for socket in list(self.clients[project_id]):
            try:
                await asyncio.wait_for(socket.send_json(payload), timeout=2)
            except Exception:
                self.clients[project_id].discard(socket)
        return payload


bus = EventBus()
