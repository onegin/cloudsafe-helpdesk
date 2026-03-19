from __future__ import annotations

import secrets
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask,
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
from sqlalchemy import inspect, or_, text

from api import api_bp
from auth import auth_bp
from config import Config
from forms import ValidationError, Validators
from models import (
    ApiToken,
    OperatorOrganizationAccess,
    Organization,
    Priority,
    Roles,
    Status,
    StatusHistory,
    Task,
    TaskComment,
    User,
    db,
)
from services import (
    admin_users,
    allowed_assignees_for_actor,
    allowed_clients_for_user,
    allowed_organizations_for_user,
    can_access_client,
    can_access_organization,
    can_view_task,
    collect_comment_recipients,
    comment_notification_text,
    dispatch_notifications,
    filter_tasks_for_user,
    priority_choices,
    record_task_change,
    resolve_assignee,
    task_notification_text,
)


login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Авторизуйтесь для продолжения"
login_manager.login_message_category = "warning"


def roles_required(*allowed_roles):
    """Restrict endpoint access to the listed roles."""

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


def _ordered_statuses():
    return Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).all()


def _first_status() -> Status | None:
    return Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).first()


def _rebuild_tasks_table(existing_columns: set[str]) -> None:
    """Rebuild tasks table to support nullable client_id and organization_id."""
    priority_expr = "COALESCE(NULLIF(priority, ''), 'medium')" if "priority" in existing_columns else "'medium'"
    organization_expr = "organization_id" if "organization_id" in existing_columns else "NULL"
    client_expr = "client_id" if "client_id" in existing_columns else "NULL"
    assigned_expr = "assigned_to_id" if "assigned_to_id" in existing_columns else "NULL"
    archived_expr = "COALESCE(archived, 0)" if "archived" in existing_columns else "0"
    archived_at_expr = "archived_at" if "archived_at" in existing_columns else "NULL"
    created_expr = "COALESCE(created_at, CURRENT_TIMESTAMP)" if "created_at" in existing_columns else "CURRENT_TIMESTAMP"
    updated_expr = (
        "COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
        if "updated_at" in existing_columns
        else created_expr
    )

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    db.session.execute(text("ALTER TABLE tasks RENAME TO tasks_old"))

    db.session.execute(
        text(
            """
            CREATE TABLE tasks (
                id INTEGER NOT NULL PRIMARY KEY,
                theme VARCHAR(255) NOT NULL,
                content TEXT NOT NULL,
                due_date DATE NOT NULL,
                priority VARCHAR(20) NOT NULL DEFAULT 'medium',
                organization_id INTEGER,
                client_id INTEGER,
                status_id INTEGER NOT NULL,
                created_by_id INTEGER NOT NULL,
                assigned_to_id INTEGER,
                archived BOOLEAN NOT NULL DEFAULT 0,
                archived_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(organization_id) REFERENCES organizations (id),
                FOREIGN KEY(client_id) REFERENCES users (id),
                FOREIGN KEY(status_id) REFERENCES statuses (id),
                FOREIGN KEY(created_by_id) REFERENCES users (id),
                FOREIGN KEY(assigned_to_id) REFERENCES users (id)
            )
            """
        )
    )

    db.session.execute(
        text(
            f"""
            INSERT INTO tasks (
                id, theme, content, due_date, priority,
                organization_id, client_id, status_id, created_by_id, assigned_to_id,
                archived, archived_at, created_at, updated_at
            )
            SELECT
                id,
                theme,
                content,
                due_date,
                {priority_expr},
                {organization_expr},
                {client_expr},
                status_id,
                created_by_id,
                {assigned_expr},
                {archived_expr},
                {archived_at_expr},
                {created_expr},
                {updated_expr}
            FROM tasks_old
            """
        )
    )

    db.session.execute(text("DROP TABLE tasks_old"))
    db.session.execute(text("PRAGMA foreign_keys=ON"))


def _generate_unique_org_name(base_name: str, existing_names: set[str]) -> str:
    name = base_name
    suffix = 2
    while name in existing_names:
        name = f"{base_name} ({suffix})"
        suffix += 1
    existing_names.add(name)
    return name


