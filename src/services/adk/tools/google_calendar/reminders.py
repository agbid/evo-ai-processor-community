"""Helpers para criar, atualizar e cancelar lembretes de reunião via ScheduledActions."""

from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta

from google.adk.tools import ToolContext
from src.services.adk.tools.evo_crm.base import EvoCrmClient
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def _extract_context_ids(tool_context: Optional[ToolContext]) -> Dict[str, Any]:
    """Extrai conversation_id, contact_id e contact_data do tool_context.state."""
    result = {"conversation_id": None, "contact_id": None, "contact_data": {}}

    if not tool_context or not hasattr(tool_context, "state"):
        return result

    state = tool_context.state
    evoai_crm_data = state.get("evoai_crm_data", {})

    if isinstance(evoai_crm_data, dict):
        result["conversation_id"] = evoai_crm_data.get("conversation_id")
        if not result["conversation_id"]:
            conv = evoai_crm_data.get("conversation", {})
            if isinstance(conv, dict):
                result["conversation_id"] = conv.get("id")

        contact = evoai_crm_data.get("contact", {})
        if isinstance(contact, dict):
            result["contact_id"] = contact.get("id")
            result["contact_data"] = contact

    if not result["conversation_id"]:
        for key in ["conversation_id", "conversationId"]:
            if key in state:
                result["conversation_id"] = str(state[key])
                break

    if result["conversation_id"]:
        result["conversation_id"] = str(result["conversation_id"])
    if result["contact_id"]:
        result["contact_id"] = str(result["contact_id"])

    return result


def _resolve_placeholders(
    message: str,
    contact_data: Dict[str, Any],
    title: str,
    start_dt: datetime,
    meet_link: str = "",
) -> str:
    """Substitui placeholders na mensagem com dados reais."""
    name = contact_data.get("name") or contact_data.get("full_name") or ""
    first_name = name.split()[0] if name else name

    date_str = start_dt.strftime("%d/%m/%Y")
    time_str = start_dt.strftime("%H:%M")

    return (
        message
        .replace("{{contact.first_name}}", first_name)
        .replace("{{contact.name}}", name)
        .replace("{{meeting.title}}", title)
        .replace("{{meeting.date}}", date_str)
        .replace("{{meeting.time}}", time_str)
        .replace("{{meeting.link}}", meet_link or "")
    )


async def create_meeting_reminders(
    client: EvoCrmClient,
    conversation_id: str,
    contact_id: str,
    google_event_id: str,
    start_dt: datetime,
    title: str,
    contact_data: Dict[str, Any],
    meeting_reminders: List[Dict[str, Any]],
    meet_link: str = "",
) -> None:
    """Cria ScheduledActions de lembrete para cada item de meeting_reminders."""
    if not meeting_reminders or not conversation_id or not contact_id:
        return

    now = datetime.now(timezone.utc)

    for reminder in meeting_reminders:
        offset_minutes = reminder.get("offset_minutes")
        message_template = reminder.get("message", "")

        if not offset_minutes or not message_template:
            continue

        scheduled_for = start_dt - timedelta(minutes=int(offset_minutes))

        if scheduled_for <= now:
            logger.info(
                f"Skipping reminder offset={offset_minutes}min: scheduled_for={scheduled_for} is in the past"
            )
            continue

        resolved_message = _resolve_placeholders(
            message_template, contact_data, title, start_dt, meet_link
        )

        payload = {
            "message": resolved_message,
            "channel": "whatsapp",
            "google_event_id": google_event_id,
            "reminder_offset_minutes": int(offset_minutes),
            "meet_link": meet_link,
        }

        try:
            await client.post(
                "/api/v1/scheduled_actions",
                json_data={
                    "contact_id": contact_id,
                    "conversation_id": conversation_id,
                    "action_type": "send_message",
                    "scheduled_for": scheduled_for.isoformat(),
                    "payload": payload,
                },
            )
            logger.info(
                f"Meeting reminder created: offset={offset_minutes}min, scheduled_for={scheduled_for}, "
                f"google_event_id={google_event_id}"
            )
        except Exception as e:
            logger.warning(f"Failed to create meeting reminder (offset={offset_minutes}min): {e}")


