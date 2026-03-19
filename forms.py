from __future__ import annotations

from datetime import datetime
from typing import Any

from models import Priority, Roles


class ValidationError(ValueError):
    """Raised when input validation fails."""


class Validators:
    @staticmethod
    def parse_due_date(value: str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (TypeError, ValueError) as exc:
            raise ValidationError("Срок выполнения должен быть в формате YYYY-MM-DD") from exc

    @staticmethod
    def parse_email(value: str) -> str:
        email = (value or "").strip()
        if not email:
            raise ValidationError("Email обязателен")
        if "@" not in email or "." not in email.split("@")[-1]:
            raise ValidationError("Некорректный email")
        return email

    @staticmethod
    def parse_priority(value: str | None) -> str:
        priority = (value or Priority.MEDIUM).strip().lower()
        if priority not in Priority.ALL:
            raise ValidationError("Некорректный приоритет")
        return priority

    @staticmethod
    def parse_optional_int(value, field_name: str) -> int | None:
        if value in (None, "", "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Некорректное значение поля '{field_name}'") from exc

    @staticmethod
    def task_payload(data: dict[str, Any], require_organization: bool = False) -> dict[str, Any]:
        theme = (data.get("theme") or "").strip()
        content = (data.get("content") or "").strip()
        due_date_raw = (data.get("due_date") or "").strip()

        if not theme:
            raise ValidationError("Поле 'Тема' обязательно")
        if len(theme) > 255:
            raise ValidationError("Тема не должна превышать 255 символов")
        if not content:
            raise ValidationError("Поле 'Содержимое' обязательно")
        if not due_date_raw:
            raise ValidationError("Поле 'Срок выполнения' обязательно")

        due_date = Validators.parse_due_date(due_date_raw)
        priority = Validators.parse_priority(data.get("priority"))
        organization_id = Validators.parse_optional_int(data.get("organization_id"), "organization_id")

        if require_organization and not organization_id:
            raise ValidationError("Нужно выбрать организацию")

        cleaned = {
            "theme": theme,
            "content": content,
            "due_date": due_date,
            "priority": priority,
            "organization_id": organization_id,
            "client_id": Validators.parse_optional_int(data.get("client_id"), "client_id"),
            "assigned_to_id": Validators.parse_optional_int(data.get("assigned_to_id"), "assigned_to_id"),
        }

        return cleaned

    @staticmethod
    def user_payload(data: dict[str, Any], password_required: bool = True) -> dict[str, Any]:
        username = (data.get("username") or "").strip()
        role = (data.get("role") or "").strip()
        password = (data.get("password") or "").strip()
        email = Validators.parse_email(data.get("email") or "")
        telegram_chat_id = (data.get("telegram_chat_id") or "").strip()
        organization_id = Validators.parse_optional_int(data.get("organization_id"), "organization_id")

        if not username:
            raise ValidationError("Логин обязателен")
        if role not in Roles.ALL:
            raise ValidationError("Некорректная роль")
        if password_required and not password:
            raise ValidationError("Пароль обязателен")
        if role == Roles.CLIENT and not organization_id:
            raise ValidationError("Для клиента нужно выбрать организацию")
        if role != Roles.CLIENT:
            organization_id = None

        cleaned = {
            "username": username,
            "role": role,
            "email": email,
            "telegram_chat_id": telegram_chat_id or None,
            "organization_id": organization_id,
        }
        if password:
            cleaned["password"] = password
        return cleaned

    @staticmethod
    def profile_payload(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "email": Validators.parse_email(data.get("email") or ""),
            "telegram_chat_id": ((data.get("telegram_chat_id") or "").strip() or None),
        }

    @staticmethod
    def comment_payload(data: dict[str, Any]) -> dict[str, Any]:
        content = (data.get("content") or "").strip()
        if not content:
            raise ValidationError("Комментарий не должен быть пустым")
        if len(content) > 5000:
            raise ValidationError("Комментарий слишком длинный (максимум 5000 символов)")
        return {"content": content}
