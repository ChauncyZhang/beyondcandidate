from __future__ import annotations

import secrets
import re
from datetime import timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from server.app.identity.api import problem, session_token
from server.app.identity.models import (
    AuditLog,
    Department,
    Job,
    PasswordInvitation,
    User,
    UserRole,
    UserStatus,
    WorkflowTemplate,
)
from server.app.identity.policy import Principal
from server.app.identity.security import PasswordService, hash_token
from server.app.identity.service import InvalidSession


router = APIRouter(prefix="/api/v1/settings")
ADMIN_ROLES = {"system_admin", "recruiting_admin"}
ORGANIZATION_READ_ROLES = ADMIN_ROLES | {"recruiter"}
INVITABLE_ROLES = {
    "system_admin",
    "recruiting_admin",
    "recruiter",
    "hiring_manager",
    "interviewer",
}
RECRUITING_ADMIN_INVITABLE_ROLES = {
    "recruiter",
    "hiring_manager",
    "interviewer",
}
WORKFLOW_TEMPLATE_DEFAULTS = (
    ("标准社招流程", ["一面"]),
    ("技术岗位流程", ["一面", "二面"]),
)
ETAG = re.compile(r'^"([1-9][0-9]*)"$')


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DepartmentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: UUID | None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("department name is required")
        return value


class DepartmentUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "inactive"] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("department name is required")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and self.status is None:
            raise ValueError("at least one department change is required")
        return self


WorkflowRound = str


class WorkflowTemplateCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    rounds: list[WorkflowRound] = Field(min_length=1, max_length=20)
    status: Literal["active", "inactive"] = "active"

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("workflow template name is required")
        return value

    @field_validator("rounds")
    @classmethod
    def normalize_rounds(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 100 for value in normalized):
            raise ValueError("workflow rounds must be non-empty and at most 100 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("workflow rounds must be unique")
        return normalized


class WorkflowTemplateUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    rounds: list[WorkflowRound] | None = Field(default=None, min_length=1, max_length=20)
    status: Literal["active", "inactive"] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return None if value is None else WorkflowTemplateCreate.normalize_name(value)

    @field_validator("rounds")
    @classmethod
    def normalize_rounds(cls, values: list[str] | None) -> list[str] | None:
        return None if values is None else WorkflowTemplateCreate.normalize_rounds(values)

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and self.rounds is None and self.status is None:
            raise ValueError("at least one workflow template change is required")
        return self


class UserInvite(StrictModel):
    display_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    department_id: UUID | None
    role: Literal[
        "system_admin",
        "recruiting_admin",
        "recruiter",
        "hiring_manager",
        "interviewer",
    ]

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display name is required")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip()
        if value.count("@") != 1 or not all(value.split("@")):
            raise ValueError("invalid email address")
        return value


def _principal(
    request: Request, allowed_roles: set[str] = ADMIN_ROLES
) -> Principal | JSONResponse:
    token = session_token(request)
    if not token:
        return problem(
            request, 401, "authentication_required", "Authentication is required."
        )
    try:
        principal = request.app.state.identity_service.principal(token)
    except InvalidSession:
        return problem(
            request, 401, "authentication_required", "Authentication is required."
        )
    if not principal.roles.intersection(allowed_roles):
        return problem(
            request, 403, "authorization_forbidden", "The action is not permitted."
        )
    return principal


def _department_data(department: Department, members: int, jobs: int) -> dict:
    return {
        "id": str(department.id),
        "name": department.name,
        "parent_id": str(department.parent_id) if department.parent_id else None,
        "status": department.status,
        "member_count": members,
        "job_count": jobs,
    }


def _department_counts(db, department: Department) -> tuple[int, int]:
    members = db.scalar(select(func.count(User.id)).where(
        User.organization_id == department.organization_id,
        User.department_id == department.id,
    )) or 0
    jobs = db.scalar(select(func.count(Job.id)).where(
        Job.organization_id == department.organization_id,
        Job.department_id == department.id,
    )) or 0
    return members, jobs


def _department_detail_data(db, department: Department) -> dict:
    members, jobs = _department_counts(db, department)
    member_rows = db.execute(
        select(User)
        .options(selectinload(User.roles))
        .where(
            User.organization_id == department.organization_id,
            User.department_id == department.id,
        )
        .order_by(User.display_name, User.id)
    ).scalars().all()
    job_rows = db.scalars(
        select(Job).where(
            Job.organization_id == department.organization_id,
            Job.department_id == department.id,
        ).order_by(Job.updated_at.desc(), Job.id)
    ).all()
    return {
        **_department_data(department, members, jobs),
        "members": [
            {
                "id": str(user.id),
                "name": user.display_name,
                "roles": sorted(role.role for role in user.roles),
                "status": user.status.value,
            }
            for user in member_rows
        ],
        "jobs": [
            {"id": str(job.id), "title": job.title, "status": job.status}
            for job in job_rows
        ],
    }


def _user_data(user: User, department_name: str | None) -> dict:
    return {
        "id": str(user.id),
        "display_name": user.display_name,
        "email": user.email,
        "department_id": str(user.department_id) if user.department_id else None,
        "department_name": department_name,
        "roles": sorted(role.role for role in user.roles),
        "status": user.status.value,
    }


def _workflow_template_data(template: WorkflowTemplate) -> dict:
    return {
        "id": str(template.id),
        "organization_id": str(template.organization_id),
        "name": template.name,
        "rounds": list(template.rounds),
        "status": template.status,
        "version": template.version,
        "created_at": template.created_at.isoformat(),
        "updated_at": template.updated_at.isoformat(),
    }


def _workflow_template_version(request: Request, value: str | None) -> int | JSONResponse:
    if value is None:
        return problem(request, 428, "precondition_required", "A quoted If-Match version is required.")
    match = ETAG.fullmatch(value)
    if match is None:
        return problem(request, 422, "validation_failed", "If-Match must be a quoted integer.")
    return int(match.group(1))


def _ensure_default_workflow_templates(db, principal: Principal, trace_id: str) -> None:
    existing_names = set(db.scalars(select(WorkflowTemplate.name).where(
        WorkflowTemplate.organization_id == principal.organization_id,
    )).all())
    created = []
    for name, rounds in WORKFLOW_TEMPLATE_DEFAULTS:
        if name in existing_names:
            continue
        template = WorkflowTemplate(
            organization_id=principal.organization_id,
            name=name,
            rounds=list(rounds),
        )
        db.add(template)
        db.flush()
        created.append(template)
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            category="system",
            event_type="organization.workflow_template_default_created",
            outcome="success",
            resource_type="workflow_template",
            resource_id=template.id,
            trace_id=trace_id,
            metadata_json={"status": template.status},
        ))
    if created:
        db.commit()


