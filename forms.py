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
    def parse_optional_date(value: str | None):
        raw = (value or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except (TypeError, ValueError) as exc:
            raise ValidationError("Дата должна быть в формате YYYY-MM-DD") from exc

    @staticmethod
    def parse_email(value: str, *, required: bool = True) -> str | None:
        email = (value or "").strip()
        if not email:
            if required:
                raise ValidationError("Email обязателен")
            return None
        if "@" not in email or "." not in email.split("@")[-1]:
            raise ValidationError("Некорректный email")
        return email

    @staticmethod
    def parse_priority_id(value) -> int:
        priority_id = Validators.parse_required_int(value, "priority_id")
        if not Priority.query.filter_by(id=priority_id).first():
            raise ValidationError("Некорректный приоритет")
        return priority_id

    @staticmethod
    def parse_optional_int(value, field_name: str) -> int | None:
        if value in (None, "", "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Некорректное значение поля '{field_name}'") from exc

    @staticmethod
    def parse_required_int(value, field_name: str) -> int:
        parsed = Validators.parse_optional_int(value, field_name)
        if parsed is None:
            raise ValidationError(f"Поле '{field_name}' обязательно")
        return parsed

    @staticmethod
    def parse_hex_color(value: str | None, field_name: str) -> str:
        color = (value or "").strip()
        if not color:
            raise ValidationError(f"Поле '{field_name}' обязательно")
        if len(color) != 7 or not color.startswith("#"):
            raise ValidationError(f"Поле '{field_name}' должно быть HEX-цветом в формате #RRGGBB")
        try:
            int(color[1:], 16)
        except ValueError as exc:
            raise ValidationError(f"Поле '{field_name}' должно быть HEX-цветом в формате #RRGGBB") from exc
        return color

    @staticmethod
    def task_payload(data: dict[str, Any], *, require_organization: bool = True) -> dict[str, Any]:
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
        priority_id = Validators.parse_priority_id(data.get("priority_id") or data.get("priority"))
        organization_id = Validators.parse_optional_int(data.get("organization_id"), "organization_id")

        if require_organization and not organization_id:
            raise ValidationError("Нужно выбрать организацию")

        return {
            "theme": theme,
            "content": content,
            "due_date": due_date,
            "priority_id": priority_id,
            "organization_id": organization_id,
            "employee_id": Validators.parse_optional_int(data.get("employee_id"), "employee_id"),
            "assigned_to_id": Validators.parse_optional_int(data.get("assigned_to_id"), "assigned_to_id"),
        }

    @staticmethod
    def user_payload(data: dict[str, Any], password_required: bool = True) -> dict[str, Any]:
        username = (data.get("username") or "").strip()
        role = (data.get("role") or "").strip()
        password = (data.get("password") or "").strip()
        email = Validators.parse_email(data.get("email") or "", required=True)
        telegram_chat_id = (data.get("telegram_chat_id") or "").strip()

        if not username:
            raise ValidationError("Логин обязателен")
        if role not in Roles.INTERNAL:
            raise ValidationError("Пользователь системы может иметь только роль admin или operator")
        if password_required and not password:
            raise ValidationError("Пароль обязателен")

        cleaned = {
            "username": username,
            "role": role,
            "email": email,
            "telegram_chat_id": telegram_chat_id or None,
        }
        if password:
            cleaned["password"] = password
        return cleaned

    @staticmethod
    def employee_payload(data: dict[str, Any]) -> dict[str, Any]:
        first_name = (data.get("first_name") or "").strip()
        last_name = (data.get("last_name") or "").strip()
        position = (data.get("position") or "").strip()
        email = Validators.parse_email(data.get("email") or "", required=True)
        phone = (data.get("phone") or "").strip()
        telegram = (data.get("telegram") or "").strip()
        organization_id = Validators.parse_required_int(data.get("organization_id"), "organization_id")

        if not first_name:
            raise ValidationError("Имя сотрудника обязательно")

        return {
            "first_name": first_name,
            "last_name": last_name or None,
            "position": position or None,
            "email": email,
            "phone": phone or None,
            "telegram": telegram or None,
            "organization_id": organization_id,
            "is_active": bool(data.get("is_active")),
        }

    @staticmethod
    def organization_payload(data: dict[str, Any]) -> dict[str, Any]:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValidationError("Название организации обязательно")

        return {
            "name": name,
            "description": (data.get("description") or "").strip() or None,
            "address": (data.get("address") or "").strip() or None,
            "inn": (data.get("inn") or "").strip() or None,
            "kpp": (data.get("kpp") or "").strip() or None,
            "bank_details": (data.get("bank_details") or "").strip() or None,
            "phone": (data.get("phone") or "").strip() or None,
            "email": Validators.parse_email(data.get("email") or "", required=False),
            "website": (data.get("website") or "").strip() or None,
        }

    @staticmethod
    def profile_payload(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "email": Validators.parse_email(data.get("email") or "", required=True),
            "telegram_chat_id": ((data.get("telegram_chat_id") or "").strip() or None),
        }

    @staticmethod
    def report_payload(data: dict[str, Any]) -> dict[str, Any]:
        days_raw = (data.get("days") or "").strip()
        start_date = Validators.parse_optional_date(data.get("start_date"))
        end_date = Validators.parse_optional_date(data.get("end_date"))

        days = None
        if days_raw:
            try:
                days = int(days_raw)
            except ValueError as exc:
                raise ValidationError("Период в днях должен быть числом") from exc
            if days <= 0:
                raise ValidationError("Период в днях должен быть больше 0")

        if start_date and end_date and start_date > end_date:
            raise ValidationError("Дата начала не может быть больше даты окончания")

        return {
            "days": days,
            "start_date": start_date,
            "end_date": end_date,
        }

    @staticmethod
    def settings_payload(data: dict[str, Any]) -> dict[str, Any]:
        site_name = (data.get("site_name") or "").strip()
        if not site_name:
            raise ValidationError("Название сайта обязательно")

        return {
            "site_name": site_name,
            "primary_color": Validators.parse_hex_color(data.get("primary_color"), "Основной цвет"),
            "secondary_color": Validators.parse_hex_color(data.get("secondary_color"), "Второстепенный цвет"),
            "background_color": Validators.parse_hex_color(data.get("background_color"), "Цвет фона"),
        }

    @staticmethod
    def comment_payload(data: dict[str, Any]) -> dict[str, Any]:
        content = (data.get("content") or "").strip()
        if not content:
            raise ValidationError("Комментарий не должен быть пустым")
        if len(content) > 5000:
            raise ValidationError("Комментарий слишком длинный (максимум 5000 символов)")
        return {"content": content}
