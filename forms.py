from __future__ import annotations

from datetime import datetime
from typing import Any

from models import Roles


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
    def task_payload(data: dict[str, Any], require_client: bool = False) -> dict[str, Any]:
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

        cleaned = {"theme": theme, "content": content, "due_date": due_date}

        if require_client:
            client_id = data.get("client_id")
            if not client_id:
                raise ValidationError("Нужно выбрать клиента")
            try:
                cleaned["client_id"] = int(client_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError("Некорректный client_id") from exc

        return cleaned

    @staticmethod
    def user_payload(data: dict[str, Any], password_required: bool = True) -> dict[str, Any]:
        username = (data.get("username") or "").strip()
        role = (data.get("role") or "").strip()
        password = (data.get("password") or "").strip()

        if not username:
            raise ValidationError("Логин обязателен")
        if role not in Roles.ALL:
            raise ValidationError("Некорректная роль")
        if password_required and not password:
            raise ValidationError("Пароль обязателен")

        cleaned = {"username": username, "role": role}
        if password:
            cleaned["password"] = password
        return cleaned
