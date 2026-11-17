import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import require_user
from app.database import get_engine, initialize_local_database


@asynccontextmanager
async def lifespan(_app):
    if os.getenv("DATABASE_URL", "").startswith("sqlite:"):
        initialize_local_database()
    yield


app = FastAPI(title="CodeAlpha Project Manager", lifespan=lifespan)
origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "http://localhost:7860,http://127.0.0.1:7860").split(",") if item.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-User-Name", "X-User-Email"])
app.mount("/static", StaticFiles(directory="frontend"), name="frontend")


@app.get("/")
def index():
    return FileResponse("frontend/index.html")


@app.get("/api/public-config")
def public_config():
    return {"clerk_publishable_key": os.getenv("CLERK_PUBLISHABLE_KEY", "")}


@app.get("/api/health")
def health():
    return {"ok": True, "configured": {"clerk": bool(os.getenv("CLERK_SECRET_KEY")), "database": bool(os.getenv("DATABASE_URL"))}}


def profile_values(request: Request, user_id: str):
    # These client-provided fields are display-only. Authorization always uses
    # the subject from Clerk's verified session token.
    name = (request.headers.get("x-user-name") or "Team member")[:100]
    email = (request.headers.get("x-user-email") or "")[:254]
    return {"id": user_id, "name": name, "email": email}


def touch_profile(conn, request: Request, user_id: str):
    values = profile_values(request, user_id)
    conn.execute(text("""INSERT INTO users (id, name, email) VALUES (:id, :name, :email)
        ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email"""), values)
    return values


def member_project(conn, pid: int, uid: str):
    row = conn.execute(text("""SELECT 1 FROM projects p LEFT JOIN project_members m ON p.id=m.project_id
        WHERE p.id=:pid AND (p.owner_id=:uid OR m.user_id=:uid)"""), {"pid": pid, "uid": uid}).first()
    if not row:
        raise HTTPException(404, "Project not found")


def member_task(conn, tid: int, uid: str):
    row = conn.execute(text("SELECT project_id FROM tasks WHERE id=:tid"), {"tid": tid}).first()
    if not row:
        raise HTTPException(404, "Task not found")
    member_project(conn, row.project_id, uid)
    return row.project_id


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default="", max_length=2000)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default="", max_length=4000)
    priority: str = Field(default="medium", pattern="^(low|medium|high)$")
    assignee_id: Optional[str] = Field(default=None, min_length=1, max_length=255)


class TaskUpdate(BaseModel):
    status: Optional[str] = Field(default=None, pattern="^(todo|in_progress|done)$")
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    priority: Optional[str] = Field(default=None, pattern="^(low|medium|high)$")
    assignee_id: Optional[str] = Field(default=None, min_length=1, max_length=255)


class CommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


@app.get("/api/me")
def me(request: Request, user_id: str = Depends(require_user)):
    with get_engine().begin() as conn:
        return touch_profile(conn, request, user_id)


@app.get("/api/users")
def list_users(user_id: str = Depends(require_user)):
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT id, name FROM users ORDER BY name")).mappings().all()
        return [dict(row) for row in rows]


@app.get("/api/projects")
def list_projects(user_id: str = Depends(require_user)):
    with get_engine().connect() as conn:
        rows = conn.execute(text("""SELECT DISTINCT p.* FROM projects p LEFT JOIN project_members pm ON p.id=pm.project_id
            WHERE p.owner_id=:uid OR pm.user_id=:uid ORDER BY p.id DESC"""), {"uid": user_id}).mappings().all()
        return [dict(row) for row in rows]


@app.post("/api/projects")
def create_project(project: ProjectCreate, user_id: str = Depends(require_user)):
    with get_engine().begin() as conn:
        result = conn.execute(text("INSERT INTO projects (name, description, owner_id) VALUES (:name, :description, :owner_id) RETURNING id"), {"name": project.name, "description": project.description or "", "owner_id": user_id})
        return dict(conn.execute(text("SELECT * FROM projects WHERE id=:id"), {"id": result.scalar_one()}).mappings().one())


@app.get("/api/projects/{pid}")
def get_project(pid: int, user_id: str = Depends(require_user)):
    with get_engine().connect() as conn:
        row = conn.execute(text("""SELECT p.* FROM projects p LEFT JOIN project_members m ON p.id=m.project_id
            WHERE p.id=:pid AND (p.owner_id=:uid OR m.user_id=:uid)"""), {"pid": pid, "uid": user_id}).mappings().first()
        if not row:
            raise HTTPException(404, "Project not found")
        return dict(row)


