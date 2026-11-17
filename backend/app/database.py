import os

from sqlalchemy import Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text, create_engine, func

metadata = MetaData()
users = Table("users", metadata,
    Column("id", String(255), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("email", String(254), nullable=False, server_default=""),
)
projects = Table("projects", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(120), nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("owner_id", String(255), ForeignKey("users.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
project_members = Table("project_members", metadata,
    Column("project_id", Integer, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", String(255), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)
tasks = Table("tasks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("title", String(200), nullable=False), Column("description", Text, nullable=False, server_default=""),
    Column("status", String(20), nullable=False, server_default="todo"), Column("priority", String(20), nullable=False, server_default="medium"),
    Column("assignee_id", String(255), ForeignKey("users.id")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
comments = Table("comments", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", String(255), ForeignKey("users.id"), nullable=False),
    Column("content", Text, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = os.getenv("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL is required")
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def initialize_local_database():
    if not os.getenv("DATABASE_URL", "").startswith("sqlite:"):
        raise RuntimeError("Automatic schema creation is for local SQLite only; apply the Neon migration")
    metadata.create_all(get_engine())


def reset_engine_for_tests():
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
