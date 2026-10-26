from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from server.app.identity.admin_api import ADMIN_ROLES, INVITABLE_ROLES, RECRUITING_ADMIN_INVITABLE_ROLES, _principal
from server.app.identity.api import problem, session_token
from server.app.identity.models import AuditLog, Organization, PasswordInvitation, User, UserRole, UserSession, UserStatus
from server.app.identity.offboarding_models import MemberOffboarding, handover_destination
from server.app.identity.service import InvalidSession


router = APIRouter(prefix="/api/v1/settings/users")


class OffboardingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    successor_user_id: UUID | None = None
    preview_version: str = Field(min_length=1, max_length=128)
    revoke_only: bool = False


def _target_error(request, actor_id, actor_roles, target, user_id):
    if target is None:
        return problem(request, 404, "user_not_found", "The user was not found.")
    if actor_id == user_id:
        return problem(request, 409, "self_offboarding_forbidden", "You cannot remove your own account.")
    target_roles = {row.role for row in target.roles}
    allowed = INVITABLE_ROLES if "system_admin" in actor_roles else RECRUITING_ADMIN_INVITABLE_ROLES
    if not target_roles or not target_roles.issubset(allowed):
        return problem(request, 403, "role_assignment_forbidden", "The member cannot be managed by this administrator.")
    return None


def _successor(db, organization_id, source, successor_id):
    if successor_id is None:
        return None
    successor = db.scalar(select(User).where(User.organization_id == organization_id, User.id == successor_id))
    if successor is None or successor.id == source.id or successor.status != UserStatus.ACTIVE:
        return False
    return successor


def _record(db, organization_id, user_id):
    return db.scalar(select(MemberOffboarding).where(MemberOffboarding.organization_id == organization_id, MemberOffboarding.user_id == user_id))


def _unresolved_refs(items):
    # Keep durable routing facts, not another copy of candidate names/content
    # that would escape the existing candidate-redaction boundary.
    fields = ("category", "resource_type", "resource_id", "reason_code")
    return [{key: item[key] for key in fields if key in item} for item in items]


