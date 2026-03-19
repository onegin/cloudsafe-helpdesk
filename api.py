from __future__ import annotations

from flask import Blueprint, jsonify, request, url_for

from forms import ValidationError, Validators
from models import ApiToken, Roles, Status, StatusHistory, Task, User, db
from services import (
    can_access_client,
    collect_new_task_recipients,
    dispatch_notifications,
    record_task_change,
    resolve_assignee,
    task_notification_text,
)


api_bp = Blueprint("api", __name__, url_prefix="/api")


def _error(message: str, status_code: int = 400, *, details: str | None = None):
    payload = {"error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _get_token_user() -> User | None:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    raw_token = authorization.split(" ", 1)[1].strip()
    return ApiToken.resolve_user(raw_token)


def _resolve_client_from_payload(payload: dict, actor: User) -> User | None:
    if actor.role == Roles.CLIENT:
        requested_id = payload.get("client_id")
        requested_email = (payload.get("client_email") or "").strip()
        requested_username = (payload.get("client_username") or "").strip()

        if requested_id:
            try:
                if int(requested_id) != actor.id:
                    return None
            except (TypeError, ValueError):
                return None
        if requested_email and requested_email != actor.email and requested_email != actor.username:
            return None
        if requested_username and requested_username != actor.username:
            return None
        return actor

    client_id = payload.get("client_id")
    client_email = (payload.get("client_email") or "").strip()
    client_username = (payload.get("client_username") or "").strip()

    query = User.query.filter_by(role=Roles.CLIENT, active=True)
    client = None
    if client_id is not None:
        try:
            client_id = int(client_id)
        except (TypeError, ValueError):
            return None
        client = query.filter_by(id=client_id).first()
    elif client_email:
        client = query.filter((User.email == client_email) | (User.username == client_email)).first()
    elif client_username:
        client = query.filter_by(username=client_username).first()

    if not client:
        return None

    if actor.role == Roles.OPERATOR and not can_access_client(actor, client.id):
        return None

    return client


@api_bp.route("/tasks", methods=["POST"])
def create_task_api():
    token_user = _get_token_user()
    if not token_user:
        return _error("Unauthorized", 401, details="Invalid or missing Bearer token")

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("Invalid JSON body", 400)

    try:
        cleaned = Validators.task_payload(payload)
    except ValidationError as exc:
        return _error("Validation error", 400, details=str(exc))

    client = _resolve_client_from_payload(payload, token_user)
    if not client:
        if token_user.role == Roles.CLIENT:
            return _error(
                "Forbidden",
                403,
                details="Client token can create tasks only for the owner",
            )
        return _error(
            "Validation error",
            400,
            details="Provide existing and accessible client_id or client_email/client_username",
        )

    initial_status = Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).first()
    if not initial_status:
        return _error("Configuration error", 500, details="No statuses configured")

    assignee, assignee_error = resolve_assignee(token_user, client.id, cleaned.get("assigned_to_id"))
    if assignee_error:
        if token_user.role == Roles.CLIENT:
            return _error("Forbidden", 403, details=assignee_error)
        return _error("Validation error", 400, details=assignee_error)

    task = Task(
        theme=cleaned["theme"],
        content=cleaned["content"],
        due_date=cleaned["due_date"],
        priority=cleaned["priority"],
        client=client,
        status=initial_status,
        created_by=token_user,
        assigned_to=assignee,
    )
    db.session.add(task)
    db.session.flush()

    db.session.add(
        StatusHistory(
            task=task,
            old_status_id=None,
            new_status_id=initial_status.id,
            changed_by=token_user,
        )
    )

    record_task_change(task, token_user, "theme", None, task.theme)
    record_task_change(task, token_user, "content", None, task.content)
    record_task_change(task, token_user, "client", None, task.client.username)
    record_task_change(task, token_user, "due_date", None, task.due_date)
    record_task_change(task, token_user, "priority", None, task.priority_label)
    record_task_change(
        task,
        token_user,
        "assigned_to",
        None,
        task.assigned_to.username if task.assigned_to else "-",
    )

    db.session.commit()

    task_url = url_for("task_detail", task_id=task.id, _external=True)
    subject = f"[Helpdesk] Новая задача #{task.id}"
    body = task_notification_text(task, task_url, "Создана новая задача")
    dispatch_notifications(collect_new_task_recipients(task), subject, body)

    response = {
        "id": task.id,
        "theme": task.theme,
        "content": task.content,
        "due_date": task.due_date.isoformat(),
        "priority": task.priority,
        "priority_label": task.priority_label,
        "archived": task.archived,
        "created_at": task.created_at.isoformat(),
        "status": {"id": initial_status.id, "name": initial_status.name},
        "client": {"id": client.id, "username": client.username, "email": client.email},
        "assigned_to": (
            {
                "id": task.assigned_to.id,
                "username": task.assigned_to.username,
            }
            if task.assigned_to
            else None
        ),
        "url": task_url,
    }
    return jsonify(response), 201
