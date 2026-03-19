from __future__ import annotations

from typing import Iterable

from flask import current_app
from sqlalchemy import false

from models import (
    ClientAccess,
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


def get_accessible_client_ids(user: User) -> set[int]:
    if user.role == Roles.ADMIN:
        ids = User.query.with_entities(User.id).filter_by(role=Roles.CLIENT, active=True).all()
        return {row[0] for row in ids}
    if user.role == Roles.CLIENT:
        return {user.id}
    if user.role == Roles.OPERATOR:
        ids = (
            ClientAccess.query.with_entities(ClientAccess.client_id)
            .filter_by(operator_id=user.id)
            .all()
        )
        return {row[0] for row in ids}
    return set()


def allowed_clients_for_user(user: User) -> list[User]:
    if user.role == Roles.CLIENT:
        return [user]

    query = User.query.filter_by(role=Roles.CLIENT, active=True)
    if user.role == Roles.ADMIN:
        return query.order_by(User.username.asc()).all()

    client_ids = get_accessible_client_ids(user)
    if not client_ids:
        return []
    return query.filter(User.id.in_(client_ids)).order_by(User.username.asc()).all()


def can_access_client(user: User, client_id: int) -> bool:
    return client_id in get_accessible_client_ids(user)


def filter_tasks_for_user(query, user: User):
    if user.role == Roles.ADMIN:
        return query
    if user.role == Roles.CLIENT:
        return query.filter(Task.client_id == user.id)

    client_ids = get_accessible_client_ids(user)
    if not client_ids:
        return query.filter(false())
    return query.filter(Task.client_id.in_(client_ids))


def can_view_task(user: User, task: Task) -> bool:
    if user.role == Roles.ADMIN:
        return True
    if user.role == Roles.CLIENT:
        return task.client_id == user.id
    if user.role == Roles.OPERATOR:
        return task.client_id in get_accessible_client_ids(user)
    return False


def operators_for_client(client_id: int) -> list[User]:
    return (
        User.query.join(ClientAccess, ClientAccess.operator_id == User.id)
        .filter(
            User.role == Roles.OPERATOR,
            User.active.is_(True),
            ClientAccess.client_id == client_id,
        )
        .order_by(User.username.asc())
        .all()
    )


def allowed_assignees_for_actor(actor: User, client_id: int) -> list[User]:
    if actor.role == Roles.ADMIN:
        return operators_for_client(client_id)
    if actor.role == Roles.OPERATOR:
        return [actor] if can_access_client(actor, client_id) and actor.active else []
    return []


def resolve_assignee(actor: User, client_id: int, assigned_to_raw) -> tuple[User | None, str | None]:
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
        has_access = ClientAccess.query.filter_by(
            operator_id=operator.id,
            client_id=client_id,
        ).first()
        if not has_access:
            return None, "Оператор не имеет доступа к выбранному клиенту"
        return operator, None

    if actor.role == Roles.OPERATOR:
        if operator.id != actor.id:
            return None, "Оператор может назначать ответственным только себя"
        if not can_access_client(actor, client_id):
            return None, "Нет доступа к клиенту этой задачи"
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
    recipients.append(task.client)
    if task.assigned_to:
        recipients.append(task.assigned_to)
    return _dedupe_users(recipients)


def collect_comment_recipients(task: Task, author: User) -> list[User]:
    recipients: list[User] = []
    recipients.extend(admin_users())

    if author.role in (Roles.ADMIN, Roles.OPERATOR):
        recipients.append(task.client)

    if author.role in (Roles.CLIENT, Roles.OPERATOR):
        recipients.extend(operators_for_client(task.client_id))

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

    return (
        f"<b>{intro}</b>\n"
        f"<b>Задача:</b> {task.theme}\n"
        f"<b>Клиент:</b> {task.client.username}\n"
        f"<b>Приоритет:</b> {task.priority_label}\n"
        f"<b>Срок:</b> {task.due_date}\n"
        f"<b>Содержимое:</b> {preview}\n"
        f"<b>Ссылка:</b> {task_url}"
    )


def comment_notification_text(comment: TaskComment, task_url: str, intro: str) -> str:
    preview = (comment.content or "").strip()
    if len(preview) > 500:
        preview = preview[:500] + "..."

    return (
        f"<b>{intro}</b>\n"
        f"<b>Задача:</b> {comment.task.theme}\n"
        f"<b>Автор комментария:</b> {comment.author.username}\n"
        f"<b>Комментарий:</b> {preview}\n"
        f"<b>Ссылка:</b> {task_url}"
    )
