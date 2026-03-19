import os


class Config:
    """Application configuration loaded from environment variables."""

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///ticket_system.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
