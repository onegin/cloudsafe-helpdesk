from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from models import User, db


auth_bp = Blueprint("auth", __name__)


def _safe_next_url(default_endpoint: str = "index") -> str:
    next_url = request.args.get("next") or request.form.get("next")
    if not next_url:
        return url_for(default_endpoint)

    parsed = urlparse(next_url)
    if parsed.netloc:
        return url_for(default_endpoint)
    return next_url


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if user and user.active and user.check_password(password):
            login_user(user)
            if user.must_change_password:
                flash("Вы вошли с временным паролем. Смените пароль.", "warning")
                return redirect(url_for("auth.change_password"))
            return redirect(_safe_next_url())

        flash("Неверный логин или пароль", "danger")

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Вы вышли из системы", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not current_user.check_password(current_password):
            flash("Текущий пароль введён неверно", "danger")
            return render_template("change_password.html")

        if len(new_password) < 4:
            flash("Новый пароль должен быть длиной минимум 4 символа", "danger")
            return render_template("change_password.html")

        if new_password != confirm_password:
            flash("Подтверждение пароля не совпадает", "danger")
            return render_template("change_password.html")

        current_user.set_password(new_password)
        current_user.must_change_password = False
        db.session.commit()
        flash("Пароль успешно обновлён", "success")
        return redirect(url_for("index"))

    return render_template("change_password.html")