def _run_simple_schema_migrations() -> None:
    """Simple SQL migrations for users who already have an old SQLite DB."""
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())

    if "users" in table_names:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if "email" not in user_columns:
            db.session.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(255)"))
        if "telegram_chat_id" not in user_columns:
            db.session.execute(text("ALTER TABLE users ADD COLUMN telegram_chat_id VARCHAR(64)"))
        if "organization_id" not in user_columns:
            db.session.execute(text("ALTER TABLE users ADD COLUMN organization_id INTEGER"))

    if "tasks" in table_names:
        task_columns_raw = inspector.get_columns("tasks")
        task_columns = {column["name"]: column for column in task_columns_raw}

        if "priority" not in task_columns:
            db.session.execute(text("ALTER TABLE tasks ADD COLUMN priority VARCHAR(20) DEFAULT 'medium'"))
        if "assigned_to_id" not in task_columns:
            db.session.execute(text("ALTER TABLE tasks ADD COLUMN assigned_to_id INTEGER"))

        needs_rebuild = (
            "organization_id" not in task_columns
            or ("client_id" in task_columns and not task_columns["client_id"].get("nullable", True))
        )
        if needs_rebuild:
            _rebuild_tasks_table(set(task_columns.keys()))

        db.session.execute(text("UPDATE tasks SET priority='medium' WHERE priority IS NULL OR priority = ''"))

    db.session.commit()

    # Refresh metadata snapshot after schema-altering operations.
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())

    if "organizations" not in table_names:
        return

    existing_names = {
        row[0]
        for row in Organization.query.with_entities(Organization.name).all()
        if row[0]
    }

    clients = User.query.filter_by(role=Roles.CLIENT).all()
    for client in clients:
        if client.organization_id:
            continue
        org_name = _generate_unique_org_name(f"Компания {client.username}", existing_names)
        organization = Organization(name=org_name, description="Создано автоматически при миграции")
        db.session.add(organization)
        db.session.flush()
        client.organization_id = organization.id

    # Ensure all tasks have organization_id.
    fallback_org = Organization.query.filter_by(name="Организация по умолчанию").first()
    tasks_without_org = Task.query.filter(Task.organization_id.is_(None)).all()
    if tasks_without_org and not fallback_org:
        fallback_org = Organization(name="Организация по умолчанию", description="Создано автоматически")
        db.session.add(fallback_org)
        db.session.flush()

    for task in tasks_without_org:
        if task.client and task.client.organization_id:
            task.organization_id = task.client.organization_id
        else:
            task.organization_id = fallback_org.id if fallback_org else None

    # Convert old operator->client access links to operator->organization links.
    if "client_access" in table_names:
        rows = db.session.execute(text("SELECT operator_id, client_id FROM client_access")).fetchall()
        existing_pairs = {
            (row.operator_id, row.organization_id)
            for row in OperatorOrganizationAccess.query.with_entities(
                OperatorOrganizationAccess.operator_id,
                OperatorOrganizationAccess.organization_id,
            ).all()
        }
        for row in rows:
            operator = db.session.get(User, row.operator_id)
            client = db.session.get(User, row.client_id)
            if not operator or operator.role != Roles.OPERATOR:
                continue
            if not client or client.role != Roles.CLIENT or not client.organization_id:
                continue

            pair = (operator.id, client.organization_id)
            if pair in existing_pairs:
                continue

            db.session.add(
                OperatorOrganizationAccess(
                    operator_id=operator.id,
                    organization_id=client.organization_id,
                )
            )
            existing_pairs.add(pair)

    # Non-client users do not belong to an organization.
    for user in User.query.filter(User.role != Roles.CLIENT, User.organization_id.is_not(None)).all():
        user.organization_id = None

    db.session.commit()


def bootstrap_defaults(app: Flask) -> None:
    admin = User.query.filter_by(role=Roles.ADMIN).first()
    if not admin:
        admin_login = app.config["ADMIN_LOGIN"]
        admin_password = app.config["ADMIN_PASSWORD"]

        admin_user = User.query.filter_by(username=admin_login).first()
        if not admin_user:
            admin_user = User(username=admin_login)
            db.session.add(admin_user)

        admin_user.role = Roles.ADMIN
        admin_user.active = True
        admin_user.organization_id = None
        admin_user.must_change_password = admin_password == "admin"
        admin_user.set_password(admin_password)
        if not admin_user.email:
            admin_user.email = f"{admin_login}@example.local"

    if Status.query.count() == 0:
        default_statuses = ["Новая", "В работе", "Завершена"]
        for index, name in enumerate(default_statuses, start=1):
            db.session.add(Status(name=name, sort_order=index * 10))

    users_without_email = User.query.filter((User.email.is_(None)) | (User.email == "")).all()
    for user in users_without_email:
        user.email = f"{user.username}@example.local"

    db.session.commit()


