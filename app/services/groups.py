"""Link related incidents into groups (REQ-04), and the group lookups determinations and the
snapshot use."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Incident, IncidentGroup, State, new_id
from app.roles import Operation, Role, require_role
from app.services.validation import invalid, require_object
from app.sessions import load_session


def create_group(
    body: Any, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    """POST /groups: link two or more UnderAssessment incidents that aren't grouped yet."""
    with transaction() as tx:
        load_session(tx, session_id)
        require_role(role, Operation.CREATE_GROUP)
        incident_ids = _validate(body)
        # Locked in incident_id order, as lock_with_group locks, so requests on overlapping
        # incidents wait for each other instead of deadlocking.
        members = tx.scalars(
            select(Incident)
            .where(Incident.session_id == session_id, Incident.incident_id.in_(incident_ids))
            .order_by(Incident.incident_id)
            .with_for_update()
        ).all()
        found = {member.incident_id for member in members}
        for index, incident_id in enumerate(incident_ids):
            if incident_id not in found:
                raise invalid(f"incident_ids[{index}]", f"Incident {incident_id!r} not found")
        for member in members:
            if member.group_id is not None:
                raise TracerError(
                    ErrorCode.CONFLICT,
                    f"Incident {member.incident_id} is already in group {member.group_id}",
                )
            if member.state != State.UNDER_ASSESSMENT:
                raise TracerError(
                    ErrorCode.CONFLICT,
                    f"Incident {member.incident_id} is {member.state.value}; only UnderAssessment "
                    "incidents can be grouped",
                )

        group = IncidentGroup(group_id=new_id("grp"), session_id=session_id)
        tx.add(group)
        member_ids = [member.incident_id for member in members]
        for member in members:
            before = {"group_id": None, "version": member.version}
            member.group_id = group.group_id
            member.version += 1
            after = {"group_id": group.group_id, "incident_ids": member_ids, "version": member.version}
            # One entry per member, so each incident's own trail shows its group and version.
            audit(
                tx, AuditAction.CREATE_GROUP, member.incident_id, before, after,
                user_id=user_id, session_id=session_id,
            )
        return {"group_id": group.group_id, "incident_ids": member_ids}


def lock_with_group(tx: Session, incident: Incident) -> list[Incident]:
    """Lock the incident and, when it's grouped, every other member, in incident_id order.

    Returns the members in that order, or the incident alone when it isn't grouped. Load the
    incident first with load_incident, without locking it.
    """
    if incident.group_id is None:
        tx.refresh(incident, with_for_update=True)
        if incident.group_id is None:
            return [incident]
        # Grouped by a request that finished after the first read; lock the other members too.
    # A group's members are fixed when it's created, so this finds every one of them.
    return list(tx.scalars(
        select(Incident)
        .where(Incident.session_id == incident.session_id, Incident.group_id == incident.group_id)
        .order_by(Incident.incident_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ))


def groups_to_dicts(tx: Session, session_id: str) -> list[dict[str, Any]]:
    """The session's groups as the Group schema, ordered by group_id, members by incident_id."""
    members: dict[str, list[str]] = {
        group_id: [] for group_id in tx.scalars(
            select(IncidentGroup.group_id)
            .where(IncidentGroup.session_id == session_id)
            .order_by(IncidentGroup.group_id)
        )
    }
    for incident_id, group_id in tx.execute(
        select(Incident.incident_id, Incident.group_id)
        .where(Incident.session_id == session_id, Incident.group_id.is_not(None))
        .order_by(Incident.incident_id)
    ):
        members[group_id].append(incident_id)
    return [{"group_id": group_id, "incident_ids": ids} for group_id, ids in members.items()]


def _validate(body: Any) -> list[str]:
    """A GroupRequest: at least two unique incident IDs. Whether they exist is checked after."""
    body = require_object(body)
    incident_ids = body.get("incident_ids")
    if not isinstance(incident_ids, list) or len(incident_ids) < 2:
        raise invalid("incident_ids", "Expected an array of at least two incident IDs")
    for index, incident_id in enumerate(incident_ids):
        field = f"incident_ids[{index}]"
        # PostgreSQL text can't hold NUL, so no stored ID has one, and querying for it would fail.
        if not isinstance(incident_id, str) or not incident_id.strip() or "\x00" in incident_id:
            raise invalid(field, "Expected an incident ID")
        if incident_id in incident_ids[:index]:
            raise invalid(field, f"{incident_id} appears more than once")
    return list(incident_ids)
