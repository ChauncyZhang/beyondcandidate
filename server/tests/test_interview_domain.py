from datetime import datetime, timezone
from uuid import UUID

from server.app.interviews import domain as interview_domain
from server.app.interviews.domain import ScheduleSlot, build_calendar_invitation, schedule_conflict


INTERVIEWER_A = UUID("11111111-1111-4111-8111-111111111111")
INTERVIEWER_B = UUID("22222222-2222-4222-8222-222222222222")


def slot(hour: int, minute: int, duration: int, *participants: UUID, status: str = "scheduled") -> ScheduleSlot:
    return ScheduleSlot(
        interview_id=UUID(int=hour * 10_000 + minute * 100 + duration),
        starts_at=datetime(2026, 7, 15, hour, minute, tzinfo=timezone.utc),
        duration_minutes=duration,
        participant_ids=frozenset(participants),
        status=status,
    )


def test_schedule_conflict_distinguishes_overlap_buffer_and_unrelated_people() -> None:
    existing = slot(8, 0, 60, INTERVIEWER_A)

    assert schedule_conflict(slot(8, 30, 30, INTERVIEWER_A), existing, buffer_minutes=15) == "hard"
    assert schedule_conflict(slot(9, 10, 30, INTERVIEWER_A), existing, buffer_minutes=15) == "soft"
    assert schedule_conflict(slot(9, 15, 30, INTERVIEWER_A), existing, buffer_minutes=15) is None
    assert schedule_conflict(slot(8, 30, 30, INTERVIEWER_B), existing, buffer_minutes=15) is None
    assert schedule_conflict(slot(8, 30, 30, INTERVIEWER_A), slot(8, 0, 60, INTERVIEWER_A, status="cancelled"), buffer_minutes=15) is None


def test_calendar_invitation_is_rfc5545_shaped_and_escapes_user_text() -> None:
    payload = build_calendar_invitation(
        interview_id=UUID("33333333-3333-4333-8333-333333333333"),
        starts_at=datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc),
        duration_minutes=45,
        summary="AI 工程师, 一面",
        location="北京办公室; 3F\\海棠",
        description="请提前 5 分钟到场\n携带作品集",
        sequence=2,
        dtstamp=datetime(2026, 7, 14, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert payload.startswith(b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert payload.endswith(b"END:VCALENDAR\r\n")
    assert b"DTSTART:20260715T080000Z\r\n" in payload
    assert b"DTEND:20260715T084500Z\r\n" in payload
    assert b"DTSTAMP:20260714T010203Z\r\n" in payload
    assert b"SEQUENCE:2\r\n" in payload
    text = payload.decode("utf-8")
    assert "SUMMARY:AI 工程师\\, 一面" in text
    assert "LOCATION:北京办公室\\; 3F\\\\海棠" in text
    assert "DESCRIPTION:请提前 5 分钟到场\\n携带作品集" in text
    assert "\n" not in text.replace("\r\n", "")


def test_cancelled_calendar_update_has_cancel_semantics() -> None:
    payload = build_calendar_invitation(
        interview_id=UUID("44444444-4444-4444-8444-444444444444"),
        starts_at=datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc),
        duration_minutes=45,
        summary="AI Engineer interview",
        location="Online",
        description="Cancelled by recruiter",
        sequence=3,
        dtstamp=datetime(2026, 7, 14, 1, 2, 3, tzinfo=timezone.utc),
        status="cancelled",
    )

    assert b"METHOD:CANCEL\r\n" in payload
    assert b"STATUS:CANCELLED\r\n" in payload
    assert b"SEQUENCE:3\r\n" in payload


def test_calendar_invitation_and_cancellation_keep_itip_identity_and_participants() -> None:
    interview_id = UUID("55555555-5555-4555-8555-555555555555")
    organizer = interview_domain.CalendarContact(name='Recruiter "Lead"', email="recruiter@example.com")
    attendees = (
        interview_domain.CalendarContact(name="Candidate, A", email="candidate@example.com"),
        interview_domain.CalendarContact(name="Interviewer; B", email="interviewer@example.com"),
    )
    common = {
        "interview_id": interview_id,
        "starts_at": datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc),
        "duration_minutes": 45,
        "summary": "AI Engineer interview",
        "location": "Online",
        "description": "Interview invitation",
        "dtstamp": datetime(2026, 7, 14, 1, 2, 3, tzinfo=timezone.utc),
        "organizer": organizer,
        "attendees": attendees,
    }

    invitation = build_calendar_invitation(**common, sequence=2).decode("utf-8")
    cancellation = build_calendar_invitation(**common, sequence=3, status="cancelled").decode("utf-8")

    uid = f"UID:{interview_id}@beyondcandidate\r\n"
    assert "METHOD:REQUEST\r\n" in invitation
    assert "METHOD:CANCEL\r\n" in cancellation
    assert "STATUS:CANCELLED\r\n" in cancellation
    assert uid in invitation
    assert uid in cancellation
    assert "SEQUENCE:2\r\n" in invitation
    assert "SEQUENCE:3\r\n" in cancellation
    assert 'ORGANIZER;CN="Recruiter ^\'Lead^\'":mailto:recruiter@example.com\r\n' in invitation
    assert 'ATTENDEE;CN="Candidate, A":mailto:candidate@example.com\r\n' in invitation
    assert 'ATTENDEE;CN="Interviewer; B":mailto:interviewer@example.com\r\n' in invitation
    for participant_line in (
        'ORGANIZER;CN="Recruiter ^\'Lead^\'":mailto:recruiter@example.com\r\n',
        'ATTENDEE;CN="Candidate, A":mailto:candidate@example.com\r\n',
        'ATTENDEE;CN="Interviewer; B":mailto:interviewer@example.com\r\n',
    ):
        assert participant_line in cancellation
