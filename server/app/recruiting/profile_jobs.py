import uuid

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError

from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import (
    Application,
    Candidate,
    FileObject,
    Resume,
    ResumeProfile,
)
from server.app.recruiting.profile_builder import PROFILE_VERSION


class ResumeProfileJobHandler:
    def __init__(self, sessions, text_enhancer, profile_builder):
        self.sessions = sessions
        self.text_enhancer = text_enhancer
        self.profile_builder = profile_builder

    async def __call__(self, job):
        try:
            organization_id = uuid.UUID(str(job.payload["organization_id"]))
            resume_id = uuid.UUID(str(job.payload["resume_id"]))
            job_organization_id = uuid.UUID(str(job.organization_id))
        except (AttributeError, KeyError, TypeError, ValueError):
            raise PermanentJobError("resume_profile_payload_invalid") from None
        if job_organization_id != organization_id:
            raise PermanentJobError("resume_profile_payload_invalid")

        with self.sessions() as database:
            row = database.execute(
                select(Resume, FileObject, Candidate)
                .join(
                    FileObject,
                    and_(
                        FileObject.organization_id == Resume.organization_id,
                        FileObject.id == Resume.file_object_id,
                    ),
                )
                .join(
                    Candidate,
                    and_(
                        Candidate.organization_id == Resume.organization_id,
                        Candidate.id == Resume.candidate_id,
                    ),
                )
                .where(
                    Resume.organization_id == organization_id,
                    Resume.id == resume_id,
                    Resume.parsed_text.is_not(None),
                    Candidate.deleted_at.is_(None),
                )
            ).one_or_none()
            if row is None:
                raise PermanentJobError("resume_profile_source_missing")
            if database.scalar(
                select(ResumeProfile.id).where(
                    ResumeProfile.organization_id == organization_id,
                    ResumeProfile.resume_id == resume_id,
                )
            ):
                return
            resume, file_object, candidate = row
            application = database.scalar(
                select(Application)
                .where(
                    Application.organization_id == organization_id,
                    Application.resume_id == resume_id,
                )
                .order_by(Application.updated_at.desc())
            )
            native_text = resume.parsed_text or ""
            source = (
                file_object.storage_key,
                file_object.original_filename,
                file_object.mime_type,
                candidate.display_name,
                application.job_id if application else None,
            )

        storage_key, filename, mime_type, candidate_name, job_id = source
        enriched = await self.text_enhancer.enhance(
            organization_id,
            storage_key=storage_key,
            filename=filename,
            mime_type=mime_type,
            native_text=native_text,
        )
        profile = await self.profile_builder.build(
            organization_id,
            job_id=job_id,
            resume_text=enriched.text,
            candidate_name=candidate_name,
            used_ocr=enriched.used_ocr,
            trace_id=getattr(job, "trace_id", None),
        )

        try:
            with self.sessions() as database:
                active_candidate = database.scalar(
                    select(Candidate)
                    .join(
                        Resume,
                        and_(
                            Resume.organization_id == Candidate.organization_id,
                            Resume.candidate_id == Candidate.id,
                        ),
                    )
                    .where(
                        Candidate.organization_id == organization_id,
                        Candidate.deleted_at.is_(None),
                        Resume.id == resume_id,
                        Resume.parsed_text.is_not(None),
                    )
                    .with_for_update()
                )
                if active_candidate is None:
                    return
                if database.scalar(
                    select(ResumeProfile.id).where(
                        ResumeProfile.organization_id == organization_id,
                        ResumeProfile.resume_id == resume_id,
                    )
                ):
                    return
                database.add(
                    ResumeProfile(
                        id=uuid.uuid5(resume_id, "resume-profile"),
                        organization_id=organization_id,
                        resume_id=resume_id,
                        data=profile.data,
                        status=profile.status,
                        source=profile.source,
                        profile_version=PROFILE_VERSION,
                        safe_error_code=profile.safe_error_code or enriched.safe_error_code,
                    )
                )
                database.commit()
        except SQLAlchemyError:
            raise RetryableJobError("resume_profile_persistence_failed") from None


def enqueue_missing_resume_profiles(sessions, *, batch_size: int = 1000) -> int:
    total = 0
    last_resume_id = None
    while True:
        with sessions() as database:
            query = (
                select(Resume.organization_id, Resume.id)
                .join(Candidate, and_(Candidate.organization_id == Resume.organization_id, Candidate.id == Resume.candidate_id))
                .outerjoin(
                    ResumeProfile,
                    and_(
                        ResumeProfile.organization_id == Resume.organization_id,
                        ResumeProfile.resume_id == Resume.id,
                    ),
                )
                .where(
                    ResumeProfile.id.is_(None),
                    Resume.parsed_text.is_not(None),
                    Candidate.deleted_at.is_(None),
                )
                .order_by(Resume.id)
                .limit(batch_size)
            )
            if last_resume_id is not None:
                query = query.where(Resume.id > last_resume_id)
            rows = database.execute(query).all()
            queue = QueueRepository(database)
            for organization_id, resume_id in rows:
                queue.enqueue(
                    organization_id,
                    "screening.profile_resume",
                    {"organization_id": str(organization_id), "resume_id": str(resume_id)},
                    dedupe_key=f"resume-profile:{resume_id}:{PROFILE_VERSION}",
                    max_attempts=3,
                )
            database.commit()
        total += len(rows)
        if len(rows) < batch_size:
            return total
        last_resume_id = rows[-1][1]
