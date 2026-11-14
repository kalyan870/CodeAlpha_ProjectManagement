import os, json, hashlib, hmac, secrets
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="CodeAlpha Project Manager")
origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "http://localhost:7860,http://127.0.0.1:7860").split(",") if item.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])
app.mount("/static", StaticFiles(directory="frontend"), name="frontend")

@app.get("/")
def index():
    return FileResponse("frontend/index.html")

DATA_DIR = os.getenv("APP_DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "pm.db")

def get_db():
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            owner_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS project_members (
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (project_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'todo',
            priority TEXT DEFAULT 'medium',
            assignee_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
    """)
    conn.commit()
    conn.close()

init_db()

# Models
class UserCreate(BaseModel): name: str = Field(min_length=1, max_length=100); email: str = Field(min_length=3, max_length=254); password: str = Field(min_length=10, max_length=256)
class UserLogin(BaseModel): email: str = Field(min_length=3, max_length=254); password: str = Field(min_length=1, max_length=256)
class ProjectCreate(BaseModel): name: str = Field(min_length=1, max_length=120); description: Optional[str] = Field(default="", max_length=2000)
class TaskCreate(BaseModel): title: str = Field(min_length=1, max_length=200); description: Optional[str] = Field(default="", max_length=4000); priority: str = Field(default="medium", pattern="^(low|medium|high)$"); assignee_id: Optional[int] = Field(default=None, gt=0)
class TaskUpdate(BaseModel): status: Optional[str] = Field(default=None, pattern="^(todo|in_progress|done)$"); title: Optional[str] = Field(default=None, min_length=1, max_length=200); description: Optional[str] = Field(default=None, max_length=4000); priority: Optional[str] = Field(default=None, pattern="^(low|medium|high)$"); assignee_id: Optional[int] = Field(default=None, gt=0)
class CommentCreate(BaseModel): content: str = Field(min_length=1, max_length=2000)

def hash_pw(pw):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 310_000)
    return "pbkdf2_sha256$310000$%s$%s" % (salt.hex(), digest.hex())

def verify_pw(pw, stored):
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, rounds, salt, expected = stored.split("$")
            actual = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds)).hex()
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False
    # Upgrade legacy SHA-256 records after a successful login.
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)

tokens = {}

def get_user(auth: str = Header(default="")):
    token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    uid = tokens.get(token)
    if not uid: raise HTTPException(401, "Not authenticated")
    return uid

# Auth
@app.post("/api/register")
def register(u: UserCreate):
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (u.email,)).fetchone():
        conn.close(); raise HTTPException(400, "Email exists")
    conn.execute("INSERT INTO users (name, email, password) VALUES (?,?,?)", (u.name.strip(), u.email.strip().lower(), hash_pw(u.password)))
    conn.commit()
    uid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    token = secrets.token_hex(32); tokens[token] = uid
    user = dict(conn.execute("SELECT id, name, email FROM users WHERE id=?", (uid,)).fetchone())
    conn.close()
    return {"token": token, "user": user}

@app.post("/api/login")
def login(u: UserLogin):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (u.email.strip().lower(),)).fetchone()
    if not row or not verify_pw(u.password, row["password"]):
        conn.close(); raise HTTPException(401, "Invalid credentials")
    if not row["password"].startswith("pbkdf2_sha256$"):
        conn.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(u.password), row["id"]))
        conn.commit()
    user = dict(row)
    token = secrets.token_hex(32); tokens[token] = user["id"]
    return {"token": token, "user": {"id": user["id"], "name": user["name"], "email": user["email"]}}

@app.get("/api/me")
def me(authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    row = dict(conn.execute("SELECT id, name, email FROM users WHERE id=?", (uid,)).fetchone())
    conn.close()
    return row

@app.get("/api/users")
def list_users(authorization: str = Header(default="")):
    get_user(authorization)
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT id, name FROM users").fetchall()]
    conn.close()
    return rows

# Projects
@app.get("/api/projects")
def list_projects(authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT p.* FROM projects p
        LEFT JOIN project_members pm ON p.id = pm.project_id
        WHERE p.owner_id=? OR pm.user_id=?
        ORDER BY p.id DESC
    """, (uid, uid)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/projects")
def create_project(p: ProjectCreate, authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    conn.execute("INSERT INTO projects (name, description, owner_id) VALUES (?,?,?)", (p.name, p.description, uid))
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = dict(conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())
    conn.close()
    return row

@app.get("/api/projects/{pid}")
def get_project(pid: int, authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    row = conn.execute("SELECT p.* FROM projects p LEFT JOIN project_members m ON p.id=m.project_id WHERE p.id=? AND (p.owner_id=? OR m.user_id=?)", (pid, uid, uid)).fetchone()
    conn.close()
    if not row: raise HTTPException(404, "Not found")
    return dict(row)

@app.post("/api/projects/{pid}/members")
def add_member(pid: int, user_id: int, authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    project = conn.execute("SELECT owner_id FROM projects WHERE id=?", (pid,)).fetchone()
    user = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not project: conn.close(); raise HTTPException(404, "Project not found")
    if project["owner_id"] != uid: conn.close(); raise HTTPException(403, "Only the project owner can add members")
    if not user: conn.close(); raise HTTPException(404, "User not found")
    conn.execute("INSERT OR IGNORE INTO project_members (project_id, user_id) VALUES (?,?)", (pid, user_id))
    conn.commit()
    conn.close()
    return {"ok": True}

def require_project_member(conn, pid, uid):
    row = conn.execute("SELECT 1 FROM projects p LEFT JOIN project_members m ON p.id=m.project_id WHERE p.id=? AND (p.owner_id=? OR m.user_id=?)", (pid, uid, uid)).fetchone()
    if not row: raise HTTPException(404, "Project not found")

def require_task_member(conn, tid, uid):
    row = conn.execute("SELECT t.project_id FROM tasks t WHERE t.id=?", (tid,)).fetchone()
    if not row: raise HTTPException(404, "Task not found")
    require_project_member(conn, row["project_id"], uid)

# Tasks
@app.get("/api/projects/{pid}/tasks")
def list_tasks(pid: int, authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    require_project_member(conn, pid, uid)
    rows = [dict(r) for r in conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY id DESC", (pid,)).fetchall()]
    conn.close()
    return rows

@app.post("/api/projects/{pid}/tasks")
def create_task(pid: int, t: TaskCreate, authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    require_project_member(conn, pid, uid)
    if t.assignee_id is not None:
        member = conn.execute("SELECT 1 FROM project_members WHERE project_id=? AND user_id=? UNION SELECT 1 FROM projects WHERE id=? AND owner_id=?", (pid, t.assignee_id, pid, t.assignee_id)).fetchone()
        if not member: conn.close(); raise HTTPException(400, "Assignee must belong to the project")
    conn.execute("INSERT INTO tasks (project_id, title, description, priority, assignee_id) VALUES (?,?,?,?,?)",
                 (pid, t.title, t.description, t.priority, t.assignee_id))
    conn.commit()
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    conn.close()
    return row

@app.patch("/api/tasks/{tid}")
def update_task(tid: int, t: TaskUpdate, authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    require_task_member(conn, tid, uid)
    fields = {k: v for k, v in t.model_dump().items() if v is not None}
    if fields.get("assignee_id") is not None:
        task = conn.execute("SELECT project_id FROM tasks WHERE id=?", (tid,)).fetchone()
        member = conn.execute("SELECT 1 FROM project_members WHERE project_id=? AND user_id=? UNION SELECT 1 FROM projects WHERE id=? AND owner_id=?", (task["project_id"], fields["assignee_id"], task["project_id"], fields["assignee_id"])).fetchone()
        if not member: conn.close(); raise HTTPException(400, "Assignee must belong to the project")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [tid]
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        conn.commit()
    result = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not result: raise HTTPException(404, "Task not found")
    return dict(result)

# Comments
@app.get("/api/tasks/{tid}/comments")
def list_comments(tid: int, authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    require_task_member(conn, tid, uid)
    rows = [dict(r) for r in conn.execute(
        "SELECT c.*, u.name as user_name FROM comments c JOIN users u ON c.user_id=u.id WHERE c.task_id=? ORDER BY c.id", (tid,)).fetchall()]
    conn.close()
    return rows

@app.post("/api/tasks/{tid}/comments")
def create_comment(tid: int, c: CommentCreate, authorization: str = Header(default="")):
    uid = get_user(authorization)
    conn = get_db()
    require_task_member(conn, tid, uid)
    conn.execute("INSERT INTO comments (task_id, user_id, content) VALUES (?,?,?)", (tid, uid, c.content))
    conn.commit()
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = dict(conn.execute(
        "SELECT c.*, u.name as user_name FROM comments c JOIN users u ON c.user_id=u.id WHERE c.id=?", (cid,)).fetchone())
    conn.close()
    return row
