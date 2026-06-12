import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT_DIR / ".env"

load_dotenv(ENV_PATH)


def _database_url() -> str:
    return (
        os.getenv("DATABASE_URL")
        or os.getenv("URL_DO_BANCO_DE_DADOS")
        or "sqlite:///./aumigao.db"
    ).strip().strip('"').strip("'")


SQLALCHEMY_DATABASE_URL = _database_url()


def get_database_diagnostics() -> dict[str, str]:
    diagnostics = {
        "database_url": SQLALCHEMY_DATABASE_URL,
        "env_path": str(ENV_PATH),
    }
    parsed = urlparse(SQLALCHEMY_DATABASE_URL)
    if parsed.scheme == "sqlite":
        raw_path = parsed.path or ""
        if SQLALCHEMY_DATABASE_URL.startswith("sqlite:///./"):
            sqlite_path = ROOT_DIR / SQLALCHEMY_DATABASE_URL.replace("sqlite:///./", "", 1)
        elif SQLALCHEMY_DATABASE_URL.startswith("sqlite:///"):
            sqlite_path = Path(raw_path)
        else:
            sqlite_path = ROOT_DIR / raw_path.lstrip("/")
        diagnostics["sqlite_path"] = str(sqlite_path.resolve())
    return diagnostics


def mask_database_url(database_url: str = SQLALCHEMY_DATABASE_URL) -> str:
    parsed = urlparse(database_url)
    if not parsed.password:
        return database_url
    return database_url.replace(f":{parsed.password}@", ":***@")


connect_args = {"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {}
engine_kwargs = {}
if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 300, "pool_size": 5, "max_overflow": 10}
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