def _generate_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)

    with app.app_context():
        db.create_all()
        _run_simple_schema_migrations()
        db.create_all()
        bootstrap_defaults(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    @app.before_request
    def csrf_protect():
        if not app.config.get("CSRF_ENABLED", True):
            return None
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if request.path.startswith("/api/"):
            return None

        token = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
        session_token = session.get("_csrf_token")
        if not token or not session_token or token != session_token:
            abort(400)
        return None

    @app.context_processor
    def inject_globals():
        return {
            "Roles": Roles,
            "Priority": Priority,
            "csrf_token": _generate_csrf_token,
            "priority_options": priority_choices(),
            "app_version": app.config.get("APP_VERSION", "1.0"),
        }

    @app.template_filter("datetime")
    def format_datetime(value):
        if not value:
            return "-"
        return value.strftime("%Y-%m-%d %H:%M")

    def _build_task_form_collections(actor: User, selected_org_id: int | None):
        organizations = allowed_organizations_for_user(actor)

        if actor.role == Roles.CLIENT:
            selected_org_id = actor.organization_id
        elif selected_org_id and not can_access_organization(actor, selected_org_id):
            selected_org_id = None

        if not selected_org_id and len(organizations) == 1:
            selected_org_id = organizations[0].id

        clients_by_org: dict[str, list[dict[str, object]]] = {}
        assignees_by_org: dict[str, list[dict[str, object]]] = {}

        for organization in organizations:
            clients_by_org[str(organization.id)] = [
                {"id": client.id, "username": client.username}
                for client in allowed_clients_for_user(actor, organization.id)
            ]
            assignees_by_org[str(organization.id)] = [
                {"id": operator.id, "username": operator.username}
                for operator in allowed_assignees_for_actor(actor, organization.id)
            ]

        clients = allowed_clients_for_user(actor, selected_org_id) if selected_org_id else []
        assignees = allowed_assignees_for_actor(actor, selected_org_id) if selected_org_id else []

        return {
            "organizations": organizations,
            "selected_org_id": selected_org_id,
            "clients": clients,
            "assignees": assignees,
            "clients_by_org": clients_by_org,
            "assignees_by_org": assignees_by_org,
        }

    @app.route("/")
    def home():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        return redirect(url_for("auth.login"))

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
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
            flash("Настройки профиля обновлены", "success")
            return redirect(url_for("profile"))

        form_data = {
            "email": current_user.email or "",
            "telegram_chat_id": current_user.telegram_chat_id or "",
        }
        return render_template("profile.html", form_data=form_data)

    @app.route("/tasks")
    @login_required
    def index():
        statuses = _ordered_statuses()
        priorities = priority_choices()

        organizations = []
        clients = []
        if current_user.role in (Roles.ADMIN, Roles.OPERATOR):
            organizations = allowed_organizations_for_user(current_user)

        query = Task.query.filter_by(archived=False)
        query = filter_tasks_for_user(query, current_user)

        status_id = request.args.get("status_id", type=int)
        if status_id:
            query = query.filter_by(status_id=status_id)

        organization_id = request.args.get("organization_id", type=int)
        if organization_id:
            if can_access_organization(current_user, organization_id):
                query = query.filter_by(organization_id=organization_id)
            else:
                flash("Нет доступа к выбранной организации", "warning")
                organization_id = None

        if current_user.role in (Roles.ADMIN, Roles.OPERATOR):
            clients = allowed_clients_for_user(current_user, organization_id)

        client_id = request.args.get("client_id", type=int)
        if client_id:
            if can_access_client(current_user, client_id):
                query = query.filter_by(client_id=client_id)
            else:
                flash("Нет доступа к выбранному сотруднику", "warning")
                client_id = None

        due_date_filter = (request.args.get("due_date") or "").strip()
        if due_date_filter:
            try:
                query = query.filter_by(due_date=Validators.parse_due_date(due_date_filter))
            except ValidationError:
                flash("Некорректная дата фильтра, используйте YYYY-MM-DD", "warning")

        priority_filter = (request.args.get("priority") or "").strip().lower()
        if priority_filter in Priority.ALL:
            query = query.filter_by(priority=priority_filter)
        else:
            priority_filter = ""

        search_query = (request.args.get("q") or "").strip()
        if search_query:
            like = f"%{search_query}%"
            query = query.filter(or_(Task.theme.ilike(like), Task.content.ilike(like)))

        tasks = query.order_by(Task.due_date.asc(), Task.created_at.desc()).all()

        return render_template(
            "index.html",
            tasks=tasks,
            statuses=statuses,
            priorities=priorities,
            organizations=organizations,
            clients=clients,
            filters={
                "status_id": status_id,
                "organization_id": organization_id,
                "client_id": client_id,
                "due_date": due_date_filter,
                "priority": priority_filter,
                "q": search_query,
            },
        )

    @app.route("/tasks/create", methods=["GET", "POST"])
    @login_required
    def create_task():
        statuses = _ordered_statuses()
        priorities = priority_choices()

        if not statuses:
            flash("Невозможно создать задачу без настроенных статусов", "danger")
            return redirect(url_for("index"))

        selected_org_id = request.form.get("organization_id", type=int)
        if request.method == "GET":
            selected_org_id = request.args.get("organization_id", type=int)
        if current_user.role == Roles.CLIENT:
            selected_org_id = current_user.organization_id

        collections = _build_task_form_collections(current_user, selected_org_id)
        organizations = collections["organizations"]
        selected_org_id = collections["selected_org_id"]
        clients = collections["clients"]
        assignees = collections["assignees"]

        if current_user.role == Roles.CLIENT and not selected_org_id:
            flash("Ваш пользователь не привязан к организации. Обратитесь к администратору.", "danger")
            return redirect(url_for("index"))

        if request.method == "POST":
            require_organization = current_user.role != Roles.CLIENT
            try:
                cleaned = Validators.task_payload(request.form, require_organization=require_organization)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template(
                    "task_form.html",
                    clients=clients,
                    assignees=assignees,
                    organizations=organizations,
                    clients_by_org=collections["clients_by_org"],
                    assignees_by_org=collections["assignees_by_org"],
                    task=None,
                    priorities=priorities,
                    form_data=request.form,
                )

            if current_user.role == Roles.CLIENT:
                organization = current_user.organization
                client = current_user
            else:
                organization = Organization.query.get(cleaned["organization_id"])
                if not organization:
                    flash("Выбранная организация не найдена", "danger")
                    return render_template(
                        "task_form.html",
                        clients=clients,
                        assignees=assignees,
                        organizations=organizations,
                        clients_by_org=collections["clients_by_org"],
                        assignees_by_org=collections["assignees_by_org"],
                        task=None,
                        priorities=priorities,
                        form_data=request.form,
                    )

                if not can_access_organization(current_user, organization.id):
                    flash("Нет доступа к выбранной организации", "danger")
                    return render_template(
                        "task_form.html",
                        clients=clients,
                        assignees=assignees,
                        organizations=organizations,
                        clients_by_org=collections["clients_by_org"],
                        assignees_by_org=collections["assignees_by_org"],
                        task=None,
                        priorities=priorities,
                        form_data=request.form,
                    )

                client = None
                if cleaned.get("client_id"):
                    client = User.query.filter_by(
                        id=cleaned["client_id"],
                        role=Roles.CLIENT,
                        active=True,
                    ).first()
                    if not client:
                        flash("Выбранный сотрудник не найден", "danger")
                        return render_template(
                            "task_form.html",
                            clients=clients,
                            assignees=assignees,
                            organizations=organizations,
                            clients_by_org=collections["clients_by_org"],
                            assignees_by_org=collections["assignees_by_org"],
                            task=None,
                            priorities=priorities,
                            form_data=request.form,
                        )
                    if client.organization_id != organization.id:
                        flash("Сотрудник не принадлежит выбранной организации", "danger")
                        return render_template(
                            "task_form.html",
                            clients=clients,
                            assignees=assignees,
                            organizations=organizations,
                            clients_by_org=collections["clients_by_org"],
                            assignees_by_org=collections["assignees_by_org"],
                            task=None,
                            priorities=priorities,
                            form_data=request.form,
                        )

            assignee, assignee_error = resolve_assignee(
                current_user,
                organization.id,
                cleaned.get("assigned_to_id"),
            )
            if assignee_error:
                flash(assignee_error, "danger")
                return render_template(
                    "task_form.html",
                    clients=clients,
                    assignees=assignees,
                    organizations=organizations,
                    clients_by_org=collections["clients_by_org"],
                    assignees_by_org=collections["assignees_by_org"],
                    task=None,
                    priorities=priorities,
                    form_data=request.form,
                )

            initial_status = _first_status()
            if not initial_status:
                flash("Невозможно создать задачу без статуса", "danger")
                return redirect(url_for("index"))

            task = Task(
                theme=cleaned["theme"],
                content=cleaned["content"],
                due_date=cleaned["due_date"],
                priority=cleaned["priority"],
                organization=organization,
                client=client,
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
            record_task_change(task, current_user, "client", None, task.target_label)
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

            notify_admins = bool(request.form.get("notify_admins", "on"))
            notify_client = bool(request.form.get("notify_client", "on"))

            recipients = []
            if notify_admins:
                recipients.extend(admin_users())
            if notify_client and task.client:
                recipients.append(task.client)
            if task.assigned_to:
                recipients.append(task.assigned_to)

            if recipients:
                task_url = url_for("task_detail", task_id=task.id, _external=True)
                subject = f"[Helpdesk] Новая задача #{task.id}"
                body = task_notification_text(task, task_url, "Создана новая задача")
                dispatch_notifications(recipients, subject, body)

            flash("Задача успешно создана", "success")
            return redirect(url_for("task_detail", task_id=task.id))

        default_form = {
            "priority": Priority.MEDIUM,
            "notify_admins": "on",
            "notify_client": "on",
            "organization_id": str(selected_org_id) if selected_org_id else "",
            "client_id": str(current_user.id) if current_user.role == Roles.CLIENT else "",
        }
        return render_template(
            "task_form.html",
            clients=clients,
            assignees=assignees,
            organizations=organizations,
            clients_by_org=collections["clients_by_org"],
            assignees_by_org=collections["assignees_by_org"],
            task=None,
            priorities=priorities,
            form_data=default_form,
        )

    @app.route("/tasks/<int:task_id>")
    @login_required
    def task_detail(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)

        statuses = _ordered_statuses()
        priorities = priority_choices()

        organizations = []
        clients = []
        assignees = []
        clients_by_org = {}
        assignees_by_org = {}

        can_operator_edit = current_user.role in (Roles.ADMIN, Roles.OPERATOR)
        if can_operator_edit:
            collections = _build_task_form_collections(current_user, task.organization_id)
            organizations = collections["organizations"]
            clients = collections["clients"]
            assignees = collections["assignees"]
            clients_by_org = collections["clients_by_org"]
            assignees_by_org = collections["assignees_by_org"]

        return render_template(
            "task.html",
            task=task,
            statuses=statuses,
            priorities=priorities,
            organizations=organizations,
            clients=clients,
            assignees=assignees,
            clients_by_org=clients_by_org,
            assignees_by_org=assignees_by_org,
            can_edit=can_operator_edit and not task.archived,
            can_change_status=can_operator_edit and not task.archived,
            can_archive=can_operator_edit and not task.archived,
            can_restore=can_operator_edit and task.archived,
            can_comment=not task.archived,
        )

    @app.route("/tasks/<int:task_id>/update", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def task_update(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)
        if task.archived:
            flash("Архивную задачу редактировать нельзя", "warning")
            return redirect(url_for("task_detail", task_id=task.id))

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

        client = None
        if cleaned.get("client_id"):
            client = User.query.filter_by(
                id=cleaned["client_id"],
                role=Roles.CLIENT,
                active=True,
            ).first()
            if not client:
                flash("Сотрудник не найден", "danger")
                return redirect(url_for("task_detail", task_id=task.id))
            if client.organization_id != organization.id:
                flash("Сотрудник должен принадлежать выбранной организации", "danger")
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
        old_client = task.target_label
        old_due_date = task.due_date
        old_priority = task.priority_label
        old_assignee = task.assigned_to.username if task.assigned_to else "-"

        task.theme = cleaned["theme"]
        task.content = cleaned["content"]
        task.organization = organization
        task.client = client
        task.due_date = cleaned["due_date"]
        task.priority = cleaned["priority"]
        task.assigned_to = assignee

        record_task_change(task, current_user, "theme", old_theme, task.theme)
        record_task_change(task, current_user, "content", old_content, task.content)
        record_task_change(task, current_user, "organization", old_organization, task.organization.name)
        record_task_change(task, current_user, "client", old_client, task.target_label)
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
            flash("Архивную задачу нельзя перевести в другой статус", "warning")
            return redirect(url_for("task_detail", task_id=task.id))

        status_id = request.form.get("status_id", type=int)
        new_status = Status.query.get(status_id) if status_id else None
        if not new_status:
            flash("Статус не найден", "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        if new_status.id == task.status_id:
            flash("Статус уже установлен", "info")
            return redirect(_safe_next_url(url_for("task_detail", task_id=task.id)))

        old_status_name = task.status.name
        previous_status_id = task.status_id
        task.status = new_status

        db.session.add(
            StatusHistory(
                task=task,
                old_status_id=previous_status_id,
                new_status_id=new_status.id,
                changed_by=current_user,
            )
        )
        record_task_change(task, current_user, "status", old_status_name, new_status.name)

        db.session.commit()

        flash("Статус задачи обновлён", "success")
        return redirect(_safe_next_url(url_for("task_detail", task_id=task.id)))

    @app.route("/tasks/<int:task_id>/comments", methods=["POST"])
    @login_required
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
        db.session.flush()

        preview = cleaned["content"]
        if len(preview) > 80:
            preview = preview[:80] + "..."
        record_task_change(task, current_user, "comment", None, f"[{current_user.username}] {preview}")

        db.session.commit()

        task_url = url_for("task_detail", task_id=task.id, _external=True)
        subject = f"[Helpdesk] Новый комментарий к задаче #{task.id}"
        body = comment_notification_text(comment, task_url, "Добавлен комментарий")
        recipients = collect_comment_recipients(task, current_user)
        dispatch_notifications(recipients, subject, body)

        flash("Комментарий добавлен", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/tasks/<int:task_id>/archive", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def archive_task(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)
        if task.archived:
            flash("Задача уже находится в архиве", "info")
            return redirect(url_for("task_detail", task_id=task.id))

        task.archived = True
        task.archived_at = datetime.utcnow()
        record_task_change(task, current_user, "archived", "false", "true")
        db.session.commit()

        flash("Задача перемещена в архив", "success")
        return redirect(url_for("archive"))

    @app.route("/tasks/<int:task_id>/restore", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def restore_task(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)
        if not task.archived:
            flash("Задача уже активна", "info")
            return redirect(url_for("task_detail", task_id=task.id))

        task.archived = False
        task.archived_at = None
        record_task_change(task, current_user, "archived", "true", "false")
        db.session.commit()

        flash("Задача восстановлена из архива", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/archive")
    @login_required
    def archive():
        query = Task.query.filter_by(archived=True)
        query = filter_tasks_for_user(query, current_user)

        statuses = _ordered_statuses()
        organizations = []
        clients = []

        status_id = request.args.get("status_id", type=int)
        if status_id:
            query = query.filter_by(status_id=status_id)

        if current_user.role in (Roles.ADMIN, Roles.OPERATOR):
            organizations = allowed_organizations_for_user(current_user)

        organization_id = request.args.get("organization_id", type=int)
        if organization_id:
            if can_access_organization(current_user, organization_id):
                query = query.filter_by(organization_id=organization_id)
            else:
                flash("Нет доступа к выбранной организации", "warning")
                organization_id = None

        if current_user.role in (Roles.ADMIN, Roles.OPERATOR):
            clients = allowed_clients_for_user(current_user, organization_id)

        client_id = request.args.get("client_id", type=int)
        if client_id:
            if can_access_client(current_user, client_id):
                query = query.filter_by(client_id=client_id)
            else:
                flash("Нет доступа к выбранному сотруднику", "warning")
                client_id = None

        priority_filter = (request.args.get("priority") or "").strip().lower()
        if priority_filter in Priority.ALL:
            query = query.filter_by(priority=priority_filter)
        else:
            priority_filter = ""

        tasks = query.order_by(Task.archived_at.desc().nullslast(), Task.id.desc()).all()

        return render_template(
            "archive.html",
            tasks=tasks,
            statuses=statuses,
            organizations=organizations,
            clients=clients,
            filters={
                "status_id": status_id,
                "organization_id": organization_id,
                "client_id": client_id,
                "priority": priority_filter,
            },
            priorities=priority_choices(),
        )

    @app.route("/kanban")
    @login_required
    def kanban():
        statuses = _ordered_statuses()
        query = Task.query.filter_by(archived=False)
        query = filter_tasks_for_user(query, current_user)

        tasks = query.order_by(Task.due_date.asc(), Task.created_at.desc()).all()
        columns = {status.id: [] for status in statuses}
        for task in tasks:
            columns.setdefault(task.status_id, []).append(task)

        return render_template(
            "kanban.html",
            statuses=statuses,
            columns=columns,
            can_drag=current_user.role in (Roles.ADMIN, Roles.OPERATOR),
        )

    @app.route("/tasks/<int:task_id>/move-status", methods=["POST"])
    @login_required
    def move_status(task_id: int):
        if current_user.role not in (Roles.ADMIN, Roles.OPERATOR):
            return jsonify({"error": "forbidden"}), 403

        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            return jsonify({"error": "forbidden"}), 403
        if task.archived:
            return jsonify({"error": "archived_task"}), 400

        payload = request.get_json(silent=True) or {}
        status_id = payload.get("status_id")
        try:
            status_id = int(status_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_status_id"}), 400

        new_status = Status.query.get(status_id)
        if not new_status:
            return jsonify({"error": "status_not_found"}), 404

        if new_status.id == task.status_id:
            return jsonify({"ok": True, "task_id": task.id, "status": new_status.name})

        old_status_id = task.status_id
        old_status_name = task.status.name

        task.status = new_status
        db.session.add(
            StatusHistory(
                task=task,
                old_status_id=old_status_id,
                new_status_id=new_status.id,
                changed_by=current_user,
            )
        )
        record_task_change(task, current_user, "status", old_status_name, new_status.name)
        db.session.commit()

        return jsonify({"ok": True, "task_id": task.id, "status": new_status.name})

    @app.route("/users")
    @roles_required(Roles.ADMIN)
    def users():
        users_list = User.query.order_by(User.created_at.desc()).all()
        return render_template("users.html", users=users_list)

    @app.route("/users/create", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def user_create():
        organizations = Organization.query.order_by(Organization.name.asc()).all()

        if request.method == "POST":
            try:
                cleaned = Validators.user_payload(request.form, password_required=True)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template(
                    "user_form.html",
                    user=None,
                    organizations=organizations,
                    form_data=request.form,
                )

            if User.query.filter_by(username=cleaned["username"]).first():
                flash("Пользователь с таким логином уже существует", "danger")
                return render_template(
                    "user_form.html",
                    user=None,
                    organizations=organizations,
                    form_data=request.form,
                )

            organization = None
            if cleaned["role"] == Roles.CLIENT:
                organization = Organization.query.get(cleaned["organization_id"])
                if not organization:
                    flash("Выбранная организация не найдена", "danger")
                    return render_template(
                        "user_form.html",
                        user=None,
                        organizations=organizations,
                        form_data=request.form,
                    )

            user = User(
                username=cleaned["username"],
                role=cleaned["role"],
                email=cleaned["email"],
                telegram_chat_id=cleaned["telegram_chat_id"],
                organization=organization,
                active=bool(request.form.get("active")),
                must_change_password=bool(request.form.get("must_change_password")),
            )
            user.set_password(cleaned["password"])
            db.session.add(user)

            generated_token = None
            if user.role == Roles.CLIENT and request.form.get("create_token"):
                _, generated_token = ApiToken.create_for_user(user)

            db.session.commit()
            flash("Пользователь создан", "success")
            if generated_token:
                flash(f"API токен клиента (показывается один раз): {generated_token}", "warning")
            return redirect(url_for("users"))

        default_form = {
            "role": Roles.CLIENT,
            "active": "on",
            "organization_id": str(request.args.get("organization_id", type=int) or ""),
        }
        return render_template(
            "user_form.html",
            user=None,
            organizations=organizations,
            form_data=default_form,
        )

    @app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def user_edit(user_id: int):
        user = User.query.get_or_404(user_id)
        organizations = Organization.query.order_by(Organization.name.asc()).all()

        if request.method == "POST":
            try:
                cleaned = Validators.user_payload(request.form, password_required=False)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template(
                    "user_form.html",
                    user=user,
                    organizations=organizations,
                    form_data=request.form,
                )

            existing = User.query.filter_by(username=cleaned["username"]).first()
            if existing and existing.id != user.id:
                flash("Логин уже занят", "danger")
                return render_template(
                    "user_form.html",
                    user=user,
                    organizations=organizations,
                    form_data=request.form,
                )

            new_role = cleaned["role"]
            new_active = bool(request.form.get("active"))

            if user.id == current_user.id and not new_active:
                flash("Нельзя деактивировать самого себя", "danger")
                return render_template(
                    "user_form.html",
                    user=user,
                    organizations=organizations,
                    form_data=request.form,
                )

            if user.role == Roles.ADMIN and (new_role != Roles.ADMIN or not new_active):
                if _active_admins_count(exclude_user_id=user.id) == 0:
                    flash("В системе должен оставаться хотя бы один активный администратор", "danger")
                    return render_template(
                        "user_form.html",
                        user=user,
                        organizations=organizations,
                        form_data=request.form,
                    )

            if user.role == Roles.CLIENT and new_role != Roles.CLIENT:
                if Task.query.filter_by(client_id=user.id).count() > 0:
                    flash(
                        "Нельзя изменить роль клиента, пока за ним закреплены задачи. "
                        "Сначала переназначьте задачи.",
                        "danger",
                    )
                    return render_template(
                        "user_form.html",
                        user=user,
                        organizations=organizations,
                        form_data=request.form,
                    )

            organization = None
            if new_role == Roles.CLIENT:
                organization = Organization.query.get(cleaned["organization_id"])
                if not organization:
                    flash("Выбранная организация не найдена", "danger")
                    return render_template(
                        "user_form.html",
                        user=user,
                        organizations=organizations,
                        form_data=request.form,
                    )

                if user.role == Roles.CLIENT and user.organization_id != organization.id:
                    active_tasks_count = Task.query.filter_by(client_id=user.id, archived=False).count()
                    if active_tasks_count > 0:
                        flash(
                            "Нельзя сменить организацию клиента, пока у него есть активные задачи. "
                            "Сначала закройте или переназначьте их.",
                            "danger",
                        )
                        return render_template(
                            "user_form.html",
                            user=user,
                            organizations=organizations,
                            form_data=request.form,
                        )

            if user.role == Roles.OPERATOR and new_role != Roles.OPERATOR:
                OperatorOrganizationAccess.query.filter_by(operator_id=user.id).delete(synchronize_session=False)

            user.username = cleaned["username"]
            user.role = new_role
            user.email = cleaned["email"]
            user.telegram_chat_id = cleaned["telegram_chat_id"]
            user.organization = organization
            user.active = new_active
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
            "organization_id": str(user.organization_id or ""),
            "active": "on" if user.active else "",
            "must_change_password": "on" if user.must_change_password else "",
        }
        return render_template(
            "user_form.html",
            user=user,
            organizations=organizations,
            form_data=form_data,
        )

    @app.route("/users/<int:user_id>/tokens/new", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def user_new_token(user_id: int):
        user = User.query.get_or_404(user_id)
        if user.role != Roles.CLIENT:
            flash("Токены можно создавать только для клиентов", "danger")
            return redirect(url_for("users"))

        _, raw_token = ApiToken.create_for_user(user)
        db.session.commit()
        flash("Новый API токен сгенерирован", "success")
        flash(f"Токен (показывается один раз): {raw_token}", "warning")
        return redirect(url_for("users"))

    @app.route("/users/<int:user_id>/tokens/<int:token_id>/revoke", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def revoke_token(user_id: int, token_id: int):
        token = ApiToken.query.filter_by(id=token_id, user_id=user_id).first_or_404()
        token.revoked = True
        db.session.commit()
        flash("Токен отозван", "success")
        return redirect(url_for("users"))

    @app.route("/organizations")
    @roles_required(Roles.ADMIN)
    def organizations():
        orgs = Organization.query.order_by(Organization.name.asc()).all()
        return render_template("organizations.html", organizations=orgs)

    @app.route("/organizations/create", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def organization_create():
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            description = (request.form.get("description") or "").strip()

            if not name:
                flash("Название организации обязательно", "danger")
                return render_template("organization_form.html", organization=None, form_data=request.form)

            existing = Organization.query.filter_by(name=name).first()
            if existing:
                flash("Организация с таким названием уже существует", "danger")
                return render_template("organization_form.html", organization=None, form_data=request.form)

            organization = Organization(name=name, description=description or None)
            db.session.add(organization)
            db.session.commit()

            flash("Организация создана", "success")
            return redirect(url_for("organizations"))

        return render_template(
            "organization_form.html",
            organization=None,
            form_data={"name": "", "description": ""},
        )

    @app.route("/organizations/<int:organization_id>/edit", methods=["GET", "POST"])
    @roles_required(Roles.ADMIN)
    def organization_edit(organization_id: int):
        organization = Organization.query.get_or_404(organization_id)
        clients = (
            User.query.filter_by(role=Roles.CLIENT, organization_id=organization.id)
            .order_by(User.username.asc())
            .all()
        )

        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            description = (request.form.get("description") or "").strip()

            if not name:
                flash("Название организации обязательно", "danger")
                return render_template(
                    "organization_form.html",
                    organization=organization,
                    clients=clients,
                    form_data=request.form,
                )

            duplicate = Organization.query.filter_by(name=name).first()
            if duplicate and duplicate.id != organization.id:
                flash("Организация с таким названием уже существует", "danger")
                return render_template(
                    "organization_form.html",
                    organization=organization,
                    clients=clients,
                    form_data=request.form,
                )

            organization.name = name
            organization.description = description or None
            db.session.commit()

            flash("Организация обновлена", "success")
            return redirect(url_for("organizations"))

        form_data = {
            "name": organization.name,
            "description": organization.description or "",
        }
        return render_template(
            "organization_form.html",
            organization=organization,
            clients=clients,
            form_data=form_data,
        )

    @app.route("/organizations/<int:organization_id>/delete", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def organization_delete(organization_id: int):
        organization = Organization.query.get_or_404(organization_id)

        if organization.users.count() > 0:
            flash("Нельзя удалить организацию: есть связанные пользователи", "danger")
            return redirect(url_for("organizations"))

        if organization.tasks.count() > 0:
            flash("Нельзя удалить организацию: есть связанные задачи", "danger")
            return redirect(url_for("organizations"))

        db.session.delete(organization)
        db.session.commit()

        flash("Организация удалена", "success")
        return redirect(url_for("organizations"))

    @app.route("/access")
    @roles_required(Roles.ADMIN)
    def access_management():
        operators = User.query.filter_by(role=Roles.OPERATOR).order_by(User.username.asc()).all()
        organizations = Organization.query.order_by(Organization.name.asc()).all()

        access_rows = OperatorOrganizationAccess.query.all()
        access_map: dict[int, set[int]] = {}
        for row in access_rows:
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

        selected_raw = request.form.getlist("organization_ids")
        selected_ids: set[int] = set()
        for value in selected_raw:
            try:
                selected_ids.add(int(value))
            except ValueError:
                continue

        valid_organization_ids = {
            row[0]
            for row in Organization.query.with_entities(Organization.id).all()
        }
        selected_ids &= valid_organization_ids

        OperatorOrganizationAccess.query.filter_by(operator_id=operator.id).delete(synchronize_session=False)
        for organization_id in selected_ids:
            db.session.add(
                OperatorOrganizationAccess(
                    operator_id=operator.id,
                    organization_id=organization_id,
                )
            )

        db.session.commit()
        flash(f"Права доступа обновлены для оператора {operator.username}", "success")
        return redirect(url_for("access_management"))

    @app.route("/statuses")
    @roles_required(Roles.ADMIN)
    def statuses():
        statuses_list = _ordered_statuses()
        return render_template("statuses.html", statuses=statuses_list)

    @app.route("/statuses/create", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def status_create():
        name = (request.form.get("name") or "").strip()
        sort_order_raw = (request.form.get("sort_order") or "").strip()

        if not name:
            flash("Название статуса обязательно", "danger")
            return redirect(url_for("statuses"))

        if Status.query.filter_by(name=name).first():
            flash("Такой статус уже существует", "danger")
            return redirect(url_for("statuses"))

        if sort_order_raw:
            try:
                sort_order = int(sort_order_raw)
            except ValueError:
                flash("Порядок сортировки должен быть числом", "danger")
                return redirect(url_for("statuses"))
        else:
            last_status = Status.query.order_by(Status.sort_order.desc(), Status.id.desc()).first()
            sort_order = (last_status.sort_order + 10) if last_status else 10

        db.session.add(Status(name=name, sort_order=sort_order))
        db.session.commit()

        flash("Статус добавлен", "success")
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

        duplicate = Status.query.filter_by(name=name).first()
        if duplicate and duplicate.id != status.id:
            flash("Статус с таким именем уже существует", "danger")
            return redirect(url_for("statuses"))

        try:
            sort_order = int(sort_order_raw)
        except ValueError:
            flash("Порядок сортировки должен быть числом", "danger")
            return redirect(url_for("statuses"))

        status.name = name
        status.sort_order = sort_order
        db.session.commit()

        flash("Статус обновлён", "success")
        return redirect(url_for("statuses"))

    @app.route("/statuses/<int:status_id>/delete", methods=["POST"])
    @roles_required(Roles.ADMIN)
    def status_delete(status_id: int):
        status = Status.query.get_or_404(status_id)

        if Status.query.count() <= 1:
            flash("Нельзя удалить последний статус", "danger")
            return redirect(url_for("statuses"))

        if status.tasks.count() > 0:
            flash("Статус нельзя удалить: есть задачи с этим статусом", "danger")
            return redirect(url_for("statuses"))

        db.session.delete(status)
        db.session.commit()

        flash("Статус удалён", "success")
        return redirect(url_for("statuses"))

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
    app.run(debug=True)
