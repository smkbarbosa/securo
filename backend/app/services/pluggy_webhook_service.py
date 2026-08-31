import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.models.bank_connection import BankConnection
from app.services import connection_service

logger = logging.getLogger(__name__)


async def get_pluggy_connection_by_item(
    session: AsyncSession, item_id: Optional[str]
) -> Optional[BankConnection]:
    """Resolve a Pluggy itemId to a pending bank connection."""
    if not item_id:
        return None
    return await connection_service.get_pending_connection_by_provider_item(
        session, "pluggy", item_id
    )


async def mark_pluggy_item_error(
    session: AsyncSession, connection: BankConnection, error: Optional[dict]
) -> None:
    """Mark a connection as errored because of a Pluggy item/error event.

    The Pluggy webhook points at an item; we replay the failure onto the
    connection so the UI surfaces it as needing attention. We do not trigger
    a full sync here — the error is about the linkage, not newly imported
    transactions — but we ask the provider to reflect the error state so a
    future sync does not silently read stale data.
    """
    connection.status = "error"
    detail = None
    if isinstance(error, dict):
        detail = str(error.get("message") or error.get("code") or error)[:500]
    settings = dict(connection.settings or {})
    settings["pluggy_error"] = detail
    connection.settings = settings
    await session.commit()
    logger.warning(
        "Pluggy item/error -> connection=%s provider='pluggy' status='error' (%s)",
        connection.id,
        detail or "no detail",
    )


async def handle_pluggy_event(event: dict) -> None:
    """Process a single Pluggy webhook event in its own DB session.

    Called off the request path (background task) so the webhook can answer
    within Pluggy's 5s budget. Events handled:
      - item/created, item/updated: refresh what the provider already has for
        that item (equivalent to a background sync), so transactions flow in.
      - item/error: mark the connection as errored for the UI.
    """
    event_type = event.get("event")
    item_id = event.get("itemId")
    if not event_type:
        logger.warning("Pluggy webhook event missing 'event' field; ignoring")
        return

    async with async_session_maker() as session:
        try:
            if event_type in ("item/created", "item/updated"):
                connection = await get_pluggy_connection_by_item(session, item_id)
                if not connection:
                    logger.info(
                        "Pluggy %s: no bound connection for item %s; skipping sync",
                        event_type, item_id,
                    )
                    return
                try:
                    await connection_service.sync_connection(
                        session,
                        connection.id,
                        connection.workspace_id,
                        connection.user_id,
                        trigger_provider_refresh=True,
                    )
                    logger.info(
                        "Pluggy %s: synced connection=%s item=%s",
                        event_type, connection.id, item_id,
                    )
                except Exception as exc:  # noqa: BLE001 - background task
                    logger.exception(
                        "Pluggy %s: sync failed for connection=%s item=%s: %s",
                        event_type, connection.id, item_id, exc,
                    )
            elif event_type == "item/error":
                connection = await get_pluggy_connection_by_item(session, item_id)
                if connection:
                    await mark_pluggy_item_error(session, connection, event.get("error"))
            else:
                logger.info("Pluggy webhook: unhandled event %s (ignored)", event_type)
        except Exception:  # noqa: BLE001
            logger.exception("Pluggy webhook processing failed for event %s", event_type)
