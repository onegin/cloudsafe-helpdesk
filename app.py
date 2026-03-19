from __future__ import annotations

from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required

from api import api_bp
from auth import auth_bp
from config import Config
from forms import ValidationError, Validators
from models import ApiToken, Roles, Status, StatusHistory, Task, User, db


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


def can_view_task(user: User, task: Task) -> bool:
    if user.role in (Roles.ADMIN, Roles.OPERATOR):
        return True
    return task.client_id == user.id


def _active_admins_count(exclude_user_id: int | None = None) -> int:
    query = User.query.filter_by(role=Roles.ADMIN, active=True)
    if exclude_user_id is not None:
        query = query.filter(User.id != exclude_user_id)
    return query.count()


def _ordered_statuses():
    return Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).all()


def _first_status() -> Status | None:
    return Status.query.order_by(Status.sort_order.asc(), Status.id.asc()).first()


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
        admin_user.must_change_password = admin_password == "admin"
        admin_user.set_password(admin_password)

    if Status.query.count() == 0:
        default_statuses = ["Новая", "В работе", "Завершена"]
        for index, name in enumerate(default_statuses, start=1):
            db.session.add(Status(name=name, sort_order=index * 10))

    db.session.commit()


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)

    with app.app_context():
        db.create_all()
        bootstrap_defaults(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        return User.query.get(int(user_id))

    @app.context_processor
    def inject_globals():
        return {"Roles": Roles}

    @app.template_filter("datetime")
    def format_datetime(value):
        if not value:
            return "-"
        return value.strftime("%Y-%m-%d %H:%M")

    @app.route("/")
    def home():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        return redirect(url_for("auth.login"))

    @app.route("/tasks")
    @login_required
    def index():
        statuses = _ordered_statuses()
        clients = User.query.filter_by(role=Roles.CLIENT, active=True).order_by(User.username.asc()).all()

        query = Task.query.filter_by(archived=False)
        if current_user.role == Roles.CLIENT:
            query = query.filter_by(client_id=current_user.id)

        status_id = request.args.get("status_id", type=int)
        if status_id:
            query = query.filter_by(status_id=status_id)

        client_id = request.args.get("client_id", type=int)
        if current_user.role in (Roles.ADMIN, Roles.OPERATOR) and client_id:
            query = query.filter_by(client_id=client_id)

        due_date_filter = (request.args.get("due_date") or "").strip()
        if due_date_filter:
            try:
                query = query.filter_by(due_date=Validators.parse_due_date(due_date_filter))
            except ValidationError:
                flash("Некорректная дата фильтра, используйте YYYY-MM-DD", "warning")

        tasks = query.order_by(Task.due_date.asc(), Task.created_at.desc()).all()

        return render_template(
            "index.html",
            tasks=tasks,
            statuses=statuses,
            clients=clients,
            filters={
                "status_id": status_id,
                "client_id": client_id,
                "due_date": due_date_filter,
            },
        )

    @app.route("/tasks/create", methods=["GET", "POST"])
    @login_required
    def create_task():
        clients = User.query.filter_by(role=Roles.CLIENT, active=True).order_by(User.username.asc()).all()
        statuses = _ordered_statuses()
        if not statuses:
            flash("Невозможно создать задачу без настроенных статусов", "danger")
            return redirect(url_for("index"))

        if request.method == "POST":
            require_client = current_user.role != Roles.CLIENT
            try:
                cleaned = Validators.task_payload(request.form, require_client=require_client)
            except ValidationError as exc:
                flash(str(exc), "danger")
                return render_template(
                    "task_form.html",
                    clients=clients,
                    task=None,
                    form_data=request.form,
                )

            if current_user.role == Roles.CLIENT:
                client = current_user
            else:
                client = User.query.filter_by(
                    id=cleaned["client_id"],
                    role=Roles.CLIENT,
                    active=True,
                ).first()
                if not client:
                    flash("Выбранный клиент не найден", "danger")
                    return render_template(
                        "task_form.html",
                        clients=clients,
                        task=None,
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
                client=client,
                status=initial_status,
                created_by=current_user,
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
            db.session.commit()

            flash("Задача успешно создана", "success")
            return redirect(url_for("task_detail", task_id=task.id))

        return render_template("task_form.html", clients=clients, task=None, form_data={})

    @app.route("/tasks/<int:task_id>")
    @login_required
    def task_detail(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not can_view_task(current_user, task):
            abort(403)

        statuses = _ordered_statuses()
        clients = []
        if current_user.role in (Roles.ADMIN, Roles.OPERATOR):
            clients = User.query.filter_by(role=Roles.CLIENT, active=True).order_by(User.username.asc()).all()

        return render_template(
            "task.html",
            task=task,
            statuses=statuses,
            clients=clients,
            can_edit=current_user.role in (Roles.ADMIN, Roles.OPERATOR) and not task.archived,
            can_change_status=current_user.role in (Roles.ADMIN, Roles.OPERATOR) and not task.archived,
            can_archive=current_user.role in (Roles.ADMIN, Roles.OPERATOR) and not task.archived,
            can_restore=current_user.role in (Roles.ADMIN, Roles.OPERATOR) and task.archived,
        )

    @app.route("/tasks/<int:task_id>/update", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def task_update(task_id: int):
        task = Task.query.get_or_404(task_id)
        if task.archived:
            flash("Архивную задачу редактировать нельзя", "warning")
            return redirect(url_for("task_detail", task_id=task.id))

        try:
            cleaned = Validators.task_payload(request.form, require_client=True)
        except ValidationError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        client = User.query.filter_by(
            id=cleaned["client_id"],
            role=Roles.CLIENT,
            active=True,
        ).first()
        if not client:
            flash("Клиент не найден", "danger")
            return redirect(url_for("task_detail", task_id=task.id))

        task.theme = cleaned["theme"]
        task.content = cleaned["content"]
        task.due_date = cleaned["due_date"]
        task.client = client
        db.session.commit()

        flash("Задача обновлена", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/tasks/<int:task_id>/change-status", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def task_change_status(task_id: int):
        task = Task.query.get_or_404(task_id)
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
        db.session.commit()

        flash("Статус задачи обновлён", "success")
        return redirect(_safe_next_url(url_for("task_detail", task_id=task.id)))

    @app.route("/tasks/<int:task_id>/archive", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def archive_task(task_id: int):
        task = Task.query.get_or_404(task_id)
        if task.archived:
            flash("Задача уже находится в архиве", "info")
            return redirect(url_for("task_detail", task_id=task.id))

        task.archived = True
        task.archived_at = datetime.utcnow()
        db.session.commit()

        flash("Задача перемещена в архив", "success")
        return redirect(url_for("archive"))

    @app.route("/tasks/<int:task_id>/restore", methods=["POST"])
    @roles_required(Roles.ADMIN, Roles.OPERATOR)
    def restore_task(task_id: int):
        task = Task.query.get_or_404(task_id)
        if not task.archived:
            flash("Задача уже активна", "info")
            return redirect(url_for("task_detail", task_id=task.id))

        task.archived = False
        task.archived_at = None
        db.session.commit()

        flash("Задача восстановлена из архива", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/archive")
    @login_required
    def archive():
        query = Task.query.filter_by(archived=True)
        if current_user.role == Roles.CLIENT:
            query = query.filter_by(client_id=current_user.id)

        status_id = request.args.get("status_id", type=int)
        if status_id:
            query = query.filter_by(status_id=status_id)

        tasks = query.order_by(Task.archived_at.desc().nullslast(), Task.id.desc()).all()
        statuses = _ordered_statuses()

        return render_template("archive.html", tasks=tasks, statuses=statuses, status_id=status_id)

    @app.route("/kanban")
    @login_required
    def kanban():
        statuses = _ordered_statuses()
        query = Task.query.filter_by(archived=False)
        if current_user.role == Roles.CLIENT:
            query = query.filter_by(client_id=current_user.id)

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
        task.status = new_status
        db.session.add(
            StatusHistory(
                task=task,
                old_status_id=old_status_id,
                new_status_id=new_status.id,
                changed_by=current_user,
            )
        )
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

        return render_template("user_form.html", user=None, form_data={})

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

            existing = User.query.filter_by(username=cleaned["username"]).first()
            if existing and existing.id != user.id:
                flash("Логин уже занят", "danger")
                return render_template("user_form.html", user=user, form_data=request.form)

            new_role = cleaned["role"]
            new_active = bool(request.form.get("active"))

            if user.id == current_user.id and not new_active:
                flash("Нельзя деактивировать самого себя", "danger")
                return render_template("user_form.html", user=user, form_data=request.form)

            if user.role == Roles.ADMIN and (new_role != Roles.ADMIN or not new_active):
                if _active_admins_count(exclude_user_id=user.id) == 0:
                    flash("В системе должен оставаться хотя бы один активный администратор", "danger")
                    return render_template("user_form.html", user=user, form_data=request.form)

            user.username = cleaned["username"]
            user.role = new_role
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
            "active": "on" if user.active else "",
            "must_change_password": "on" if user.must_change_password else "",
        }
        return render_template("user_form.html", user=user, form_data=form_data)

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