async def cancel_meeting_reminders(
    client: EvoCrmClient,
    conversation_id: str,
    google_event_id: str,
) -> None:
    """Cancela (DELETE) todos os ScheduledActions de lembrete vinculados ao evento."""
    if not conversation_id or not google_event_id:
        return

    try:
        response = await client.get(
            "/api/v1/scheduled_actions",
            params={
                "conversation_id": conversation_id,
                "status": "scheduled",
                "google_event_id": google_event_id,
            },
        )
        items = response.get("data", [])
        if not isinstance(items, list):
            items = []
    except Exception as e:
        logger.warning(f"Failed to fetch meeting reminders for cancellation: {e}")
        return

    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
        try:
            await client.delete(f"/api/v1/scheduled_actions/{item_id}")
            logger.info(f"Meeting reminder {item_id} deleted (event cancelled)")
        except Exception as e:
            logger.warning(f"Failed to delete meeting reminder {item_id}: {e}")


async def update_meeting_reminders(
    client: EvoCrmClient,
    conversation_id: str,
    google_event_id: str,
    new_start_dt: datetime,
    title: str,
    contact_data: Dict[str, Any],
    meeting_reminders: List[Dict[str, Any]],
) -> None:
    """Recalcula scheduled_for dos lembretes após reagendamento.

    Para cada lembrete existente:
    - Se novo scheduled_for <= agora: cancela (DELETE)
    - Caso contrário: atualiza scheduled_for e mensagem (PATCH)
    """
    if not conversation_id or not google_event_id:
        return

    try:
        response = await client.get(
            "/api/v1/scheduled_actions",
            params={
                "conversation_id": conversation_id,
                "status": "scheduled",
                "google_event_id": google_event_id,
            },
        )
        items = response.get("data", [])
        if not isinstance(items, list):
            items = []
    except Exception as e:
        logger.warning(f"Failed to fetch meeting reminders for update: {e}")
        return

    now = datetime.now(timezone.utc)

    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue

        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        offset_minutes = payload.get("reminder_offset_minutes")
        if not offset_minutes:
            continue

        meet_link = payload.get("meet_link", "")
        new_scheduled_for = new_start_dt - timedelta(minutes=int(offset_minutes))

        if new_scheduled_for <= now:
            try:
                await client.delete(f"/api/v1/scheduled_actions/{item_id}")
                logger.info(
                    f"Meeting reminder {item_id} deleted (new time {new_scheduled_for} is in past)"
                )
            except Exception as e:
                logger.warning(f"Failed to delete past-dated meeting reminder {item_id}: {e}")
        else:
            # Re-resolve message template for new date/time
            original_template = _find_template_for_offset(meeting_reminders, int(offset_minutes))
            if original_template:
                resolved_message = _resolve_placeholders(
                    original_template, contact_data, title, new_start_dt, meet_link
                )
            else:
                resolved_message = payload.get("message", "")

            updated_payload = {
                **payload,
                "message": resolved_message,
                "google_event_id": google_event_id,
            }

            try:
                await client.patch(
                    f"/api/v1/scheduled_actions/{item_id}",
                    json_data={
                        "scheduled_for": new_scheduled_for.isoformat(),
                        "payload": updated_payload,
                    },
                )
                logger.info(
                    f"Meeting reminder {item_id} rescheduled to {new_scheduled_for}"
                )
            except Exception as e:
                logger.warning(f"Failed to update meeting reminder {item_id}: {e}")


def _find_template_for_offset(
    meeting_reminders: List[Dict[str, Any]], offset_minutes: int
) -> Optional[str]:
    """Retorna o template de mensagem para um dado offset_minutes."""
    for reminder in meeting_reminders:
        if int(reminder.get("offset_minutes", -1)) == offset_minutes:
            return reminder.get("message")
    return None
