import os, json, hashlib, secrets
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="CodeAlpha Project Manager")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="frontend"), name="frontend")

@app.get("/")
def index():
    return FileResponse("frontend/index.html")

DB = "/data/pm.db"
os.makedirs("/data", exist_ok=True)

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
class UserCreate(BaseModel): name: str; email: str; password: str
class UserLogin(BaseModel): email: str; password: str
class ProjectCreate(BaseModel): name: str; description: Optional[str] = ""
class TaskCreate(BaseModel): title: str; description: Optional[str] = ""; priority: Optional[str] = "medium"; assignee_id: Optional[int] = None
class TaskUpdate(BaseModel): status: Optional[str] = None; title: Optional[str] = None; description: Optional[str] = None; priority: Optional[str] = None; assignee_id: Optional[int] = None
class CommentCreate(BaseModel): content: str

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

tokens = {}

def get_user(auth: str):
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
    conn.execute("INSERT INTO users (name, email, password) VALUES (?,?,?)", (u.name, u.email, hash_pw(u.password)))
    conn.commit()
    uid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    token = secrets.token_hex(32); tokens[token] = uid
    user = dict(conn.execute("SELECT id, name, email FROM users WHERE id=?", (uid,)).fetchone())
    conn.close()
    return {"token": token, "user": user}

@app.post("/api/login")
def login(u: UserLogin):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email=? AND password=?", (u.email, hash_pw(u.password))).fetchone()
    conn.close()
    if not row: raise HTTPException(401, "Invalid credentials")
    user = dict(row)
    token = secrets.token_hex(32); tokens[token] = user["id"]
    return {"token": token, "user": {"id": user["id"], "name": user["name"], "email": user["email"]}}

@app.get("/api/me")
def me(authorization: str = ""):
    uid = get_user(authorization)
    conn = get_db()
    row = dict(conn.execute("SELECT id, name, email FROM users WHERE id=?", (uid,)).fetchone())
    conn.close()
    return row

@app.get("/api/users")
def list_users():
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT id, name, email FROM users").fetchall()]
    conn.close()
    return rows

# Projects
@app.get("/api/projects")
def list_projects(authorization: str = ""):
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
def create_project(p: ProjectCreate, authorization: str = ""):
    uid = get_user(authorization)
    conn = get_db()
    conn.execute("INSERT INTO projects (name, description, owner_id) VALUES (?,?,?)", (p.name, p.description, uid))
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = dict(conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())
    conn.close()
    return row

@app.get("/api/projects/{pid}")
def get_project(pid: int, authorization: str = ""):
    uid = get_user(authorization)
    conn = get_db()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not row: raise HTTPException(404, "Not found")
    return dict(row)

@app.post("/api/projects/{pid}/members")
def add_member(pid: int, user_id: int, authorization: str = ""):
    uid = get_user(authorization)
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO project_members (project_id, user_id) VALUES (?,?)", (pid, user_id))
    conn.commit()
    conn.close()
    return {"ok": True}

# Tasks
@app.get("/api/projects/{pid}/tasks")
def list_tasks(pid: int, authorization: str = ""):
    get_user(authorization)
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY id DESC", (pid,)).fetchall()]
    conn.close()
    return rows

@app.post("/api/projects/{pid}/tasks")
def create_task(pid: int, t: TaskCreate, authorization: str = ""):
    uid = get_user(authorization)
    conn = get_db()
    conn.execute("INSERT INTO tasks (project_id, title, description, priority, assignee_id) VALUES (?,?,?,?,?)",
                 (pid, t.title, t.description, t.priority, t.assignee_id))
    conn.commit()
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    conn.close()
    return row

@app.patch("/api/tasks/{tid}")
def update_task(tid: int, t: TaskUpdate, authorization: str = ""):
    get_user(authorization)
    conn = get_db()
    fields = {k: v for k, v in t.model_dump().items() if v is not None}
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [tid]
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        conn.commit()
    row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    conn.close()
    return row

# Comments
@app.get("/api/tasks/{tid}/comments")
def list_comments(tid: int, authorization: str = ""):
    get_user(authorization)
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT c.*, u.name as user_name FROM comments c JOIN users u ON c.user_id=u.id WHERE c.task_id=? ORDER BY c.id", (tid,)).fetchall()]
    conn.close()
    return rows

@app.post("/api/tasks/{tid}/comments")
def create_comment(tid: int, c: CommentCreate, authorization: str = ""):
    uid = get_user(authorization)
    conn = get_db()
    conn.execute("INSERT INTO comments (task_id, user_id, content) VALUES (?,?,?)", (tid, uid, c.content))
    conn.commit()
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = dict(conn.execute(
        "SELECT c.*, u.name as user_name FROM comments c JOIN users u ON c.user_id=u.id WHERE c.id=?", (cid,)).fetchone())
    conn.close()
    return row
