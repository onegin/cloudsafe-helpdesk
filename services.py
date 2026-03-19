from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from flask import current_app
from sqlalchemy import false

from models import (
    Employee,
    OperatorOrganizationAccess,
    Organization,
    Priority,
    Roles,
    Setting,
    Task,
    TaskComment,
    TaskHistory,
    User,
    db,
)
from notifications import notify_contact


DEFAULT_SETTINGS: dict[str, str] = {
    "site_name": "CloudSafe HelpDesk",
    "primary_color": "#0d6efd",
    "secondary_color": "#0b5ed7",
    "background_color": "#f4f6f9",
    "logo_path": "",
    "favicon_path": "",
}


@dataclass
class Recipient:
    key: str
    name: str
    email: str | None
    telegram: str | None


def priority_choices() -> list[tuple[str, str]]:
    return [(value, Priority.LABELS[value]) for value in Priority.ALL]


def get_setting(key: str, default: str | None = None) -> str | None:
    row = Setting.query.filter_by(key=key).first()
    if row:
        return row.value
    if default is not None:
        return default
    return DEFAULT_SETTINGS.get(key)


def get_all_settings() -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    for row in Setting.query.all():
        values[row.key] = row.value or ""
    return values


def set_setting(key: str, value: str | None) -> None:
    row = Setting.query.filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))


def reset_settings_to_defaults() -> None:
    Setting.query.delete(synchronize_session=False)
    for key, value in DEFAULT_SETTINGS.items():
        db.session.add(Setting(key=key, value=value))


def get_accessible_organization_ids(user: User) -> set[int]:
    if user.role == Roles.ADMIN:
        ids = Organization.query.with_entities(Organization.id).all()
        return {row[0] for row in ids}

    if user.role == Roles.OPERATOR:
        ids = (
            OperatorOrganizationAccess.query.with_entities(OperatorOrganizationAccess.organization_id)
            .filter_by(operator_id=user.id)
            .all()
        )
        return {row[0] for row in ids}

    return set()


def allowed_organizations_for_user(user: User) -> list[Organization]:
    if user.role == Roles.ADMIN:
        return Organization.query.order_by(Organization.name.asc()).all()

    org_ids = get_accessible_organization_ids(user)
    if not org_ids:
        return []

    return (
        Organization.query.filter(Organization.id.in_(org_ids))
        .order_by(Organization.name.asc())
        .all()
    )


def can_access_organization(user: User, organization_id: int | None) -> bool:
    if not organization_id:
        return False
    return organization_id in get_accessible_organization_ids(user)


def allowed_employees_for_user(user: User, organization_id: int | None = None) -> list[Employee]:
    query = Employee.query.filter_by(is_active=True)

    if user.role == Roles.ADMIN:
        if organization_id:
            query = query.filter_by(organization_id=organization_id)
        return query.order_by(Employee.last_name.asc(), Employee.first_name.asc()).all()

    if user.role == Roles.OPERATOR:
        org_ids = get_accessible_organization_ids(user)
        if organization_id is not None:
            if organization_id not in org_ids:
                return []
            org_ids = {organization_id}

        if not org_ids:
            return []

        return (
            query.filter(Employee.organization_id.in_(org_ids))
            .order_by(Employee.last_name.asc(), Employee.first_name.asc())
            .all()
        )

    return []


def filter_tasks_for_user(query, user: User):
    if user.role == Roles.ADMIN:
        return query

    if user.role == Roles.OPERATOR:
        org_ids = get_accessible_organization_ids(user)
        if not org_ids:
            return query.filter(false())
        return query.filter(Task.organization_id.in_(org_ids))

    return query.filter(false())


def can_view_task(user: User, task: Task) -> bool:
    if user.role == Roles.ADMIN:
        return True

    if user.role == Roles.OPERATOR:
        return can_access_organization(user, task.organization_id)

    return False


def operators_for_organization(organization_id: int) -> list[User]:
    return (
        User.query.join(OperatorOrganizationAccess, OperatorOrganizationAccess.operator_id == User.id)
        .filter(
            User.role == Roles.OPERATOR,
            User.active.is_(True),
            OperatorOrganizationAccess.organization_id == organization_id,
        )
        .order_by(User.username.asc())
        .all()
    )


def allowed_assignees_for_actor(actor: User, organization_id: int | None) -> list[User]:
    if not organization_id:
        return []

    if actor.role == Roles.ADMIN:
        return operators_for_organization(organization_id)

    if actor.role == Roles.OPERATOR:
        return [actor] if can_access_organization(actor, organization_id) and actor.active else []

    return []


def resolve_assignee(actor: User, organization_id: int, assigned_to_raw) -> tuple[User | None, str | None]:
    if assigned_to_raw in (None, "", "0"):
        return None, None

    try:
        assigned_to_id = int(assigned_to_raw)
    except (TypeError, ValueError):
        return None, "Некорректный оператор в поле 'Ответственный'"

    operator = User.query.filter_by(id=assigned_to_id, role=Roles.OPERATOR, active=True).first()
    if not operator:
        return None, "Указанный ответственный оператор не найден"

    if actor.role == Roles.ADMIN:
        has_access = OperatorOrganizationAccess.query.filter_by(
            operator_id=operator.id,
            organization_id=organization_id,
        ).first()
        if not has_access:
            return None, "Оператор не имеет доступа к выбранной организации"
        return operator, None

    if actor.role == Roles.OPERATOR:
        if operator.id != actor.id:
            return None, "Оператор может назначать ответственным только себя"
        if not can_access_organization(actor, organization_id):
            return None, "Нет доступа к организации этой задачи"
        return operator, None

    return None, "Недостаточно прав"


