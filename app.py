from __future__ import annotations

import csv
import secrets
from datetime import date, datetime, time, timedelta
from functools import wraps
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import LoginManager, current_user, login_required
from sqlalchemy import func, inspect, or_, text
from werkzeug.utils import secure_filename

from api import api_bp
from auth import auth_bp
from config import Config
from forms import ValidationError, Validators
from models import (
    Employee,
    OperatorOrganizationAccess,
    Organization,
    Roles,
    Setting,
    Status,
    StatusHistory,
    Task,
    TaskComment,
    User,
    db,
)
from services import (
    DEFAULT_SETTINGS,
    admin_users,
    allowed_assignees_for_actor,
    allowed_employees_for_user,
    allowed_organizations_for_user,
    can_access_organization,
    can_view_task,
    collect_comment_recipients,
    collect_new_task_recipients,
    comment_notification_text,
    dispatch_notifications,
    filter_tasks_for_user,
    get_all_settings,
    priority_choices,
    record_task_change,
    reset_settings_to_defaults,
    resolve_assignee,
    set_setting,
    task_notification_text,
)


load_dotenv()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Авторизуйтесь для продолжения"
login_manager.login_message_category = "warning"

LEGACY_CLIENT_ROLE = "legacy_client"
FINAL_STATUS_NAMES = {"завершена", "выполнена", "закрыта", "done", "closed", "resolved"}


def roles_required(*allowed_roles):
    """Restrict endpoint access to listed roles."""

    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in allowed_roles:
                abort(403)
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


def _safe_next_url(fallback_url: str) -> str:
    next_url = request.form.get("next") or request.args.get("next")
    if not next_url:
        return fallback_url
    parsed = urlparse(next_url)
    if parsed.netloc:
        return fallback_url
    return next_url


def _active_admins_count(exclude_user_id: int | None = None) -> int:
    query = User.query.filter_by(role=Roles.ADMIN, active=True)
    if exclude_user_id is not None:
        query = query.filter(User.id != exclude_user_id)
    return query.count()


def _ordered_statuses() -> list[Status]:
    return Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).all()


def _first_status() -> Status | None:
    return Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).first()


def _generate_unique_org_name(base_name: str, existing_names: set[str]) -> str:
    name = base_name
    suffix = 2
    while name in existing_names:
        name = f"{base_name} ({suffix})"
        suffix += 1
    existing_names.add(name)
    return name


def _add_column_if_missing(table_name: str, column_name: str, ddl_tail: str) -> None:
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    if table_name not in table_names:
        return
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    if column_name in existing:
        return
    db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl_tail}"))


