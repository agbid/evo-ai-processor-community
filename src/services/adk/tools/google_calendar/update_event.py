"""Google Calendar event update (reschedule) tool."""

from typing import Optional, Dict, Any, List
from datetime import datetime
from google.adk.tools import FunctionTool, ToolContext
import traceback

from .base import GoogleCalendarClient
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def create_update_calendar_event_tool(
    agent_id: Optional[str] = None,
    calendar_config: Optional[Dict[str, Any]] = None,
    credentials_config: Optional[Dict[str, Any]] = None,
    db=None
) -> FunctionTool:
    """
    Create a tool for updating (rescheduling/editing) Google Calendar events.

    Args:
        agent_id: Optional default agent ID
        calendar_config: Google Calendar configuration from agent.config.integrations
        credentials_config: Google Calendar credentials from agent.config.integrations
        db: Database session for direct database access

    Returns:
        FunctionTool for updating calendar events
    """
    client = GoogleCalendarClient(db=db)

    async def update_calendar_event(
        event_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        calendar_id: str = "primary",
        check_availability: bool = True,
        tool_context: Optional[ToolContext] = None,
    ) -> Dict[str, Any]:
        """
        Update (reschedule or edit) an existing event in Google Calendar.

        Use this tool when:
        - A customer asks to reschedule an existing appointment to a new date/time
        - A customer asks to change the title, description, or attendees of an event

        Only the fields provided are changed; omitted fields keep their current value.
        Do NOT create a new event to represent a reschedule - always update the
        existing event with this tool instead.

        To find the event_id, use check_calendar_availability for the date/time of
        the existing meeting: if the slot is not available, the response's
        "conflicting_events" list contains the event(s) with their "id".

        Args:
            event_id: ID of the event to update (obtained from check_calendar_availability's
                conflicting_events, or from a previous create_calendar_event result)
            start_date: New start date/time in ISO format, if rescheduling
            end_date: New end date/time in ISO format, if rescheduling
            title: New event title, if changing
            description: New event description, if changing
            attendees: New list of attendee emails, if changing
            calendar_id: Which calendar the event is on (default: 'primary')
            check_availability: Whether to verify no conflicts exist at the new time
                (only applies when start_date/end_date are provided, default: True)
            tool_context: Tool execution context

        Returns:
            Dictionary with updated event details or error message
        """
        try:
            logger.info(f"Updating calendar event {event_id} (calendar_id={calendar_id})")

            # Use agent_id from closure (passed to create_update_calendar_event_tool)
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

            if (
                start_date is None
                and end_date is None
                and title is None
                and description is None
                and attendees is None
            ):
                return {
                    "status": "error",
                    "message": "At least one field to update must be provided "
                    "(start_date, end_date, title, description or attendees)"
                }

            # Parse dates if provided
            start_dt = None
            end_dt = None
            try:
                if start_date is not None:
                    start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                if end_date is not None:
                    end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            except ValueError as e:
                return {
                    "status": "error",
                    "message": f"Invalid date format: {str(e)}. Use ISO format like '2024-01-15T14:00:00'"
                }

            if start_dt is not None and end_dt is not None and end_dt <= start_dt:
                return {
                    "status": "error",
                    "message": "End date must be after start date"
                }

            # Use configs from closure (passed from agent.config.integrations)
            # Support both flat and nested structures
            if "settings" in calendar_config:
                config = calendar_config["settings"]
            else:
                # Config values are directly in calendar_config
                config = calendar_config

            # Check availability at the new time if requested
            if check_availability and start_dt is not None and end_dt is not None:
                logger.info(f"Checking availability for {start_date} to {end_date}")
                availability_result = await client.check_availability(
                    credentials_config,
                    start_dt,
                    end_dt,
                    calendar_id
                )

                if availability_result["status"] == "error":
                    logger.error(f"Availability check failed: {availability_result.get('message')}")
                    return availability_result

                if not availability_result["available"]:
                    # Ignore a conflict that is just the event being moved
                    conflicting_events = [
                        event for event in availability_result.get("events", [])
                        if event.get("id") != event_id
                    ]
                    if conflicting_events:
                        logger.warning("Time slot has conflicting events")
                        return {
                            "status": "error",
                            "message": "Time slot has conflicting events",
                            "conflicting_events": [
                                {
                                    "id": event.get("id"),
                                    "summary": event.get("summary", "Untitled"),
                                    "start": event.get("start", {}).get("dateTime"),
                                    "end": event.get("end", {}).get("dateTime")
                                }
                                for event in conflicting_events
                            ]
                        }

            # Update the event
            logger.info("Updating event in Google Calendar")
            result = await client.update_event(
                credentials_config=credentials_config,
                config=config,
                event_id=event_id,
                calendar_id=calendar_id,
                summary=title,
                description=description,
                start_time=start_dt,
                end_time=end_dt,
                attendees=attendees,
            )

            if result["status"] == "error":
                logger.error(f"Event update failed: {result.get('message')}")
                return result

            event_details = result.get("event", {})
            response = {
                "status": "success",
                "message": "Event updated successfully",
                "event": {
                    "id": event_details.get("id"),
                    "title": event_details.get("summary"),
                    "start": event_details.get("start"),
                    "end": event_details.get("end"),
                    "link": event_details.get("link"),
                }
            }

            logger.info(f"Event updated successfully: {event_details.get('id')}")
            return response

        except Exception as e:
            logger.error(f"Unexpected error in update_calendar_event: {str(e)}")
            logger.error(traceback.format_exc())
            return {
                "status": "error",
                "message": f"Failed to update calendar event: {str(e)}"
            }

    # Set function metadata
    update_calendar_event.__name__ = "update_calendar_event"

    return update_calendar_event