def record_task_change(task: Task, changed_by: User | None, field_name: str, old_value, new_value) -> None:
    old_str = "" if old_value is None else str(old_value)
    new_str = "" if new_value is None else str(new_value)
    if old_str == new_str:
        return

    db.session.add(
        TaskHistory(
            task=task,
            changed_by=changed_by,
            field_name=field_name,
            old_value=old_str,
            new_value=new_str,
        )
    )


def _recipient_from_user(user: User) -> Recipient | None:
    if not user or not user.active:
        return None
    return Recipient(
        key=f"user:{user.id}",
        name=user.username,
        email=user.email,
        telegram=user.telegram_chat_id,
    )


def _recipient_from_employee(employee: Employee) -> Recipient | None:
    if not employee or not employee.is_active:
        return None
    return Recipient(
        key=f"employee:{employee.id}",
        name=employee.full_name,
        email=employee.email,
        telegram=employee.telegram,
    )


def _recipient_from_organization(organization: Organization) -> Recipient | None:
    if not organization or not organization.email:
        return None
    return Recipient(
        key=f"organization:{organization.id}",
        name=organization.name,
        email=organization.email,
        telegram=None,
    )


def _dedupe_recipients(recipients: Iterable[Recipient], *, exclude_key: str | None = None) -> list[Recipient]:
    seen: set[str] = set()
    result: list[Recipient] = []

    for recipient in recipients:
        if not recipient:
            continue
        if exclude_key and recipient.key == exclude_key:
            continue
        if recipient.key in seen:
            continue
        seen.add(recipient.key)
        result.append(recipient)

    return result


def admin_users() -> list[User]:
    return User.query.filter_by(role=Roles.ADMIN, active=True).order_by(User.id.asc()).all()


def admin_recipients() -> list[Recipient]:
    recipients = [_recipient_from_user(user) for user in admin_users()]
    return [r for r in recipients if r]


def collect_new_task_recipients(task: Task) -> list[Recipient]:
    recipients: list[Recipient] = []
    recipients.extend(admin_recipients())

    if task.employee:
        employee_recipient = _recipient_from_employee(task.employee)
        if employee_recipient:
            recipients.append(employee_recipient)
    else:
        org_recipient = _recipient_from_organization(task.organization)
        if org_recipient:
            recipients.append(org_recipient)

    if task.assigned_to:
        assigned_recipient = _recipient_from_user(task.assigned_to)
        if assigned_recipient:
            recipients.append(assigned_recipient)

    return _dedupe_recipients(recipients)


def collect_comment_recipients(task: Task, author: User) -> list[Recipient]:
    recipients: list[Recipient] = []
    recipients.extend(admin_recipients())

    if task.assigned_to:
        assigned = _recipient_from_user(task.assigned_to)
        if assigned:
            recipients.append(assigned)

    if task.employee:
        employee_recipient = _recipient_from_employee(task.employee)
        if employee_recipient:
            recipients.append(employee_recipient)
    else:
        org_recipient = _recipient_from_organization(task.organization)
        if org_recipient:
            recipients.append(org_recipient)

    return _dedupe_recipients(recipients, exclude_key=f"user:{author.id}")


def dispatch_notifications(recipients: Iterable[Recipient], subject: str, body: str) -> int:
    sent = 0
    for recipient in _dedupe_recipients(recipients):
        if notify_contact(
            email=recipient.email,
            telegram=recipient.telegram,
            subject=subject,
            body=body,
        ):
            sent += 1
    current_app.logger.info("Notifications sent=%s subject=%s", sent, subject)
    return sent


def task_notification_text(task: Task, task_url: str, intro: str) -> str:
    preview = (task.content or "").strip()
    if len(preview) > 500:
        preview = preview[:500] + "..."

    employee_label = task.employee.full_name if task.employee else "Общая задача"
    organization_label = task.organization.name if task.organization else "-"

    return (
        f"<b>{intro}</b>\n"
        f"<b>Задача:</b> {task.theme}\n"
        f"<b>Организация:</b> {organization_label}\n"
        f"<b>Сотрудник:</b> {employee_label}\n"
        f"<b>Приоритет:</b> {task.priority_label}\n"
        f"<b>Срок:</b> {task.due_date}\n"
        f"<b>Содержимое:</b> {preview}\n"
        f"<b>Ссылка:</b> {task_url}"
    )


def comment_notification_text(comment: TaskComment, task_url: str, intro: str) -> str:
    preview = (comment.content or "").strip()
    if len(preview) > 500:
        preview = preview[:500] + "..."

    employee_label = comment.task.employee.full_name if comment.task.employee else "Общая задача"
    organization_label = comment.task.organization.name if comment.task.organization else "-"

    return (
        f"<b>{intro}</b>\n"
        f"<b>Задача:</b> {comment.task.theme}\n"
        f"<b>Организация:</b> {organization_label}\n"
        f"<b>Сотрудник:</b> {employee_label}\n"
        f"<b>Автор комментария:</b> {comment.author.username}\n"
        f"<b>Комментарий:</b> {preview}\n"
        f"<b>Ссылка:</b> {task_url}"
    )