@app.post("/api/projects/{pid}/members")
def add_member(pid: int, user_id_to_add: str, user_id: str = Depends(require_user)):
    with get_engine().begin() as conn:
        project = conn.execute(text("SELECT owner_id FROM projects WHERE id=:pid"), {"pid": pid}).mappings().first()
        if not project:
            raise HTTPException(404, "Project not found")
        if project["owner_id"] != user_id:
            raise HTTPException(403, "Only the project owner can add members")
        if not conn.execute(text("SELECT 1 FROM users WHERE id=:id"), {"id": user_id_to_add}).first():
            raise HTTPException(404, "User not found")
        conn.execute(text("INSERT INTO project_members (project_id, user_id) VALUES (:pid, :uid) ON CONFLICT DO NOTHING"), {"pid": pid, "uid": user_id_to_add})
    return {"ok": True}


@app.get("/api/projects/{pid}/tasks")
def list_tasks(pid: int, user_id: str = Depends(require_user)):
    with get_engine().connect() as conn:
        member_project(conn, pid, user_id)
        rows = conn.execute(text("SELECT * FROM tasks WHERE project_id=:pid ORDER BY id DESC"), {"pid": pid}).mappings().all()
        return [dict(row) for row in rows]


@app.post("/api/projects/{pid}/tasks")
def create_task(pid: int, task: TaskCreate, user_id: str = Depends(require_user)):
    with get_engine().begin() as conn:
        member_project(conn, pid, user_id)
        if task.assignee_id and not conn.execute(text("""SELECT 1 FROM project_members WHERE project_id=:pid AND user_id=:uid
            UNION SELECT 1 FROM projects WHERE id=:pid AND owner_id=:uid"""), {"pid": pid, "uid": task.assignee_id}).first():
            raise HTTPException(400, "Assignee must belong to the project")
        values = task.model_dump()
        values["description"] = values["description"] or ""
        result = conn.execute(text("""INSERT INTO tasks (project_id,title,description,priority,assignee_id)
            VALUES (:pid,:title,:description,:priority,:assignee_id) RETURNING id"""), {"pid": pid, **values})
        return dict(conn.execute(text("SELECT * FROM tasks WHERE id=:id"), {"id": result.scalar_one()}).mappings().one())


@app.patch("/api/tasks/{tid}")
def update_task(tid: int, task: TaskUpdate, user_id: str = Depends(require_user)):
    fields = {key: value for key, value in task.model_dump().items() if value is not None}
    with get_engine().begin() as conn:
        pid = member_task(conn, tid, user_id)
        if fields.get("assignee_id") and not conn.execute(text("""SELECT 1 FROM project_members WHERE project_id=:pid AND user_id=:uid
            UNION SELECT 1 FROM projects WHERE id=:pid AND owner_id=:uid"""), {"pid": pid, "uid": fields["assignee_id"]}).first():
            raise HTTPException(400, "Assignee must belong to the project")
        if fields:
            sets = ", ".join(f"{key}=:{key}" for key in fields)
            conn.execute(text(f"UPDATE tasks SET {sets} WHERE id=:tid"), {**fields, "tid": tid})
        return dict(conn.execute(text("SELECT * FROM tasks WHERE id=:tid"), {"tid": tid}).mappings().one())


@app.get("/api/tasks/{tid}/comments")
def list_comments(tid: int, user_id: str = Depends(require_user)):
    with get_engine().connect() as conn:
        member_task(conn, tid, user_id)
        rows = conn.execute(text("""SELECT c.*, u.name AS user_name FROM comments c JOIN users u ON c.user_id=u.id
            WHERE c.task_id=:tid ORDER BY c.id"""), {"tid": tid}).mappings().all()
        return [dict(row) for row in rows]


@app.post("/api/tasks/{tid}/comments")
def create_comment(tid: int, comment: CommentCreate, request: Request, user_id: str = Depends(require_user)):
    with get_engine().begin() as conn:
        member_task(conn, tid, user_id)
        touch_profile(conn, request, user_id)
        result = conn.execute(text("INSERT INTO comments (task_id,user_id,content) VALUES (:tid,:uid,:content) RETURNING id"), {"tid": tid, "uid": user_id, "content": comment.content})
        row = conn.execute(text("""SELECT c.*, u.name AS user_name FROM comments c JOIN users u ON c.user_id=u.id
            WHERE c.id=:id"""), {"id": result.scalar_one()}).mappings().one()
        return dict(row)
