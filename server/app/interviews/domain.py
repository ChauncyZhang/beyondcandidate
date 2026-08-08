from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID


ConflictKind = Literal["hard", "soft"]


@dataclass(frozen=True)
class CalendarContact:
    name: str
    email: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("calendar contact name must not be empty")
        if (
            self.email != self.email.strip()
            or self.email.lower().startswith("mailto:")
            or self.email.count("@") != 1
            or any(character.isspace() or ord(character) < 32 for character in self.email)
        ):
            raise ValueError("calendar contact email must be a plain email address")


DEFAULT_CALENDAR_ORGANIZER = CalendarContact(name="HR Resume Filter", email="recruiting@beyondcandidate.local")
DEFAULT_CALENDAR_ATTENDEES = (
    CalendarContact(name="Interview participant", email="participant@beyondcandidate.local"),
)


@dataclass(frozen=True)
class ScheduleSlot:
    interview_id: UUID
    starts_at: datetime
    duration_minutes: int
    participant_ids: frozenset[UUID]
    status: str = "scheduled"

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.starts_at.utcoffset() is None:
            raise ValueError("starts_at must include a timezone")
        if self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(minutes=self.duration_minutes)


def schedule_conflict(proposed: ScheduleSlot, existing: ScheduleSlot, *, buffer_minutes: int) -> ConflictKind | None:
    if buffer_minutes < 0:
        raise ValueError("buffer_minutes must not be negative")
    if proposed.interview_id == existing.interview_id:
        return None
    if proposed.status == "cancelled" or existing.status == "cancelled":
        return None
    if not proposed.participant_ids.intersection(existing.participant_ids):
        return None
    if proposed.starts_at < existing.ends_at and existing.starts_at < proposed.ends_at:
        return "hard"
    gap = min(abs(proposed.starts_at - existing.ends_at), abs(existing.starts_at - proposed.ends_at))
    return "soft" if gap < timedelta(minutes=buffer_minutes) else None


def _escape_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _escape_parameter_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("^", "^^").replace('"', "^'").replace("\n", "^n")


def _contact_line(property_name: str, contact: CalendarContact) -> str:
    return f'{property_name};CN="{_escape_parameter_text(contact.name)}":mailto:{contact.email}'


def _fold_line(line: str) -> list[str]:
    folded: list[str] = []
    current = ""
    byte_limit = 75
    for character in line:
        candidate = current + character
        if current and len(candidate.encode("utf-8")) > byte_limit:
            folded.append(current)
            current = " " + character
            byte_limit = 75
        else:
            current = candidate
    folded.append(current)
    return folded


def _utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("calendar timestamps must include a timezone")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_calendar_invitation(
    *,
    interview_id: UUID,
    starts_at: datetime,
    duration_minutes: int,
    summary: str,
    location: str,
    description: str,
    sequence: int,
    dtstamp: datetime,
    status: str = "scheduled",
    organizer: CalendarContact = DEFAULT_CALENDAR_ORGANIZER,
    attendees: tuple[CalendarContact, ...] = DEFAULT_CALENDAR_ATTENDEES,
    timezone_name: str | None = None,
) -> bytes:
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive")
    if sequence < 0:
        raise ValueError("sequence must not be negative")
    if status not in {"draft", "scheduled", "confirmed", "completed", "pending_feedback", "feedback_completed", "rescheduled", "cancelled", "no_show"}:
        raise ValueError("unsupported calendar status")
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    timezone_lines = [] if timezone_name is None else [f"X-WR-TIMEZONE:{_escape_text(timezone_name)}"]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//HR Resume Filter//Recruiting Interview//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:CANCEL" if status == "cancelled" else "METHOD:REQUEST",
        *timezone_lines,
        "BEGIN:VEVENT",
        f"UID:{interview_id}@beyondcandidate",
        f"DTSTAMP:{_utc_stamp(dtstamp)}",
        f"DTSTART:{_utc_stamp(starts_at)}",
        f"DTEND:{_utc_stamp(ends_at)}",
        f"SEQUENCE:{sequence}",
        *(["STATUS:CANCELLED"] if status == "cancelled" else []),
        _contact_line("ORGANIZER", organizer),
        *(_contact_line("ATTENDEE", attendee) for attendee in attendees),
        f"SUMMARY:{_escape_text(summary)}",
        f"LOCATION:{_escape_text(location)}",
        f"DESCRIPTION:{_escape_text(description)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    folded = [part for line in lines for part in _fold_line(line)]
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")
