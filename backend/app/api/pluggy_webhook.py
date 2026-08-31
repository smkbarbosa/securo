import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from app.core.config import get_settings
from app.services.pluggy_webhook_service import handle_pluggy_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

# Keep strong references to background tasks so the event loop does not
# garbage-collect a task (and cancel it) right after the request returns.
_background_tasks: set[asyncio.Task] = set()


class PluggyWebhookEnvelope(BaseModel):
    event: str
    eventId: Optional[str] = None
    itemId: Optional[str] = None
    accountId: Optional[str] = None
    error: Optional[dict[str, Any]] = None


@router.post("/pluggy")
async def pluggy_webhook(
    request: Request,
    x_pluggy_webhook_secret: Optional[str] = Header(default=None),
):
    """Receive provider webhooks from Pluggy.

    Pluggy posts `item/created`, `item/updated`, `item/error` (and
    transaction-level events) here. We ack immediately (200) and process
    off the request path so the response stays under Pluggy's 5s timeout.
    """
    settings = get_settings()
    if settings.pluggy_webhook_secret:
        provided = x_pluggy_webhook_secret or ""
        if provided != settings.pluggy_webhook_secret:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook secret",
            )

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    if isinstance(payload, list):
        events = payload
    else:
        events = [payload]

    for event in events:
        try:
            pluggy_event = PluggyWebhookEnvelope(**event)
        except Exception:  # noqa: BLE001
            logger.warning("Pluggy webhook: ignoring malformed event %s", event)
            continue
        task = asyncio.create_task(handle_pluggy_event(pluggy_event.model_dump()))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return {"received": True}