def _run_simple_schema_migrations(app: Flask) -> None:
    """Lightweight migrations for existing SQLite installs."""
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())

    # Users
    if "users" in table_names:
        _add_column_if_missing("users", "email", "email VARCHAR(255)")
        _add_column_if_missing("users", "telegram_chat_id", "telegram_chat_id VARCHAR(64)")
        _add_column_if_missing("users", "organization_id", "organization_id INTEGER")
        _add_column_if_missing("users", "active", "active BOOLEAN DEFAULT 1")
        _add_column_if_missing("users", "must_change_password", "must_change_password BOOLEAN DEFAULT 0")

    # Organizations
    if "organizations" in table_names:
        _add_column_if_missing("organizations", "description", "description TEXT")
        _add_column_if_missing("organizations", "address", "address VARCHAR(500)")
        _add_column_if_missing("organizations", "inn", "inn VARCHAR(20)")
        _add_column_if_missing("organizations", "kpp", "kpp VARCHAR(20)")
        _add_column_if_missing("organizations", "bank_details", "bank_details TEXT")
        _add_column_if_missing("organizations", "phone", "phone VARCHAR(64)")
        _add_column_if_missing("organizations", "email", "email VARCHAR(255)")
        _add_column_if_missing("organizations", "website", "website VARCHAR(255)")
        _add_column_if_missing("organizations", "api_token", "api_token VARCHAR(64)")
        _add_column_if_missing("organizations", "api_token_prefix", "api_token_prefix VARCHAR(12)")
        _add_column_if_missing("organizations", "updated_at", "updated_at DATETIME")

    # Statuses
    if "statuses" in table_names:
        _add_column_if_missing("statuses", "is_final", "is_final BOOLEAN DEFAULT 0")

    # Tasks
    if "tasks" in table_names:
        _add_column_if_missing("tasks", "priority", "priority VARCHAR(20) DEFAULT 'medium'")
        _add_column_if_missing("tasks", "organization_id", "organization_id INTEGER")
        _add_column_if_missing("tasks", "employee_id", "employee_id INTEGER")
        _add_column_if_missing("tasks", "assigned_to_id", "assigned_to_id INTEGER")
        _add_column_if_missing("tasks", "archived", "archived BOOLEAN DEFAULT 0")
        _add_column_if_missing("tasks", "archived_at", "archived_at DATETIME")
        _add_column_if_missing("tasks", "created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
        _add_column_if_missing("tasks", "updated_at", "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")

    db.session.commit()

    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    task_columns = set()
    if "tasks" in table_names:
        task_columns = {column["name"] for column in inspector.get_columns("tasks")}

    # Convert old operator->client access into operator->organization access.
    if "client_access" in table_names:
        rows = db.session.execute(text("SELECT operator_id, client_id FROM client_access")).mappings().all()
        for row in rows:
            org_row = db.session.execute(
                text("SELECT organization_id FROM users WHERE id = :client_id"),
                {"client_id": row["client_id"]},
            ).first()
            org_id = org_row[0] if org_row else None
            if not org_id:
                continue
            db.session.execute(
                text(
                    """
                    INSERT OR IGNORE INTO operator_organization_access (operator_id, organization_id, created_at)
                    VALUES (:operator_id, :organization_id, :created_at)
                    """
                ),
                {
                    "operator_id": row["operator_id"],
                    "organization_id": org_id,
                    "created_at": datetime.utcnow(),
                },
            )

    # Convert legacy client users to Employee entries.
    legacy_clients = User.query.filter_by(role=Roles.CLIENT).all()
    if legacy_clients:
        existing_org_names = {
            row[0]
            for row in Organization.query.with_entities(Organization.name).all()
            if row[0]
        }

        fallback_admin = User.query.filter_by(role=Roles.ADMIN, active=True).order_by(User.id.asc()).first()
        if not fallback_admin:
            fallback_admin = User(
                username="system_admin",
                role=Roles.ADMIN,
                active=True,
                must_change_password=False,
                email="system@localhost",
            )
            fallback_admin.set_password(app.config.get("ADMIN_PASSWORD", "admin"))
            db.session.add(fallback_admin)
            db.session.flush()

        user_to_employee: dict[int, tuple[int, int]] = {}

        for client in legacy_clients:
            organization = client.organization
            if not organization:
                org_name = _generate_unique_org_name(f"Компания {client.username}", existing_org_names)
                organization = Organization(name=org_name, description="Создано автоматически при миграции")
                db.session.add(organization)
                db.session.flush()
                client.organization_id = organization.id

            employee_email = client.email or f"{client.username}@example.local"
            employee = Employee.query.filter_by(
                organization_id=organization.id,
                email=employee_email,
                first_name=client.username,
            ).first()
            if not employee:
                employee = Employee(
                    first_name=client.username,
                    last_name=None,
                    position=None,
                    telegram=client.telegram_chat_id,
                    phone=None,
                    email=employee_email,
                    organization_id=organization.id,
                    is_active=True,
                )
                db.session.add(employee)
                db.session.flush()

            user_to_employee[client.id] = (employee.id, organization.id)

        for user_id, (employee_id, organization_id) in user_to_employee.items():
            if "client_id" in task_columns:
                db.session.execute(
                    text(
                        """
                        UPDATE tasks
                        SET employee_id = COALESCE(employee_id, :employee_id),
                            organization_id = COALESCE(organization_id, :organization_id)
                        WHERE client_id = :user_id
                        """
                    ),
                    {
                        "employee_id": employee_id,
                        "organization_id": organization_id,
                        "user_id": user_id,
                    },
                )
                db.session.execute(
                    text("UPDATE tasks SET client_id = NULL WHERE client_id = :user_id"),
                    {"user_id": user_id},
                )

            db.session.execute(
                text("UPDATE tasks SET created_by_id = :fallback_id WHERE created_by_id = :user_id"),
                {"fallback_id": fallback_admin.id, "user_id": user_id},
            )
            db.session.execute(
                text("UPDATE tasks SET assigned_to_id = NULL WHERE assigned_to_id = :user_id"),
                {"user_id": user_id},
            )

            if "task_comments" in table_names:
                db.session.execute(
                    text("UPDATE task_comments SET user_id = :fallback_id WHERE user_id = :user_id"),
                    {"fallback_id": fallback_admin.id, "user_id": user_id},
                )
            if "status_history" in table_names:
                db.session.execute(
                    text("UPDATE status_history SET changed_by_id = NULL WHERE changed_by_id = :user_id"),
                    {"user_id": user_id},
                )
            if "task_history" in table_names:
                db.session.execute(
                    text("UPDATE task_history SET changed_by_id = NULL WHERE changed_by_id = :user_id"),
                    {"user_id": user_id},
                )
            if "api_tokens" in table_names:
                db.session.execute(
                    text("DELETE FROM api_tokens WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )

        for client in legacy_clients:
            client.active = False
            client.must_change_password = False
            client.role = LEGACY_CLIENT_ROLE
            client.organization_id = None
            client.set_password(secrets.token_urlsafe(16))

    # Fill organization_id in tasks where possible.
    if "tasks" in table_names:
        db.session.execute(
            text(
                """
                UPDATE tasks
                SET organization_id = (
                    SELECT e.organization_id
                    FROM employees e
                    WHERE e.id = tasks.employee_id
                )
                WHERE organization_id IS NULL
                  AND employee_id IS NOT NULL
                """
            )
        )

        missing_org_tasks = db.session.execute(
            text("SELECT COUNT(*) FROM tasks WHERE organization_id IS NULL")
        ).scalar_one()

        if missing_org_tasks:
            fallback_org = Organization.query.filter_by(name="Организация по умолчанию").first()
            if not fallback_org:
                fallback_org = Organization(name="Организация по умолчанию", description="Создано автоматически")
                db.session.add(fallback_org)
                db.session.flush()

            db.session.execute(
                text("UPDATE tasks SET organization_id = :organization_id WHERE organization_id IS NULL"),
                {"organization_id": fallback_org.id},
            )

        db.session.execute(
            text(
                """
                UPDATE tasks
                SET employee_id = NULL
                WHERE employee_id IS NOT NULL
                  AND EXISTS (
                    SELECT 1
                    FROM employees e
                    WHERE e.id = tasks.employee_id
                      AND e.organization_id != tasks.organization_id
                  )
                """
            )
        )

    # Make sure non-internal users are inactive.
    User.query.filter(User.role.notin_(Roles.INTERNAL)).update(
        {User.active: False}, synchronize_session=False
    )

    # Setup final statuses if none marked.
    if Status.query.filter_by(is_final=True).count() == 0:
        for status in Status.query.all():
            if status.name.strip().lower() in FINAL_STATUS_NAMES:
                status.is_final = True

    db.session.commit()


def bootstrap_defaults(app: Flask) -> None:
    """Create initial statuses, settings and default admin on empty DB."""
    for key, value in DEFAULT_SETTINGS.items():
        if not Setting.query.filter_by(key=key).first():
            db.session.add(Setting(key=key, value=value))

    if Status.query.count() == 0:
        defaults = [
            ("Новая", 10, False),
            ("В работе", 20, False),
            ("Завершена", 30, True),
        ]
        for name, sort_order, is_final in defaults:
            db.session.add(Status(name=name, sort_order=sort_order, is_final=is_final))

    admin_user = User.query.filter_by(role=Roles.ADMIN).first()
    if not admin_user:
        admin_user = User(
            username=app.config.get("ADMIN_LOGIN", "admin"),
            role=Roles.ADMIN,
            active=True,
            must_change_password=True,
            email="admin@example.local",
        )
        admin_user.set_password(app.config.get("ADMIN_PASSWORD", "admin"))
        db.session.add(admin_user)
    else:
        admin_user.active = True

    if Status.query.filter_by(is_final=True).count() == 0:
        completed = Status.query.filter(Status.name.ilike("%заверш%"))
        for status in completed:
            status.is_final = True

    db.session.commit()


def _build_task_form_collections(actor: User, selected_org_id: int | None):
    organizations = allowed_organizations_for_user(actor)

    if selected_org_id and not can_access_organization(actor, selected_org_id):
        selected_org_id = None

    if not selected_org_id and len(organizations) == 1:
        selected_org_id = organizations[0].id

    employees_by_org: dict[str, list[dict[str, object]]] = {}
    assignees_by_org: dict[str, list[dict[str, object]]] = {}

    for organization in organizations:
        employees_by_org[str(organization.id)] = [
            {"id": employee.id, "name": employee.full_name}
            for employee in allowed_employees_for_user(actor, organization.id)
        ]
        assignees_by_org[str(organization.id)] = [
            {"id": operator.id, "name": operator.username}
            for operator in allowed_assignees_for_actor(actor, organization.id)
        ]

    employees = allowed_employees_for_user(actor, selected_org_id) if selected_org_id else []
    assignees = allowed_assignees_for_actor(actor, selected_org_id) if selected_org_id else []

    return {
        "organizations": organizations,
        "selected_org_id": selected_org_id,
        "employees": employees,
        "assignees": assignees,
        "employees_by_org": employees_by_org,
        "assignees_by_org": assignees_by_org,
    }


def _apply_task_filters(base_query):
    query = filter_tasks_for_user(base_query, current_user)

    statuses = _ordered_statuses()
    organizations = allowed_organizations_for_user(current_user)

    organization_id = request.args.get("organization_id", type=int)
    if organization_id:
        if can_access_organization(current_user, organization_id):
            query = query.filter(Task.organization_id == organization_id)
        else:
            flash("Нет доступа к выбранной организации", "warning")
            organization_id = None

    employees = allowed_employees_for_user(current_user, organization_id)

    employee_id = request.args.get("employee_id", type=int)
    if employee_id:
        employee = Employee.query.get(employee_id)
        if not employee or not can_access_organization(current_user, employee.organization_id):
            flash("Нет доступа к выбранному сотруднику", "warning")
            employee_id = None
        else:
            query = query.filter(Task.employee_id == employee_id)

    status_id = request.args.get("status_id", type=int)
    if status_id:
        query = query.filter(Task.status_id == status_id)

    priority = (request.args.get("priority") or "").strip().lower()
    if priority:
        query = query.filter(Task.priority == priority)

    due_date_raw = (request.args.get("due_date") or "").strip()
    if due_date_raw:
        try:
            due_date = Validators.parse_due_date(due_date_raw)
            query = query.filter(Task.due_date == due_date)
        except ValidationError:
            flash("Некорректная дата срока", "warning")

    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Task.theme.ilike(like), Task.content.ilike(like)))

    filters = {
        "organization_id": organization_id,
        "employee_id": employee_id,
        "status_id": status_id,
        "priority": priority,
        "due_date": due_date_raw,
        "q": q,
    }

    return query, statuses, organizations, employees, filters


def _build_report_period(source: dict) -> tuple[date, date, dict[str, str]]:
    defaults = {
        "days": "30",
        "start_date": "",
        "end_date": "",
    }

    has_input = any((source.get("days"), source.get("start_date"), source.get("end_date")))
    payload = {
        "days": source.get("days") if has_input else "30",
        "start_date": source.get("start_date") if has_input else "",
        "end_date": source.get("end_date") if has_input else "",
    }

    try:
        cleaned = Validators.report_payload(payload)
    except ValidationError as exc:
        flash(str(exc), "danger")
        today = date.today()
        return today - timedelta(days=29), today, defaults

    today = date.today()

    if cleaned["start_date"] and cleaned["end_date"]:
        start_date = cleaned["start_date"]
        end_date = cleaned["end_date"]
        form_data = {
            "days": "",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        return start_date, end_date, form_data

    days = cleaned["days"] or 30
    start_date = today - timedelta(days=days - 1)
    end_date = today
    form_data = {
        "days": str(days),
        "start_date": "",
        "end_date": "",
    }
    return start_date, end_date, form_data


def _collect_report_data(start_date: date, end_date: date) -> dict:
    start_dt = datetime.combine(start_date, time.min)
    end_dt_exclusive = datetime.combine(end_date + timedelta(days=1), time.min)

    statuses = _ordered_statuses()

    rows = (
        db.session.query(Task.status_id, func.count(Task.id))
        .filter(Task.created_at >= start_dt, Task.created_at < end_dt_exclusive)
        .group_by(Task.status_id)
        .all()
    )

    per_status_map = {status.id: 0 for status in statuses}
    total = 0
    for status_id, count_value in rows:
        count_int = int(count_value)
        per_status_map[status_id] = count_int
        total += count_int

    completed = 0
    details: list[dict[str, object]] = []
    for status in statuses:
        count_value = per_status_map.get(status.id, 0)
        if status.is_final:
            completed += count_value
        details.append({
            "status": status,
            "count": count_value,
        })

    not_completed = total - completed

    return {
        "total": total,
        "completed": completed,
        "not_completed": not_completed,
        "details": details,
        "chart_labels": [item["status"].name for item in details],
        "chart_values": [item["count"] for item in details],
        "start_date": start_date,
        "end_date": end_date,
    }


def _save_uploaded_file(file_storage, prefix: str, allowed_ext: set[str]) -> str:
    filename = secure_filename(file_storage.filename or "")
    if not filename:
        raise ValidationError("Файл не выбран")

    ext = Path(filename).suffix.lower()
    if ext not in allowed_ext:
        raise ValidationError(f"Недопустимый тип файла: {ext}")

    upload_dir = Path("static") / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    unique_name = f"{prefix}_{secrets.token_hex(8)}{ext}"
    absolute_path = upload_dir / unique_name
    file_storage.save(absolute_path)

    return f"uploads/{unique_name}"


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path("static/uploads").mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)

    with app.app_context():
        db.create_all()
        _run_simple_schema_migrations(app)
        bootstrap_defaults(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        if not user_id.isdigit():
            return None
        user = db.session.get(User, int(user_id))
        if not user:
            return None
        if not user.active:
            return None
        if user.role not in Roles.INTERNAL:
            return None
        return user

    @app.template_global("csrf_token")
    def csrf_token():
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_hex(16)
            session["_csrf_token"] = token
        return token

    @app.before_request
    def csrf_protect():
        if not app.config.get("CSRF_ENABLED", True):
            return None

        if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            return None

        if request.endpoint and request.endpoint.startswith("api."):
            return None

        token = session.get("_csrf_token")
        incoming = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")

        if not token or token != incoming:
            abort(400)
        return None

    @app.context_processor
    def inject_globals():
        settings = get_all_settings()
        logo_path = (settings.get("logo_path") or "").strip()
        favicon_path = (settings.get("favicon_path") or "").strip()

        return {
            "Roles": Roles,
            "app_version": app.config.get("APP_VERSION", "1.0"),
            "site_name": settings.get("site_name") or DEFAULT_SETTINGS["site_name"],
            "ui_settings": settings,
            "logo_url": url_for("static", filename=logo_path) if logo_path else None,
            "favicon_url": url_for("static", filename=favicon_path) if favicon_path else None,
        }

    @app.template_filter("datetime")
    def format_datetime(value):
        if not value:
            return "—"
        return value.strftime("%d.%m.%Y %H:%M")

    @app.route("/")
    @app.route("/tasks")
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def index():
        query = Task.query.filter(Task.archived.is_(False))
        query, statuses, organizations, employees, filters = _apply_task_filters(query)

        tasks = query.order_by(Task.created_at.desc()).all()

        return render_template(
            "index.html",
            tasks=tasks,
            statuses=statuses,
            organizations=organizations,
            employees=employees,
            priorities=priority_choices(),
            filters=filters,
        )

    @app.route("/profile", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def profile():
        if request.method == "POST":
            try:
                cleaned = Validators.profile_payload(request.form)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template("profile.html", form_data=request.form)

            current_user.email = cleaned["email"]
            current_user.telegram_chat_id = cleaned["telegram_chat_id"]
            db.session.commit()
            flash("Профиль обновлён", "success")
            return redirect(url_for("profile"))

        form_data = {
            "email": current_user.email or "",
            "telegram_chat_id": current_user.telegram_chat_id or "",
        }
        return render_template("profile.html", form_data=form_data)

    @app.route("/tasks/create", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def create_task():
        selected_org_id = request.form.get("organization_id", type=int)
        if request.method == "GET":
            selected_org_id = request.args.get("organization_id", type=int)

        collections = _build_task_form_collections(current_user, selected_org_id)

        if request.method == "POST":
            try:
                cleaned = Validators.task_payload(request.form, require_organization=True)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template(
                    "task_form.html",
                    form_data=request.form,
                    organizations=collections["organizations"],
                    employees=collections["employees"],
                    assignees=collections["assignees"],
                    employees_by_org=collections["employees_by_org"],
                    assignees_by_org=collections["assignees_by_org"],
                    priorities=priority_choices(),
                )

            organization = Organization.query.get(cleaned["organization_id"])
            if not organization:
                flash("Организация не найдена", "danger")
                return redirect(url_for("create_task"))

            if not can_access_organization(current_user, organization.id):
                flash("Нет доступа к выбранной организации", "danger")
                return redirect(url_for("create_task"))

            employee = None
            if cleaned["employee_id"]:
                employee = Employee.query.filter_by(
                    id=cleaned["employee_id"],
                    organization_id=organization.id,
                    is_active=True,
                ).first()
                if not employee:
                    flash("Сотрудник не найден или не принадлежит организации", "danger")
                    return redirect(url_for("create_task", organization_id=organization.id))

            assignee, assignee_error = resolve_assignee(
                current_user,
                organization.id,
                cleaned.get("assigned_to_id"),
            )
            if assignee_error:
                flash(assignee_error, "danger")
                return redirect(url_for("create_task", organization_id=organization.id))

            initial_status = _first_status()
            if not initial_status:
                flash("Сначала создайте хотя бы один статус", "danger")
                return redirect(url_for("index"))

            task = Task(
                theme=cleaned["theme"],
                content=cleaned["content"],
                due_date=cleaned["due_date"],
                priority=cleaned["priority"],
                organization=organization,
                employee=employee,
                status=initial_status,
                created_by=current_user,
                assigned_to=assignee,
            )
            db.session.add(task)
            db.session.flush()

            db.session.add(
                StatusHistory(
                    task=task,
                    old_status_id=None,
                    new_status_id=initial_status.id,
                    changed_by=current_user,
                )
            )

            record_task_change(task, current_user, "theme", None, task.theme)
            record_task_change(task, current_user, "content", None, task.content)
            record_task_change(task, current_user, "organization", None, task.organization.name)
            record_task_change(task, current_user, "employee", None, task.target_label)
            record_task_change(task, current_user, "due_date", None, task.due_date)
            record_task_change(task, current_user, "priority", None, task.priority_label)
            record_task_change(
                task,
                current_user,
                "assigned_to",
                None,
                task.assigned_to.username if task.assigned_to else "-",
            )

            db.session.commit()

            task_url = url_for("task_detail", task_id=task.id, _external=True)
            subject = f"[Helpdesk] Новая задача #{task.id}"
            body = task_notification_text(task, task_url, "Создана новая задача")

            recipients = collect_new_task_recipients(task)
            notify_target = bool(request.form.get("notify_target"))
            notify_admins = bool(request.form.get("notify_admins"))

            if not notify_target:
                recipients = [r for r in recipients if not r.key.startswith("employee:") and not r.key.startswith("organization:")]
            if not notify_admins:
                admin_keys = {f"user:{user.id}" for user in admin_users()}
                recipients = [r for r in recipients if r.key not in admin_keys]

            dispatch_notifications(recipients, subject, body)

            flash("Задача создана", "success")
            return redirect(url_for("task_detail", task_id=task.id))

        form_data = {
            "theme": "",
            "content": "",
            "due_date": "",
            "priority": "medium",
            "notify_target": "on",
            "notify_admins": "on",
            "organization_id": str(collections["selected_org_id"] or ""),
            "employee_id": "",
            "assigned_to_id": "",
        }

        return render_template(
            "task_form.html",
            form_data=form_data,
            organizations=collections["organizations"],
            employees=collections["employees"],
            assignees=collections["assignees"],
            employees_by_org=collections["employees_by_org"],
            assignees_by_org=collections["assignees_by_org"],
            priorities=priority_choices(),
        )

    @app.route("/tasks/<int:task_id>")
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def task_detail(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)

        can_edit = not task.archived
        can_archive = not task.archived
        can_restore = task.archived

        organizations = []
        employees = []
        assignees = []
        employees_by_org = {}
        assignees_by_org = {}

        if can_edit:
            collections = _build_task_form_collections(current_user, task.organization_id)
            organizations = collections["organizations"]
            employees = collections["employees"]
            assignees = collections["assignees"]
            employees_by_org = collections["employees_by_org"]
            assignees_by_org = collections["assignees_by_org"]

        return render_template(
            "task.html",
            task=task,
            statuses=_ordered_statuses(),
            priorities=priority_choices(),
            can_edit=can_edit,
            can_archive=can_archive,
            can_restore=can_restore,
            can_change_status=can_edit,
            can_comment=can_edit,
            organizations=organizations,
            employees=employees,
            assignees=assignees,
            employees_by_org=employees_by_org,
            assignees_by_org=assignees_by_org,
        )

    @app.route("/tasks/<int:task_id>/update", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def task_update(task_id: int):
        task = Task.query.get_or_404(task_id)
        if task.archived:
            flash("Архивную задачу нельзя редактировать", "warning")
            return redirect(url_for("task_detail", task_id=task.id))

        if not can_view_task(current_user, task):
            abort(403)

        try:
            cleaned = Validators.task_payload(request.form, require_organization=True)
        except ValidationError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        organization = Organization.query.get(cleaned["organization_id"])
        if not organization:
            flash("Организация не найдена", "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        if not can_access_organization(current_user, organization.id):
            flash("Нет доступа к выбранной организации", "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        employee = None
        if cleaned["employee_id"]:
            employee = Employee.query.filter_by(
                id=cleaned["employee_id"],
                organization_id=organization.id,
                is_active=True,
            ).first()
            if not employee:
                flash("Сотрудник не найден или не принадлежит организации", "danger")
                return redirect(url_for("task_detail", task_id=task.id))

        assignee, assignee_error = resolve_assignee(
            current_user,
            organization.id,
            cleaned.get("assigned_to_id"),
        )
        if assignee_error:
            flash(assignee_error, "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        old_theme = task.theme
        old_content = task.content
        old_organization = task.organization.name if task.organization else "-"
        old_target = task.target_label
        old_due_date = task.due_date
        old_priority = task.priority_label
        old_assignee = task.assigned_to.username if task.assigned_to else "-"

        task.theme = cleaned["theme"]
        task.content = cleaned["content"]
        task.due_date = cleaned["due_date"]
        task.priority = cleaned["priority"]
        task.organization = organization
        task.employee = employee
        task.assigned_to = assignee

        record_task_change(task, current_user, "theme", old_theme, task.theme)
        record_task_change(task, current_user, "content", old_content, task.content)
        record_task_change(task, current_user, "organization", old_organization, task.organization.name)
        record_task_change(task, current_user, "employee", old_target, task.target_label)
        record_task_change(task, current_user, "due_date", old_due_date, task.due_date)
        record_task_change(task, current_user, "priority", old_priority, task.priority_label)
        record_task_change(
            task,
            current_user,
            "assigned_to",
            old_assignee,
            task.assigned_to.username if task.assigned_to else "-",
        )

        db.session.commit()
        flash("Задача обновлена", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/tasks/<int:task_id>/change-status", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def task_change_status(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)
        if task.archived:
            flash("Архивную задачу нельзя менять", "warning")
            return redirect(url_for("task_detail", task_id=task.id))

        try:
            status_id = Validators.parse_required_int(request.form.get("status_id"), "status_id")
        except ValidationError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        new_status = Status.query.get(status_id)
        if not new_status:
            flash("Статус не найден", "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        old_status = task.status
        if old_status.id == new_status.id:
            flash("Статус не изменился", "info")
            return redirect(_safe_next_url(url_for("task_detail", task_id=task.id)))

        task.status = new_status
        db.session.add(
            StatusHistory(
                task=task,
                old_status_id=old_status.id,
                new_status_id=new_status.id,
                changed_by=current_user,
            )
        )
        record_task_change(task, current_user, "status", old_status.name, new_status.name)
        db.session.commit()

        flash("Статус обновлён", "success")
        return redirect(_safe_next_url(url_for("task_detail", task_id=task.id)))

    @app.route("/tasks/<int:task_id>/move-status", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def move_status(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            return jsonify({"error": "Forbidden"}), 403
        if task.archived:
            return jsonify({"error": "Archived task cannot be moved"}), 400

        payload = request.get_json(silent=True) or {}
        try:
            status_id = Validators.parse_required_int(payload.get("status_id"), "status_id")
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        new_status = Status.query.get(status_id)
        if not new_status:
            return jsonify({"error": "Status not found"}), 404

        old_status = task.status
        if old_status.id == new_status.id:
            return jsonify({"ok": True, "status": old_status.name})

        task.status = new_status
        db.session.add(
            StatusHistory(
                task=task,
                old_status_id=old_status.id,
                new_status_id=new_status.id,
                changed_by=current_user,
            )
        )
        record_task_change(task, current_user, "status", old_status.name, new_status.name)
        db.session.commit()

        return jsonify({"ok": True, "status": new_status.name})

    @app.route("/tasks/<int:task_id>/comments", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def add_comment(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)
        if task.archived:
            flash("Нельзя комментировать архивную задачу", "warning")
            return redirect(url_for("task_detail", task_id=task.id))

        try:
            cleaned = Validators.comment_payload(request.form)
        except ValidationError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        comment = TaskComment(task=task, author=current_user, content=cleaned["content"])
        db.session.add(comment)
        db.session.commit()

        task_url = url_for("task_detail", task_id=task.id, _external=True)
        subject = f"[Helpdesk] Новый комментарий к задаче #{task.id}"
        body = comment_notification_text(comment, task_url, "Добавлен комментарий")
        dispatch_notifications(collect_comment_recipients(task, current_user), subject, body)

        flash("Комментарий добавлен", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/tasks/<int:task_id>/archive", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def archive_task(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)

        if not task.archived:
            task.archived = True
            task.archived_at = datetime.utcnow()
            record_task_change(task, current_user, "archived", "false", "true")
            db.session.commit()
            flash("Задача отправлена в архив", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/tasks/<int:task_id>/restore", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def restore_task(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)

        if task.archived:
            task.archived = False
            task.archived_at = None
            record_task_change(task, current_user, "archived", "true", "false")
            db.session.commit()
            flash("Задача восстановлена", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/archive")
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def archive():
        query = Task.query.filter(Task.archived.is_(True))
        query, statuses, organizations, employees, filters = _apply_task_filters(query)

        tasks = query.order_by(Task.archived_at.desc(), Task.id.desc()).all()

        return render_template(
            "archive.html",
            tasks=tasks,
            statuses=statuses,
            organizations=organizations,
            employees=employees,
            priorities=priority_choices(),
            filters=filters,
        )

    @app.route("/kanban")
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def kanban():
        statuses = _ordered_statuses()
        query = Task.query.filter(Task.archived.is_(False))
        tasks = filter_tasks_for_user(query, current_user).order_by(Task.created_at.desc()).all()

        columns: dict[int, list[Task]] = {status.id: [] for status in statuses}
        for task in tasks:
            columns.setdefault(task.status_id, []).append(task)

        return render_template(
            "kanban.html",
            statuses=statuses,
            columns=columns,
            can_drag=True,
        )

    @app.route("/users")
    @roles_required(Roles.ADMIN)
    def users():
        users_list = (
            User.query.filter(User.role.in_(Roles.INTERNAL))
            .order_by(User.created_at.desc(), User.id.desc())
            .all()
        )
        return render_template("users.html", users=users_list)

    @app.route("/users/create", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def user_create():
        if request.method == "POST":
            try:
                cleaned = Validators.user_payload(request.form, password_required=True)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template("user_form.html", user=None, form_data=request.form)

            if User.query.filter_by(username=cleaned["username"]).first():
                flash("Пользователь с таким логином уже существует", "danger")
                return render_template("user_form.html", user=None, form_data=request.form)

            user = User(
                username=cleaned["username"],
                role=cleaned["role"],
                email=cleaned["email"],
                telegram_chat_id=cleaned["telegram_chat_id"],
                active=bool(request.form.get("active")),
                must_change_password=bool(request.form.get("must_change_password")),
            )
            user.set_password(cleaned["password"])

            db.session.add(user)
            db.session.commit()

            flash("Пользователь создан", "success")
            return redirect(url_for("users"))

        form_data = {
            "username": "",
            "role": Roles.OPERATOR,
            "email": "",
            "telegram_chat_id": "",
            "active": "on",
            "must_change_password": "",
        }
        return render_template("user_form.html", user=None, form_data=form_data)

    @app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def user_edit(user_id: int):
        user = User.query.get_or_404(user_id)

        if request.method == "POST":
            try:
                cleaned = Validators.user_payload(request.form, password_required=False)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template("user_form.html", user=user, form_data=request.form)

            duplicate = User.query.filter(User.username == cleaned["username"], User.id != user.id).first()
            if duplicate:
                flash("Логин уже занят", "danger")
                return render_template("user_form.html", user=user, form_data=request.form)

            next_role = cleaned["role"]
            next_active = bool(request.form.get("active"))

            if user.role == Roles.ADMIN and (next_role != Roles.ADMIN or not next_active):
                if _active_admins_count(exclude_user_id=user.id) == 0:
                    flash("В системе должен оставаться хотя бы один активный администратор", "danger")
                    return render_template("user_form.html", user=user, form_data=request.form)

            user.username = cleaned["username"]
            user.role = next_role
            user.email = cleaned["email"]
            user.telegram_chat_id = cleaned["telegram_chat_id"]
            user.active = next_active
            user.must_change_password = bool(request.form.get("must_change_password"))

            if cleaned.get("password"):
                user.set_password(cleaned["password"])

            db.session.commit()
            flash("Пользователь обновлён", "success")
            return redirect(url_for("users"))

        form_data = {
            "username": user.username,
            "role": user.role,
            "email": user.email or "",
            "telegram_chat_id": user.telegram_chat_id or "",
            "active": "on" if user.active else "",
            "must_change_password": "on" if user.must_change_password else "",
        }
        return render_template("user_form.html", user=user, form_data=form_data)

    @app.route("/organizations")
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def organizations():
        query = Organization.query.order_by(Organization.name.asc())

        if current_user.role == Roles.OPERATOR:
            org_ids = {org.id for org in allowed_organizations_for_user(current_user)}
            if not org_ids:
                orgs = []
            else:
                orgs = query.filter(Organization.id.in_(org_ids)).all()
        else:
            orgs = query.all()

        return render_template("organizations.html", organizations=orgs)

    @app.route("/organizations/create", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def organization_create():
        if request.method == "POST":
            try:
                cleaned = Validators.organization_payload(request.form)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template("organization_form.html", organization=None, form_data=request.form, employees=[])

            duplicate = Organization.query.filter_by(name=cleaned["name"]).first()
            if duplicate:
                flash("Организация с таким названием уже существует", "danger")
                return render_template("organization_form.html", organization=None, form_data=request.form, employees=[])

            organization = Organization(**cleaned)
            if request.form.get("generate_token"):
                raw_token = organization.generate_api_token()
                flash(f"API-токен создан: {raw_token}", "warning")

            db.session.add(organization)
            db.session.commit()

            flash("Организация создана", "success")
            return redirect(url_for("organizations"))

        form_data = {
            "name": "",
            "description": "",
            "address": "",
            "inn": "",
            "kpp": "",
            "bank_details": "",
            "phone": "",
            "email": "",
            "website": "",
            "generate_token": "on",
        }
        return render_template("organization_form.html", organization=None, form_data=form_data, employees=[])

    @app.route("/organizations/<int:organization_id>/edit", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def organization_edit(organization_id: int):
        organization = Organization.query.get_or_404(organization_id)

        if request.method == "POST":
            try:
                cleaned = Validators.organization_payload(request.form)
            except ValidationError as exc:
                flash(str(exc), "danger")
                employees = (
                    Employee.query.filter_by(organization_id=organization.id)
                    .order_by(Employee.last_name.asc(), Employee.first_name.asc())
                    .all()
                )
                return render_template(
                    "organization_form.html",
                    organization=organization,
                    form_data=request.form,
                    employees=employees,
                )

            duplicate = Organization.query.filter(Organization.name == cleaned["name"], Organization.id != organization.id).first()
            if duplicate:
                flash("Организация с таким названием уже существует", "danger")
                employees = (
                    Employee.query.filter_by(organization_id=organization.id)
                    .order_by(Employee.last_name.asc(), Employee.first_name.asc())
                    .all()
                )
                return render_template(
                    "organization_form.html",
                    organization=organization,
                    form_data=request.form,
                    employees=employees,
                )

            for key, value in cleaned.items():
                setattr(organization, key, value)

            db.session.commit()
            flash("Организация обновлена", "success")
            return redirect(url_for("organization_edit", organization_id=organization.id))

        employees = (
            Employee.query.filter_by(organization_id=organization.id)
            .order_by(Employee.last_name.asc(), Employee.first_name.asc())
            .all()
        )

        form_data = {
            "name": organization.name,
            "description": organization.description or "",
            "address": organization.address or "",
            "inn": organization.inn or "",
            "kpp": organization.kpp or "",
            "bank_details": organization.bank_details or "",
            "phone": organization.phone or "",
            "email": organization.email or "",
            "website": organization.website or "",
        }

        return render_template(
            "organization_form.html",
            organization=organization,
            form_data=form_data,
            employees=employees,
        )

    @app.route("/organizations/<int:organization_id>/delete", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def organization_delete(organization_id: int):
        organization = Organization.query.get_or_404(organization_id)

        if organization.employees.count() > 0:
            flash("Нельзя удалить организацию: есть связанные сотрудники", "danger")
            return redirect(url_for("organizations"))

        if organization.tasks.count() > 0:
            flash("Нельзя удалить организацию: есть связанные задачи", "danger")
            return redirect(url_for("organizations"))

        db.session.delete(organization)
        db.session.commit()
        flash("Организация удалена", "success")
        return redirect(url_for("organizations"))

    @app.route("/organizations/<int:organization_id>/token/new", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def organization_new_token(organization_id: int):
        organization = Organization.query.get_or_404(organization_id)
        raw_token = organization.generate_api_token()
        db.session.commit()
        flash(f"Новый API-токен: {raw_token}", "warning")
        return redirect(url_for("organization_edit", organization_id=organization.id))

    @app.route("/employees")
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def employees():
        organizations = allowed_organizations_for_user(current_user)
        organization_id = request.args.get("organization_id", type=int)

        query = Employee.query.filter_by(is_active=True)
        if current_user.role == Roles.OPERATOR:
            org_ids = {org.id for org in organizations}
            if not org_ids:
                query = query.filter(text("1=0"))
            else:
                query = query.filter(Employee.organization_id.in_(org_ids))

        if organization_id:
            if current_user.role == Roles.ADMIN or can_access_organization(current_user, organization_id):
                query = query.filter_by(organization_id=organization_id)
            else:
                flash("Нет доступа к выбранной организации", "warning")
                organization_id = None

        employees_list = query.order_by(Employee.last_name.asc(), Employee.first_name.asc(), Employee.id.asc()).all()

        return render_template(
            "employees.html",
            employees=employees_list,
            organizations=organizations,
            filters={"organization_id": organization_id},
        )

    @app.route("/employees/create", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def employee_create():
        organizations = Organization.query.order_by(Organization.name.asc()).all()

        if request.method == "POST":
            try:
                cleaned = Validators.employee_payload(request.form)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template(
                    "employee_form.html",
                    employee=None,
                    organizations=organizations,
                    form_data=request.form,
                )

            organization = Organization.query.get(cleaned["organization_id"])
            if not organization:
                flash("Организация не найдена", "danger")
                return render_template(
                    "employee_form.html",
                    employee=None,
                    organizations=organizations,
                    form_data=request.form,
                )

            employee = Employee(**cleaned)
            db.session.add(employee)
            db.session.commit()
            flash("Сотрудник создан", "success")
            return redirect(url_for("employees", organization_id=employee.organization_id))

        form_data = {
            "first_name": "",
            "last_name": "",
            "position": "",
            "email": "",
            "phone": "",
            "telegram": "",
            "organization_id": str(request.args.get("organization_id", type=int) or ""),
            "is_active": "on",
        }
        return render_template(
            "employee_form.html",
            employee=None,
            organizations=organizations,
            form_data=form_data,
        )

    @app.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def employee_edit(employee_id: int):
        employee = Employee.query.get_or_404(employee_id)
        organizations = Organization.query.order_by(Organization.name.asc()).all()

        if request.method == "POST":
            try:
                cleaned = Validators.employee_payload(request.form)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template(
                    "employee_form.html",
                    employee=employee,
                    organizations=organizations,
                    form_data=request.form,
                )

            organization = Organization.query.get(cleaned["organization_id"])
            if not organization:
                flash("Организация не найдена", "danger")
                return render_template(
                    "employee_form.html",
                    employee=employee,
                    organizations=organizations,
                    form_data=request.form,
                )

            if employee.organization_id != organization.id:
                active_tasks = Task.query.filter_by(employee_id=employee.id, archived=False).count()
                if active_tasks > 0:
                    flash(
                        "Нельзя сменить организацию сотрудника, пока есть активные задачи. Переназначьте задачи сначала.",
                        "danger",
                    )
                    return render_template(
                        "employee_form.html",
                        employee=employee,
                        organizations=organizations,
                        form_data=request.form,
                    )

            for key, value in cleaned.items():
                setattr(employee, key, value)

            db.session.commit()
            flash("Сотрудник обновлён", "success")
            return redirect(url_for("employees", organization_id=employee.organization_id))

        form_data = {
            "first_name": employee.first_name,
            "last_name": employee.last_name or "",
            "position": employee.position or "",
            "email": employee.email,
            "phone": employee.phone or "",
            "telegram": employee.telegram or "",
            "organization_id": str(employee.organization_id),
            "is_active": "on" if employee.is_active else "",
        }
        return render_template(
            "employee_form.html",
            employee=employee,
            organizations=organizations,
            form_data=form_data,
        )

    @app.route("/employees/<int:employee_id>/delete", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def employee_delete(employee_id: int):
        employee = Employee.query.get_or_404(employee_id)
        if Task.query.filter_by(employee_id=employee.id).count() > 0:
            flash("Нельзя удалить сотрудника: есть связанные задачи", "danger")
            return redirect(url_for("employees", organization_id=employee.organization_id))

        db.session.delete(employee)
        db.session.commit()
        flash("Сотрудник удалён", "success")
        return redirect(url_for("employees"))

    @app.route("/access")
    @roles_required(Roles.ADMIN)
    def access_management():
        operators = User.query.filter_by(role=Roles.OPERATOR).order_by(User.username.asc()).all()
        organizations = Organization.query.order_by(Organization.name.asc()).all()

        access_map: dict[int, set[int]] = {}
        for row in OperatorOrganizationAccess.query.all():
            access_map.setdefault(row.operator_id, set()).add(row.organization_id)

        return render_template(
            "access.html",
            operators=operators,
            organizations=organizations,
            access_map=access_map,
        )

    @app.route("/access/<int:operator_id>/update", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def access_update(operator_id: int):
        operator = User.query.filter_by(id=operator_id, role=Roles.OPERATOR).first_or_404()

        selected_ids: set[int] = set()
        for raw in request.form.getlist("organization_ids"):
            try:
                selected_ids.add(int(raw))
            except ValueError:
                continue

        valid_ids = {
            row[0]
            for row in Organization.query.with_entities(Organization.id).all()
        }
        selected_ids &= valid_ids

        OperatorOrganizationAccess.query.filter_by(operator_id=operator.id).delete(synchronize_session=False)
        for org_id in selected_ids:
            db.session.add(
                OperatorOrganizationAccess(
                    operator_id=operator.id,
                    organization_id=org_id,
                )
            )

        db.session.commit()
        flash(f"Доступы оператора {operator.username} обновлены", "success")
        return redirect(url_for("access_management"))

    @app.route("/statuses")
    @roles_required(Roles.ADMIN)
    def statuses():
        return render_template("statuses.html", statuses=_ordered_statuses())

    @app.route("/priorities")
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def priorities():
        return render_template("priorities.html", priorities=priority_choices())

    @app.route("/statuses/create", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def status_create():
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Название статуса обязательно", "danger")
            return redirect(url_for("statuses"))

        if Status.query.filter_by(name=name).first():
            flash("Статус с таким названием уже существует", "danger")
            return redirect(url_for("statuses"))

        sort_order_raw = (request.form.get("sort_order") or "").strip()
        if sort_order_raw:
            try:
                sort_order = int(sort_order_raw)
            except ValueError:
                flash("Порядок сортировки должен быть числом", "danger")
                return redirect(url_for("statuses"))
        else:
            last_status = Status.query.order_by(Status.sort_order.desc(), Status.id.desc()).first()
            sort_order = (last_status.sort_order + 10) if last_status else 10

        status = Status(
            name=name,
            sort_order=sort_order,
            is_final=bool(request.form.get("is_final")),
        )
        db.session.add(status)
        db.session.commit()
        flash("Статус создан", "success")
        return redirect(url_for("statuses"))

    @app.route("/statuses/<int:status_id>/update", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def status_update(status_id: int):
        status = Status.query.get_or_404(status_id)

        name = (request.form.get("name") or "").strip()
        sort_order_raw = (request.form.get("sort_order") or "").strip()

        if not name:
            flash("Название статуса обязательно", "danger")
            return redirect(url_for("statuses"))

        duplicate = Status.query.filter(Status.name == name, Status.id != status.id).first()
        if duplicate:
            flash("Статус с таким названием уже существует", "danger")
            return redirect(url_for("statuses"))

        try:
            sort_order = int(sort_order_raw)
        except ValueError:
            flash("Порядок сортировки должен быть числом", "danger")
            return redirect(url_for("statuses"))

        status.name = name
        status.sort_order = sort_order
        status.is_final = bool(request.form.get("is_final"))
        db.session.commit()

        flash("Статус обновлён", "success")
        return redirect(url_for("statuses"))

    @app.route("/statuses/<int:status_id>/delete", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def status_delete(status_id: int):
        status = Status.query.get_or_404(status_id)

        if status.tasks.count() > 0:
            flash("Нельзя удалить статус: есть связанные задачи", "danger")
            return redirect(url_for("statuses"))

        if Status.query.count() <= 1:
            flash("В системе должен оставаться минимум один статус", "danger")
            return redirect(url_for("statuses"))

        db.session.delete(status)
        db.session.commit()
        flash("Статус удалён", "success")
        return redirect(url_for("statuses"))

    @app.route("/reports", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def reports():
        source = request.form if request.method == "POST" else request.args
        start_date, end_date, form_data = _build_report_period(source)
        report = _collect_report_data(start_date, end_date)

        if request.method == "POST":
            return redirect(
                url_for(
                    "reports",
                    days=form_data["days"],
                    start_date=form_data["start_date"],
                    end_date=form_data["end_date"],
                )
            )

        return render_template(
            "reports.html",
            report=report,
            form_data=form_data,
        )

    @app.route("/reports/export.csv")
    @roles_required(Roles.ADMIN)
    def reports_export_csv():
        start_date, end_date, form_data = _build_report_period(request.args)
        report = _collect_report_data(start_date, end_date)

        output = StringIO()
        writer = csv.writer(output)

        writer.writerow(["Report", "Tasks by status"])
        writer.writerow(["Period start", report["start_date"].isoformat()])
        writer.writerow(["Period end", report["end_date"].isoformat()])
        writer.writerow([])
        writer.writerow(["Metric", "Value"])
        writer.writerow(["Total created", report["total"]])
        writer.writerow(["Completed", report["completed"]])
        writer.writerow(["Not completed", report["not_completed"]])
        writer.writerow([])
        writer.writerow(["Status", "Count", "Final"])

        for item in report["details"]:
            status = item["status"]
            writer.writerow([status.name, item["count"], "yes" if status.is_final else "no"])

        csv_data = output.getvalue()
        output.close()

        filename = f"report_tasks_{report['start_date'].isoformat()}_{report['end_date'].isoformat()}.csv"
        return Response(
            csv_data,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/settings/system", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def system_settings():
        if request.method == "POST":
            if request.form.get("reset_defaults"):
                reset_settings_to_defaults()
                db.session.commit()
                flash("Настройки сброшены к значениям по умолчанию", "success")
                return redirect(url_for("system_settings"))

            try:
                cleaned = Validators.settings_payload(request.form)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template("system_settings.html", form_data=request.form)

            for key, value in cleaned.items():
                set_setting(key, value)

            logo_file = request.files.get("logo")
            favicon_file = request.files.get("favicon")

            try:
                if logo_file and logo_file.filename:
                    logo_path = _save_uploaded_file(logo_file, "logo", {".png", ".jpg", ".jpeg", ".svg", ".webp"})
                    set_setting("logo_path", logo_path)

                if favicon_file and favicon_file.filename:
                    favicon_path = _save_uploaded_file(favicon_file, "favicon", {".ico", ".png", ".svg"})
                    set_setting("favicon_path", favicon_path)
            except ValidationError as exc:
                flash(str(exc), "danger")
                db.session.rollback()
                return redirect(url_for("system_settings"))

            db.session.commit()
            flash("Системные настройки сохранены", "success")
            return redirect(url_for("system_settings"))

        return render_template("system_settings.html", form_data=get_all_settings())

    @app.errorhandler(400)
    def bad_request(_error):
        return render_template("error.html", code=400, message="Некорректный запрос"), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", code=403, message="Доступ запрещён"), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="Страница не найдена"), 404

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
