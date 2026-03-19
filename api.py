from __future__ import annotations

from flask import Blueprint, jsonify, request

from forms import ValidationError, Validators
from models import ApiToken, Roles, Status, StatusHistory, Task, User, db


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
        if requested_email and requested_email != actor.username:
            return None
        if requested_username and requested_username != actor.username:
            return None
        return actor

    client_id = payload.get("client_id")
    client_email = (payload.get("client_email") or "").strip()
    client_username = (payload.get("client_username") or "").strip()

    query = User.query.filter_by(role=Roles.CLIENT, active=True)
    if client_id is not None:
        try:
            client_id = int(client_id)
        except (TypeError, ValueError):
            return None
        return query.filter_by(id=client_id).first()

    if client_email:
        return query.filter_by(username=client_email).first()

    if client_username:
        return query.filter_by(username=client_username).first()

    return None


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
            details="Provide existing client_id or client_email/client_username",
        )

    initial_status = Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).first()
    if not initial_status:
        return _error("Configuration error", 500, details="No statuses configured")

    task = Task(
        theme=cleaned["theme"],
        content=cleaned["content"],
        due_date=cleaned["due_date"],
        client=client,
        status=initial_status,
        created_by=token_user,
    )
    db.session.add(task)
    db.session.flush()

    history = StatusHistory(
        task=task,
        old_status_id=None,
        new_status_id=initial_status.id,
        changed_by=token_user,
    )
    db.session.add(history)
    db.session.commit()

    response = {
        "id": task.id,
        "theme": task.theme,
        "content": task.content,
        "due_date": task.due_date.isoformat(),
        "archived": task.archived,
        "created_at": task.created_at.isoformat(),
        "status": {"id": initial_status.id, "name": initial_status.name},
        "client": {"id": client.id, "username": client.username},
    }
    return jsonify(response), 201