def _version(source, successor, record, plan):
    value = {
        "user": str(source.id), "authorization_version": source.authorization_version,
        "roles": sorted(row.role for row in source.roles), "status": source.status.value,
        "record": record.version if record else 0,
        "successor": str(successor.id) if successor else None,
        "successor_version": successor.authorization_version if successor else None,
        "successor_status": successor.status.value if successor else None,
        "plan": plan["fingerprint"],
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _detail(db, source, successor, record, plan):
    rows = db.scalars(select(User).where(User.organization_id == source.organization_id, User.id != source.id).order_by(User.display_name, User.id)).all()
    return {
        "user_id": str(source.id), "user_name": source.display_name,
        "removed_at": record.removed_at.isoformat() if record else None,
        "successor_user_id": str(successor.id) if successor else None,
        "handover_status": ("pending" if plan["items"] else "completed") if record else "not_started",
        "preview_version": _version(source, successor, record, plan),
        "counts": plan["counts"], "items": plan["items"],
        "successors": [{"id": str(row.id), "display_name": row.display_name,
            "eligible": row.status == UserStatus.ACTIVE,
            "reason": None if row.status == UserStatus.ACTIVE else "该成员未激活或已停用，不能接手。"} for row in rows],
    }


@router.get("/{user_id}/offboarding")
def preview_offboarding(user_id: UUID, request: Request, successor_id: UUID | None = None):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    from server.app.identity.offboarding_handover import build_handover_plan
    with request.app.state.identity_store.sync_session() as db:
        source = db.scalar(select(User).where(User.organization_id == principal.organization_id, User.id == user_id))
        error = _target_error(request, principal.user_id, principal.roles, source, user_id)
        if error is not None:
            return error
        record = _record(db, principal.organization_id, user_id)
        successor = (_successor(db, principal.organization_id, source, successor_id)
            if successor_id is not None else handover_destination(db, principal.organization_id, source.id) if record else None)
        if successor is False:
            # A saved destination can become inactive; do not hide the pending case.
            if successor_id is not None:
                return problem(request, 422, "offboarding_successor_invalid", "Select a different active member in this organization.")
            successor = None
        plan = build_handover_plan(db, principal.organization_id, source, successor, now=request.app.state.identity_service.clock.current_time())
        return {"data": _detail(db, source, successor, record, plan)}


@router.post("/{user_id}/offboarding")
def offboard_member(user_id: UUID, payload: OffboardingCommand, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    from server.app.identity.offboarding_handover import HandoverPlanChanged, apply_handover, build_handover_plan
    service = request.app.state.identity_service
    with request.app.state.identity_store.sync_session() as db:
        # Serialize member removals for last-administrator and successor-chain safety.
        db.scalar(select(Organization).where(Organization.id == principal.organization_id).with_for_update())
        record = _record(db, principal.organization_id, user_id)
        successor_id = payload.successor_user_id
        if successor_id is None and payload.revoke_only and record:
            destination = handover_destination(db, principal.organization_id, user_id)
            successor_id = destination.id if destination else None
        ids = {principal.user_id, user_id}
        if successor_id:
            ids.add(successor_id)
        users = db.scalars(select(User).where(User.organization_id == principal.organization_id, User.id.in_(ids)).order_by(User.id).with_for_update(of=User).execution_options(populate_existing=True)).all()
        actor = next((user for user in users if user.id == principal.user_id), None)
        if actor is None or actor.status != UserStatus.ACTIVE:
            return problem(request, 401, "authentication_required", "Authentication is required.")
        try:
            service._resolve_session(db, session_token(request))
        except InvalidSession:
            return problem(request, 401, "authentication_required", "Authentication is required.")
        roles = set(db.scalars(select(UserRole.role).where(UserRole.user_id == actor.id)))
        if not roles.intersection(ADMIN_ROLES):
            return problem(request, 403, "authorization_forbidden", "The action is not permitted.")
        source = next((user for user in users if user.id == user_id), None)
        error = _target_error(request, actor.id, roles, source, user_id)
        if error is not None:
            return error
        if any(row.role == "system_admin" for row in source.roles) and source.status == UserStatus.ACTIVE:
            count = db.scalar(select(func.count(User.id)).where(User.organization_id == principal.organization_id, User.status == UserStatus.ACTIVE, User.roles.any(UserRole.role == "system_admin")))
            if count <= 1:
                return problem(request, 409, "last_system_admin_required", "The last active system administrator cannot be removed.")
        successor = _successor(db, principal.organization_id, source, successor_id)
        if successor is False and payload.revoke_only and payload.successor_user_id is None:
            successor = None
        if successor is False or (successor is None and not payload.revoke_only):
            return problem(request, 422, "offboarding_successor_invalid", "Select a different active member or explicitly revoke access only.")
        now = service.clock.current_time()
        plan = build_handover_plan(db, principal.organization_id, source, successor, now=now, lock=True)
        if payload.preview_version != _version(source, successor, record, plan):
            return problem(request, 409, "offboarding_preview_changed", "Responsibilities changed. Refresh the preview before continuing.")
        first_removal = record is None
        if first_removal:
            record = MemberOffboarding(organization_id=source.organization_id, user_id=source.id,
                successor_user_id=successor.id if successor and not payload.revoke_only else None, removed_by=actor.id, removed_at=now,
                updated_at=now, version=1, status="pending", unresolved=_unresolved_refs(plan["items"]))
            db.add(record)
            source.status = UserStatus.DISABLED
            source.authorization_version += 1
            db.execute(update(UserSession).where(UserSession.organization_id == source.organization_id,
                UserSession.user_id == source.id, UserSession.revoked_at.is_(None)).values(revoked_at=now, revocation_reason="member_offboarded"))
            db.execute(update(PasswordInvitation).where(PasswordInvitation.organization_id == source.organization_id,
                PasswordInvitation.user_id == source.id, PasswordInvitation.used_at.is_(None), PasswordInvitation.expires_at > now).values(expires_at=now))
            db.add(AuditLog(organization_id=source.organization_id, actor_user_id=actor.id, category="system",
                event_type="identity.member_offboarded", outcome="success", resource_type="user",
                resource_id=source.id, trace_id=request.state.trace_id, metadata_json={"successor_user_id": str(record.successor_user_id) if record.successor_user_id else None}))
        elif successor is not None and not payload.revoke_only:
            record.successor_user_id = successor.id
        transferred = 0
        handover_error = None
        if not payload.revoke_only:
            # A domain write failure must not roll back the access revocation.
            try:
                with db.begin_nested():
                    # Intent changes the resolution of queued producers; domain
                    # rows remain locked from the validated pre-removal plan.
                    action_plan = build_handover_plan(db, principal.organization_id, source, successor, now=now, lock=True)
                    transferred = apply_handover(db, action_plan, actor_user_id=actor.id, trace_id=request.state.trace_id, now=now)
                    db.flush()
            except (SQLAlchemyError, HandoverPlanChanged):
                transferred = 0
                handover_error = "账号已收回；事项交接暂未完成，请刷新后重试。"
        db.flush()
        if payload.revoke_only:
            successor = handover_destination(db, principal.organization_id, source.id)
        remaining = build_handover_plan(db, principal.organization_id, source, successor, now=now)
        record.unresolved = _unresolved_refs(remaining["items"])
        record.status = "pending" if remaining["items"] else "completed"
        record.updated_at = now
        if not first_removal:
            record.version += 1
        if not payload.revoke_only:
            db.add(AuditLog(organization_id=source.organization_id, actor_user_id=actor.id, category="system",
                event_type="identity.member_handover", outcome="failure" if handover_error else "success", resource_type="user",
                resource_id=source.id, trace_id=request.state.trace_id, metadata_json={"successor_user_id": str(successor.id),
                    "transferred_count": transferred, "pending_count": len(remaining["items"])}))
        db.commit()
        data = _detail(db, source, successor, record, remaining)
        data["transferred_count"] = transferred
        if handover_error:
            data["handover_error"] = handover_error
        return {"data": data}
