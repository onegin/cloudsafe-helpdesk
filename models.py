from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()


class Roles:
    ADMIN = "admin"
    OPERATOR = "operator"
    CLIENT = "client"

    ALL = (ADMIN, OPERATOR, CLIENT)


class Priority:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    ALL = (LOW, MEDIUM, HIGH)
    LABELS = {
        LOW: "Низкий",
        MEDIUM: "Средний",
        HIGH: "Высокий",
    }


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Roles.CLIENT, index=True)

    email = db.Column(db.String(255), nullable=True, index=True)
    telegram_chat_id = db.Column(db.String(64), nullable=True)

    active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    tasks = db.relationship(
        "Task",
        back_populates="client",
        foreign_keys="Task.client_id",
        lazy="dynamic",
    )
    created_tasks = db.relationship(
        "Task",
        back_populates="created_by",
        foreign_keys="Task.created_by_id",
        lazy="dynamic",
    )
    assigned_tasks = db.relationship(
        "Task",
        back_populates="assigned_to",
        foreign_keys="Task.assigned_to_id",
        lazy="dynamic",
    )

    status_changes = db.relationship(
        "StatusHistory",
        back_populates="changed_by",
        foreign_keys="StatusHistory.changed_by_id",
        lazy="dynamic",
    )
    field_changes = db.relationship(
        "TaskHistory",
        back_populates="changed_by",
        foreign_keys="TaskHistory.changed_by_id",
        lazy="dynamic",
    )
    comments = db.relationship(
        "TaskComment",
        back_populates="author",
        foreign_keys="TaskComment.user_id",
        lazy="dynamic",
    )

    operator_access = db.relationship(
        "ClientAccess",
        back_populates="operator",
        foreign_keys="ClientAccess.operator_id",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    client_access = db.relationship(
        "ClientAccess",
        back_populates="client",
        foreign_keys="ClientAccess.client_id",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    api_tokens = db.relationship(
        "ApiToken",
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="ApiToken.created_at.desc()",
        lazy="dynamic",
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.active


class ClientAccess(db.Model):
    __tablename__ = "client_access"

    id = db.Column(db.Integer, primary_key=True)
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    operator = db.relationship("User", foreign_keys=[operator_id], back_populates="operator_access")
    client = db.relationship("User", foreign_keys=[client_id], back_populates="client_access")

    __table_args__ = (
        db.UniqueConstraint("operator_id", "client_id", name="uq_operator_client_access"),
    )


class Status(db.Model):
    __tablename__ = "statuses"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    tasks = db.relationship("Task", back_populates="status", lazy="dynamic")


class Task(db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.Integer, primary_key=True)
    theme = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    due_date = db.Column(db.Date, nullable=False, index=True)

    priority = db.Column(db.String(20), nullable=False, default=Priority.MEDIUM, index=True)

    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    status_id = db.Column(db.Integer, db.ForeignKey("statuses.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    archived = db.Column(db.Boolean, nullable=False, default=False, index=True)
    archived_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    client = db.relationship("User", back_populates="tasks", foreign_keys=[client_id])
    status = db.relationship("Status", back_populates="tasks")
    created_by = db.relationship("User", back_populates="created_tasks", foreign_keys=[created_by_id])
    assigned_to = db.relationship("User", back_populates="assigned_tasks", foreign_keys=[assigned_to_id])

    history = db.relationship(
        "StatusHistory",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="StatusHistory.changed_at.desc()",
    )
    change_history = db.relationship(
        "TaskHistory",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskHistory.changed_at.desc()",
    )
    comments = db.relationship(
        "TaskComment",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskComment.created_at.asc()",
    )

    def is_overdue(self) -> bool:
        return not self.archived and self.due_date < date.today()

    @property
    def priority_label(self) -> str:
        return Priority.LABELS.get(self.priority, self.priority)


class StatusHistory(db.Model):
    __tablename__ = "status_history"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False, index=True)
    old_status_id = db.Column(db.Integer, db.ForeignKey("statuses.id"), nullable=True)
    new_status_id = db.Column(db.Integer, db.ForeignKey("statuses.id"), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    changed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    task = db.relationship("Task", back_populates="history")
    old_status = db.relationship("Status", foreign_keys=[old_status_id])
    new_status = db.relationship("Status", foreign_keys=[new_status_id])
    changed_by = db.relationship("User", back_populates="status_changes", foreign_keys=[changed_by_id])


class TaskHistory(db.Model):
    __tablename__ = "task_history"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False, index=True)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    field_name = db.Column(db.String(120), nullable=False)
    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)
    changed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    task = db.relationship("Task", back_populates="change_history")
    changed_by = db.relationship("User", back_populates="field_changes", foreign_keys=[changed_by_id])


class TaskComment(db.Model):
    __tablename__ = "task_comments"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    task = db.relationship("Task", back_populates="comments")
    author = db.relationship("User", back_populates="comments", foreign_keys=[user_id])


class ApiToken(db.Model):
    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    token_prefix = db.Column(db.String(12), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked = db.Column(db.Boolean, nullable=False, default=False, index=True)

    user = db.relationship("User", back_populates="api_tokens")

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @classmethod
    def create_for_user(cls, user: User) -> tuple["ApiToken", str]:
        raw_token = secrets.token_urlsafe(32)
        token = cls(
            user=user,
            token_hash=cls.hash_token(raw_token),
            token_prefix=raw_token[:8],
        )
        db.session.add(token)
        return token, raw_token

    @classmethod
    def resolve_user(cls, raw_token: str) -> User | None:
        if not raw_token:
            return None
        token_hash = cls.hash_token(raw_token)
        token = (
            cls.query.filter_by(token_hash=token_hash, revoked=False)
            .join(User, cls.user_id == User.id)
            .filter(User.active.is_(True))
            .first()
        )
        if not token:
            return None
        token.last_used_at = datetime.utcnow()
        db.session.flush()
        return token.user


__all__ = [
    "db",
    "Roles",
    "Priority",
    "User",
    "ClientAccess",
    "Status",
    "Task",
    "StatusHistory",
    "TaskHistory",
    "TaskComment",
    "ApiToken",
]
