"""Database engine, session factory and declarative base."""
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

DATABASE_URL = settings.database_url
IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_MYSQL = DATABASE_URL.startswith("mysql")

connect_args: dict = {}
engine_options: dict = {"pool_pre_ping": True}

if IS_SQLITE:
    connect_args["check_same_thread"] = False
elif IS_MYSQL:
    # TiDB speaks the MySQL protocol. utf8mb4 keeps emoji and non-Latin text
    # intact; recycle connections before TiDB closes idle ones.
    connect_args["charset"] = "utf8mb4"
    engine_options["pool_recycle"] = 1800
    if settings.database_ssl:
        ssl_args: dict = {"check_hostname": settings.database_ssl_verify_identity}
        ca = settings.database_ssl_ca
        if not ca:
            # python:3.11-slim (Debian) stores the system root CAs here.
            default_ca = Path("/etc/ssl/certs/ca-certificates.crt")
            ca = str(default_ca) if default_ca.is_file() else None
        if ca:
            ssl_args["ca"] = ca
        connect_args["ssl"] = ssl_args

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    **engine_options,
)

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover - trivial
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