@router.get("/workflow-templates")
def list_workflow_templates(request: Request):
    principal = _principal(request, ORGANIZATION_READ_ROLES)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        try:
            _ensure_default_workflow_templates(db, principal, request.state.trace_id)
        except IntegrityError:
            db.rollback()
        templates = db.scalars(select(WorkflowTemplate).where(
            WorkflowTemplate.organization_id == principal.organization_id,
        ).order_by(WorkflowTemplate.name, WorkflowTemplate.id)).all()
        return {"data": [_workflow_template_data(template) for template in templates]}


@router.post("/workflow-templates", status_code=201)
def create_workflow_template(payload: WorkflowTemplateCreate, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        template = WorkflowTemplate(
            organization_id=principal.organization_id,
            name=payload.name,
            rounds=payload.rounds,
            status=payload.status,
        )
        db.add(template)
        try:
            db.flush()
            db.add(AuditLog(
                organization_id=principal.organization_id,
                actor_user_id=principal.user_id,
                category="system",
                event_type="organization.workflow_template_created",
                outcome="success",
                resource_type="workflow_template",
                resource_id=template.id,
                trace_id=request.state.trace_id,
                metadata_json={"status": template.status},
            ))
            db.commit()
        except IntegrityError:
            db.rollback()
            return problem(request, 409, "workflow_template_already_exists", "The workflow template already exists.")
        response = JSONResponse({"data": _workflow_template_data(template)}, status_code=201)
        response.headers["ETag"] = f'"{template.version}"'
        return response


@router.patch("/workflow-templates/{template_id}")
def update_workflow_template(
    template_id: UUID,
    payload: WorkflowTemplateUpdate,
    request: Request,
    if_match: str | None = Header(None),
):
    principal = _principal(request)
    expected_version = _workflow_template_version(request, if_match)
    for value in (principal, expected_version):
        if isinstance(value, JSONResponse):
            return value
    with request.app.state.identity_store.sync_session() as db:
        template = db.scalar(select(WorkflowTemplate).where(
            WorkflowTemplate.organization_id == principal.organization_id,
            WorkflowTemplate.id == template_id,
        ).with_for_update())
        if template is None:
            return problem(request, 404, "workflow_template_not_found", "The workflow template was not found.")
        if template.version != expected_version:
            return problem(request, 409, "resource_version_conflict", "The workflow template has changed.")
        changed_fields = []
        for field in ("name", "rounds", "status"):
            value = getattr(payload, field)
            if value is not None and value != getattr(template, field):
                setattr(template, field, value)
                changed_fields.append(field)
        template.version += 1
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            category="system",
            event_type="organization.workflow_template_updated",
            outcome="success",
            resource_type="workflow_template",
            resource_id=template.id,
            trace_id=request.state.trace_id,
            metadata_json={"fields": changed_fields, "status": template.status},
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return problem(request, 409, "workflow_template_already_exists", "The workflow template already exists.")
        db.refresh(template)
        response = JSONResponse({"data": _workflow_template_data(template)})
        response.headers["ETag"] = f'"{template.version}"'
        return response


@router.get("/departments")
def list_departments(request: Request):
    principal = _principal(request, ORGANIZATION_READ_ROLES)
    if isinstance(principal, JSONResponse):
        return principal
    member_count = (
        select(func.count(User.id))
        .where(
            User.organization_id == Department.organization_id,
            User.department_id == Department.id,
        )
        .correlate(Department)
        .scalar_subquery()
    )
    job_count = (
        select(func.count(Job.id))
        .where(
            Job.organization_id == Department.organization_id,
            Job.department_id == Department.id,
        )
        .correlate(Department)
        .scalar_subquery()
    )
    with request.app.state.identity_store.sync_session() as db:
        rows = db.execute(
            select(Department, member_count, job_count)
            .where(Department.organization_id == principal.organization_id)
            .order_by(Department.name, Department.id)
        ).all()
        return {
            "data": [
                _department_data(department, members, jobs)
                for department, members, jobs in rows
            ]
        }


@router.get("/departments/{department_id}")
def get_department(department_id: UUID, request: Request):
    principal = _principal(request, ORGANIZATION_READ_ROLES)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        department = db.scalar(select(Department).where(
            Department.organization_id == principal.organization_id,
            Department.id == department_id,
        ))
        if department is None:
            return problem(request, 404, "department_not_found", "The department was not found.")
        return {"data": _department_detail_data(db, department)}


@router.patch("/departments/{department_id}")
def update_department(department_id: UUID, payload: DepartmentUpdate, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        department = db.scalar(select(Department).where(
            Department.organization_id == principal.organization_id,
            Department.id == department_id,
        ))
        if department is None:
            return problem(request, 404, "department_not_found", "The department was not found.")
        if payload.name is not None and payload.name != department.name:
            duplicate = db.scalar(select(Department.id).where(
                Department.organization_id == principal.organization_id,
                Department.parent_id == department.parent_id,
                Department.name == payload.name,
                Department.id != department.id,
            ))
            if duplicate is not None:
                return problem(request, 409, "department_already_exists", "The department already exists.")
            department.name = payload.name
        if payload.status is not None:
            department.status = payload.status
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            category="system",
            event_type="organization.department_updated",
            outcome="success",
            resource_type="department",
            resource_id=department.id,
            trace_id=request.state.trace_id,
            metadata_json={"status": department.status},
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return problem(request, 409, "department_already_exists", "The department already exists.")
        db.refresh(department)
        return {"data": _department_detail_data(db, department)}


@router.post("/departments", status_code=201)
def create_department(payload: DepartmentCreate, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        if payload.parent_id is not None and db.scalar(
            select(Department.id).where(
                Department.organization_id == principal.organization_id,
                Department.id == payload.parent_id,
                Department.status == "active",
            )
        ) is None:
            return problem(
                request, 422, "department_invalid", "The department is invalid."
            )
        duplicate = db.scalar(
            select(Department.id).where(
                Department.organization_id == principal.organization_id,
                Department.parent_id == payload.parent_id,
                Department.name == payload.name,
            )
        )
        if duplicate is not None:
            return problem(
                request,
                409,
                "department_already_exists",
                "The department already exists.",
            )
        department = Department(
            organization_id=principal.organization_id,
            parent_id=payload.parent_id,
            name=payload.name,
        )
        db.add(department)
        try:
            db.flush()
            db.add(
                AuditLog(
                    organization_id=principal.organization_id,
                    actor_user_id=principal.user_id,
                    category="system",
                    event_type="organization.department_created",
                    outcome="success",
                    resource_type="department",
                    resource_id=department.id,
                    trace_id=request.state.trace_id,
                    metadata_json={},
                )
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            return problem(
                request,
                409,
                "department_already_exists",
                "The department already exists.",
            )
        return JSONResponse(
            {"data": _department_data(department, 0, 0)}, status_code=201
        )


@router.get("/users")
def list_users(request: Request):
    principal = _principal(request, ORGANIZATION_READ_ROLES)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        rows = db.execute(
            select(User, Department.name)
            .options(selectinload(User.roles))
            .outerjoin(
                Department,
                (Department.organization_id == User.organization_id)
                & (Department.id == User.department_id),
            )
            .where(User.organization_id == principal.organization_id)
            .order_by(User.display_name, User.id)
        ).all()
        return {"data": [_user_data(user, name) for user, name in rows]}


@router.post("/users", status_code=201)
def invite_user(
    payload: UserInvite,
    request: Request,
    idempotency_key: str | None = Header(None),
):
    del idempotency_key
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if (
        "system_admin" not in principal.roles
        and payload.role not in RECRUITING_ADMIN_INVITABLE_ROLES
    ):
        return problem(
            request,
            403,
            "role_assignment_forbidden",
            "The role cannot be assigned.",
        )
    if payload.role not in INVITABLE_ROLES:
        return problem(
            request,
            422,
            "validation_failed",
            "The request is invalid.",
        )

    normalized_email = payload.email.casefold()
    now = request.app.state.identity_service.clock.current_time()
    raw_token = request.app.state.identity_service.tokens.new_token()
    with request.app.state.identity_store.sync_session() as db:
        if payload.department_id is not None:
            department = db.scalar(
                select(Department).where(
                    Department.organization_id == principal.organization_id,
                    Department.id == payload.department_id,
                    Department.status == "active",
                )
            )
            if department is None:
                return problem(
                    request, 422, "department_invalid", "The department is invalid."
                )
        else:
            department = None
        if db.scalar(
            select(User.id).where(
                User.organization_id == principal.organization_id,
                User.normalized_email == normalized_email,
            )
        ) is not None:
            return problem(
                request,
                409,
                "user_email_already_exists",
                "The email already exists.",
            )

        user = User(
            organization_id=principal.organization_id,
            department_id=payload.department_id,
            email=payload.email,
            normalized_email=normalized_email,
            display_name=payload.display_name,
            password_hash=PasswordService().hash(secrets.token_urlsafe(48)),
            status=UserStatus.INVITED,
        )
        user.roles.append(UserRole(role=payload.role))
        db.add(user)
        expires_at = now + timedelta(hours=48)
        try:
            db.flush()
            db.add(
                PasswordInvitation(
                    organization_id=principal.organization_id,
                    user_id=user.id,
                    token_hash=hash_token(raw_token),
                    expires_at=expires_at,
                )
            )
            db.add(
                AuditLog(
                    organization_id=principal.organization_id,
                    actor_user_id=principal.user_id,
                    category="system",
                    event_type="identity.user_invited",
                    outcome="success",
                    resource_type="user",
                    resource_id=user.id,
                    trace_id=request.state.trace_id,
                    metadata_json={},
                )
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            return problem(
                request,
                409,
                "user_email_already_exists",
                "The email already exists.",
            )
        return JSONResponse(
            {
                "data": {
                    "user": _user_data(
                        user, department.name if department is not None else None
                    ),
                    "invitation": {
                        "token": raw_token,
                        "expires_at": expires_at.isoformat(),
                    },
                }
            },
            status_code=201,
        )
