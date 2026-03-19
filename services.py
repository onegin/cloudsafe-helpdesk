from __future__ import annotations

from typing import Iterable

from flask import current_app
from sqlalchemy import and_, false, or_

from models import (
    OperatorOrganizationAccess,
    Organization,
    Priority,
    Roles,
    Task,
    TaskComment,
    TaskHistory,
    User,
    db,
)
from notifications import notify_user


def priority_choices() -> list[tuple[str, str]]:
    return [(value, Priority.LABELS[value]) for value in Priority.ALL]


def get_accessible_organization_ids(user: User) -> set[int]:
    if user.role == Roles.ADMIN:
        ids = Organization.query.with_entities(Organization.id).all()
        return {row[0] for row in ids}

    if user.role == Roles.CLIENT:
        return {user.organization_id} if user.organization_id else set()

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

    if user.role == Roles.CLIENT:
        if user.organization_id:
            org = Organization.query.get(user.organization_id)
            return [org] if org else []
        return []

    org_ids = get_accessible_organization_ids(user)
    if not org_ids:
        return []

    return Organization.query.filter(Organization.id.in_(org_ids)).order_by(Organization.name.asc()).all()


def can_access_organization(user: User, organization_id: int | None) -> bool:
    if not organization_id:
        return False
    return organization_id in get_accessible_organization_ids(user)


def allowed_clients_for_user(user: User, organization_id: int | None = None) -> list[User]:
    if user.role == Roles.CLIENT:
        if not user.organization_id:
            return []
        if organization_id and organization_id != user.organization_id:
            return []
        return [user]

    query = User.query.filter_by(role=Roles.CLIENT, active=True)

    if user.role == Roles.ADMIN:
        if organization_id:
            query = query.filter_by(organization_id=organization_id)
        return query.order_by(User.username.asc()).all()

    org_ids = get_accessible_organization_ids(user)
    if organization_id is not None:
        if organization_id not in org_ids:
            return []
        org_ids = {organization_id}

    if not org_ids:
        return []

    return (
        query.filter(User.organization_id.in_(org_ids))
        .order_by(User.username.asc())
        .all()
    )


def can_access_client(user: User, client_id: int) -> bool:
    client = User.query.filter_by(id=client_id, role=Roles.CLIENT, active=True).first()
    if not client:
        return False

    if user.role == Roles.ADMIN:
        return True

    if user.role == Roles.CLIENT:
        return user.id == client.id

    if user.role == Roles.OPERATOR:
        return bool(client.organization_id and can_access_organization(user, client.organization_id))

    return False


def filter_tasks_for_user(query, user: User):
    if user.role == Roles.ADMIN:
        return query

    if user.role == Roles.OPERATOR:
        org_ids = get_accessible_organization_ids(user)
        if not org_ids:
            return query.filter(false())
        return query.filter(Task.organization_id.in_(org_ids))

    if user.role == Roles.CLIENT:
        if not user.organization_id:
            return query.filter(false())
        return query.filter(
            or_(
                Task.client_id == user.id,
                and_(Task.client_id.is_(None), Task.organization_id == user.organization_id),
            )
        )

    return query.filter(false())


def can_view_task(user: User, task: Task) -> bool:
    if user.role == Roles.ADMIN:
        return True

    if user.role == Roles.OPERATOR:
        return can_access_organization(user, task.organization_id)

    if user.role == Roles.CLIENT:
        if task.client_id == user.id:
            return True
        return bool(
            user.organization_id
            and task.organization_id == user.organization_id
            and task.client_id is None
        )

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

    return None, "Клиент не может назначать ответственного"


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


def _dedupe_users(users: Iterable[User], *, exclude_user_id: int | None = None) -> list[User]:
    seen: set[int] = set()
    result: list[User] = []

    for user in users:
        if not user or not user.active:
            continue
        if exclude_user_id and user.id == exclude_user_id:
            continue
        if user.id in seen:
            continue
        seen.add(user.id)
        result.append(user)

    return result


def admin_users() -> list[User]:
    return User.query.filter_by(role=Roles.ADMIN, active=True).order_by(User.id.asc()).all()


def collect_new_task_recipients(task: Task) -> list[User]:
    recipients: list[User] = []
    recipients.extend(admin_users())
    if task.client:
        recipients.append(task.client)
    if task.assigned_to:
        recipients.append(task.assigned_to)
    return _dedupe_users(recipients)


def collect_comment_recipients(task: Task, author: User) -> list[User]:
    recipients: list[User] = []
    recipients.extend(admin_users())

    if author.role in (Roles.ADMIN, Roles.OPERATOR) and task.client:
        recipients.append(task.client)

    if author.role in (Roles.CLIENT, Roles.OPERATOR):
        recipients.extend(operators_for_organization(task.organization_id))

    if author.role == Roles.CLIENT and task.assigned_to:
        recipients.append(task.assigned_to)

    return _dedupe_users(recipients, exclude_user_id=author.id)


def dispatch_notifications(recipients: Iterable[User], subject: str, body: str) -> int:
    sent = 0
    for user in _dedupe_users(recipients):
        if notify_user(user, subject, body):
            sent += 1
    current_app.logger.info("Notifications sent=%s subject=%s", sent, subject)
    return sent


def task_notification_text(task: Task, task_url: str, intro: str) -> str:
    preview = (task.content or "").strip()
    if len(preview) > 500:
        preview = preview[:500] + "..."

    client_label = task.client.username if task.client else "Все сотрудники"
    organization_label = task.organization.name if task.organization else "-"

    return (
        f"<b>{intro}</b>\n"
        f"<b>Задача:</b> {task.theme}\n"
        f"<b>Организация:</b> {organization_label}\n"
        f"<b>Сотрудник:</b> {client_label}\n"
        f"<b>Приоритет:</b> {task.priority_label}\n"
        f"<b>Срок:</b> {task.due_date}\n"
        f"<b>Содержимое:</b> {preview}\n"
        f"<b>Ссылка:</b> {task_url}"
    )


def comment_notification_text(comment: TaskComment, task_url: str, intro: str) -> str:
    preview = (comment.content or "").strip()
    if len(preview) > 500:
        preview = preview[:500] + "..."

    client_label = comment.task.client.username if comment.task.client else "Все сотрудники"
    organization_label = comment.task.organization.name if comment.task.organization else "-"

    return (
        f"<b>{intro}</b>\n"
        f"<b>Задача:</b> {comment.task.theme}\n"
        f"<b>Организация:</b> {organization_label}\n"
        f"<b>Сотрудник:</b> {client_label}\n"
        f"<b>Автор комментария:</b> {comment.author.username}\n"
        f"<b>Комментарий:</b> {preview}\n"
        f"<b>Ссылка:</b> {task_url}"
    )
