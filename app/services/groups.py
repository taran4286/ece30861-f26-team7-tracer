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
                # A 404, as the spec's NOT_FOUND covers unknown incidents; the field names the ID.
                raise TracerError(
                    ErrorCode.NOT_FOUND, f"Incident {incident_id!r} not found",
                    f"incident_ids[{index}]",
                )
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


class GroupedMeanwhile(Exception):
    """The incident joined a group between its first read and its lock. Nothing was written;
    the caller runs its whole transaction again, which then locks the group."""


def lock_with_group(tx: Session, incident: Incident) -> list[Incident]:
    """Lock the incident and, when it's grouped, every other member, in incident_id order.

    Returns the members in that order, or the incident alone when it isn't grouped. Load the
    incident first with load_incident, without locking it. Raises GroupedMeanwhile when the
    incident was grouped after that load.
    """
    if incident.group_id is None:
        tx.refresh(incident, with_for_update=True)
        if incident.group_id is None:
            return [incident]
        # Waiting for the other members while holding this one's lock could deadlock with a
        # request that locked them in order, so roll back and let the caller start again.
        raise GroupedMeanwhile(incident.incident_id)
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
    # One statement, so a group created while this runs is either wholly in it or not at all.
    rows = tx.execute(
        select(IncidentGroup.group_id, Incident.incident_id)
        .outerjoin(Incident, Incident.group_id == IncidentGroup.group_id)
        .where(IncidentGroup.session_id == session_id)
        .order_by(IncidentGroup.group_id, Incident.incident_id)
    )
    members: dict[str, list[str]] = {}
    for group_id, incident_id in rows:
        members.setdefault(group_id, [])
        if incident_id is not None:
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
