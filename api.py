from __future__ import annotations

from flask import Blueprint, jsonify, request, url_for

from forms import ValidationError, Validators
from models import (
    Employee,
    OperatorOrganizationAccess,
    Organization,
    Roles,
    Status,
    StatusHistory,
    Task,
    User,
    db,
)
from services import (
    collect_new_task_recipients,
    dispatch_notifications,
    record_task_change,
    task_notification_text,
)


api_bp = Blueprint("api", __name__, url_prefix="/api")


def _error(message: str, status_code: int = 400, *, details: str | None = None):
    payload = {"error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _get_token_organization() -> Organization | None:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None

    raw_token = authorization.split(" ", 1)[1].strip()
    return Organization.resolve_by_token(raw_token)


def _resolve_assignee(organization_id: int, assigned_to_raw):
    assigned_to_id = Validators.parse_optional_int(assigned_to_raw, "assigned_to_id")
    if assigned_to_id is None:
        return None, None

    operator = User.query.filter_by(id=assigned_to_id, role=Roles.OPERATOR, active=True).first()
    if not operator:
        return None, "Указанный оператор не найден"

    has_access = OperatorOrganizationAccess.query.filter_by(
        operator_id=operator.id,
        organization_id=organization_id,
    ).first()
    if not has_access:
        return None, "Оператор не имеет доступа к выбранной организации"

    return operator, None


def _api_actor_user() -> User | None:
    admin = User.query.filter_by(role=Roles.ADMIN, active=True).order_by(User.id.asc()).first()
    if admin:
        return admin
    return User.query.filter(User.role.in_(Roles.INTERNAL), User.active.is_(True)).order_by(User.id.asc()).first()


@api_bp.route("/tasks", methods=["POST"])
def create_task_api():
    organization = _get_token_organization()
    if not organization:
        return _error("Unauthorized", 401, details="Invalid or missing Bearer token")

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("Invalid JSON body", 400)

    try:
        cleaned = Validators.task_payload(payload, require_organization=False)
    except ValidationError as exc:
        return _error("Validation error", 400, details=str(exc))

    if cleaned.get("organization_id") and cleaned["organization_id"] != organization.id:
        return _error(
            "Forbidden",
            403,
            details="Token can create tasks only for bound organization",
        )

    employee = None
    if cleaned.get("employee_id"):
        employee = Employee.query.filter_by(
            id=cleaned["employee_id"],
            organization_id=organization.id,
            is_active=True,
        ).first()
        if not employee:
            return _error(
                "Validation error",
                400,
                details="Сотрудник не найден или не принадлежит организации",
            )

    assignee, assignee_error = _resolve_assignee(organization.id, cleaned.get("assigned_to_id"))
    if assignee_error:
        return _error("Validation error", 400, details=assignee_error)

    initial_status = Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).first()
    if not initial_status:
        return _error("Configuration error", 500, details="No statuses configured")

    actor = _api_actor_user()
    if not actor:
        return _error("Configuration error", 500, details="No active internal user found")

    task = Task(
        theme=cleaned["theme"],
        content=cleaned["content"],
        due_date=cleaned["due_date"],
        priority=cleaned["priority"],
        organization=organization,
        employee=employee,
        status=initial_status,
        created_by=actor,
        assigned_to=assignee,
    )
    db.session.add(task)
    db.session.flush()

    db.session.add(
        StatusHistory(
            task=task,
            old_status_id=None,
            new_status_id=initial_status.id,
            changed_by=None,
        )
    )

    record_task_change(task, None, "theme", None, task.theme)
    record_task_change(task, None, "content", None, task.content)
    record_task_change(task, None, "organization", None, task.organization.name)
    record_task_change(task, None, "employee", None, task.target_label)
    record_task_change(task, None, "due_date", None, task.due_date)
    record_task_change(task, None, "priority", None, task.priority_label)
    record_task_change(
        task,
        None,
        "assigned_to",
        None,
        task.assigned_to.username if task.assigned_to else "-",
    )

    db.session.commit()

    task_url = url_for("task_detail", task_id=task.id, _external=True)
    subject = f"[Helpdesk] Новая задача #{task.id}"
    body = task_notification_text(task, task_url, "Создана новая задача (API)")
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
        "organization": {
            "id": organization.id,
            "name": organization.name,
        },
        "employee": (
            {
                "id": task.employee.id,
                "name": task.employee.full_name,
                "email": task.employee.email,
            }
            if task.employee
            else None
        ),
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
