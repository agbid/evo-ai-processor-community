"""Google Calendar event deletion (cancellation) tool."""

from typing import Optional, Dict, Any
from google.adk.tools import FunctionTool, ToolContext
import traceback

from .base import GoogleCalendarClient
from .reminders import _extract_context_ids, cancel_meeting_reminders
from src.services.adk.tools.evo_crm.base import EvoCrmClient
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def create_delete_calendar_event_tool(
    agent_id: Optional[str] = None,
    calendar_config: Optional[Dict[str, Any]] = None,
    credentials_config: Optional[Dict[str, Any]] = None,
    db=None
) -> FunctionTool:
    """
    Create a tool for deleting (cancelling) Google Calendar events.

    Args:
        agent_id: Optional default agent ID
        calendar_config: Google Calendar configuration from agent.config.integrations
        credentials_config: Google Calendar credentials from agent.config.integrations
        db: Database session for direct database access

    Returns:
        FunctionTool for deleting calendar events
    """
    client = GoogleCalendarClient(db=db)

    async def delete_calendar_event(
        event_id: str,
        calendar_id: str = "primary",
        notify_attendees: bool = False,
        tool_context: Optional[ToolContext] = None,
    ) -> Dict[str, Any]:
        """
        Cancel/delete an existing event from Google Calendar.

        Use this tool when:
        - A customer asks to cancel an existing appointment/meeting
        - A customer asks to reschedule (cancel the old event, then create the new one)

        Do NOT create a new event with a title like "Cancellation" to represent a
        cancellation - always use this tool to remove the original event instead.

        To find the event_id of the meeting to cancel, use check_calendar_availability
        for the date/time of that meeting: if the slot is not available, the response's
        "conflicting_events" list contains the event(s) with their "id".

        Args:
            event_id: ID of the event to delete (obtained from check_calendar_availability's
                conflicting_events, or from a previous create_calendar_event result)
            calendar_id: Which calendar the event is on (default: 'primary')
            notify_attendees: Whether to send a cancellation email to attendees (default: False)
            tool_context: Tool execution context

        Returns:
            Dictionary with the result of the cancellation
        """
        try:
            logger.info(f"Deleting calendar event {event_id} (calendar_id={calendar_id})")

            # Use agent_id from closure (passed to create_delete_calendar_event_tool)
            effective_agent_id = agent_id

            # Validate required parameters
            if not effective_agent_id:
                return {
                    "status": "error",
                    "message": "Agent ID is required but was not provided"
                }

            # Validate configs provided
            if not calendar_config:
                return {
                    "status": "error",
                    "message": "Google Calendar integration not configured for this agent"
                }

            if not credentials_config:
                return {
                    "status": "error",
                    "message": "Google Calendar credentials not configured for this agent"
                }

            if not event_id or not event_id.strip():
                return {
                    "status": "error",
                    "message": "event_id is required"
                }

            send_updates = "all" if notify_attendees else "none"

            result = await client.delete_event(
                credentials_config=credentials_config,
                event_id=event_id,
                calendar_id=calendar_id,
                send_updates=send_updates
            )

            if result["status"] == "error":
                logger.error(f"Event deletion failed: {result.get('message')}")
                return result

            logger.info(f"Event {event_id} deleted successfully")

            # Cancelar lembretes de reunião vinculados ao evento
            try:
                ctx_ids = _extract_context_ids(tool_context)
                if ctx_ids["conversation_id"]:
                    crm_client = EvoCrmClient()
                    await cancel_meeting_reminders(
                        client=crm_client,
                        conversation_id=ctx_ids["conversation_id"],
                        google_event_id=event_id,
                    )
                else:
                    logger.warning(
                        "Could not cancel meeting reminders: conversation_id not found in context"
                    )
            except Exception as reminder_err:
                logger.warning(f"Failed to cancel meeting reminders: {reminder_err}")

            return {
                "status": "success",
                "message": "Event cancelled successfully",
                "event_id": event_id
            }

        except Exception as e:
            logger.error(f"Unexpected error in delete_calendar_event: {str(e)}")
            logger.error(traceback.format_exc())
            return {
                "status": "error",
                "message": f"Failed to delete calendar event: {str(e)}"
            }

    # Set function metadata
    delete_calendar_event.__name__ = "delete_calendar_event"

    return delete_calendar_event
