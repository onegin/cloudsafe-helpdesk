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


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Roles.CLIENT, index=True)
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
    status_changes = db.relationship(
        "StatusHistory",
        back_populates="changed_by",
        foreign_keys="StatusHistory.changed_by_id",
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

    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    status_id = db.Column(db.Integer, db.ForeignKey("statuses.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

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
    history = db.relationship(
        "StatusHistory",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="StatusHistory.changed_at.desc()",
    )

    def is_overdue(self) -> bool:
        return not self.archived and self.due_date < date.today()


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
    "User",
    "Status",
    "Task",
    "StatusHistory",
    "ApiToken",
]
