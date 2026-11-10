import os, re, json, sqlite3, secrets
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, g
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "fib.db")
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

CREATOR_LOGIN = "X1nce"
CREATOR_PASSWORD = "cb&VLVJ6$RuSyOHdsNynUuZ4]-+vV:[D"
OLD_CREATOR_PASSWORD = "FIB_X1nce!Creator_2026"

ROLES = {
    "creator": "Создатель",
    "leader": "Директор",
    "deputy": "Заместитель директора",
    "head": "Глава отдела",
    "dephead": "Заместитель главы отдела",
}
STATUSES = {
    "new": "Новая",
    "in_progress": "На обработке",
    "accepted": "Принято",
    "rejected": "Отклонено",
}

def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS staff_accounts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      login TEXT NOT NULL UNIQUE COLLATE NOCASE,
      name TEXT NOT NULL,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL,
      dept TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS applications (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      number TEXT NOT NULL UNIQUE,
      kind TEXT NOT NULL,
      fields_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'new',
      created_at TEXT NOT NULL,
      reviewed_by TEXT NOT NULL DEFAULT '',
      updated_at TEXT NOT NULL
    );
    """)
    db.commit()
    # Миграции для уже существующей базы данных.
    columns = {row[1] for row in db.execute("PRAGMA table_info(applications)").fetchall()}
    if "rejection_reason" not in columns:
        db.execute("ALTER TABLE applications ADD COLUMN rejection_reason TEXT NOT NULL DEFAULT ''")
    staff_columns = {row[1] for row in db.execute("PRAGMA table_info(staff_accounts)").fetchall()}
    if "last_seen" not in staff_columns:
        db.execute("ALTER TABLE staff_accounts ADD COLUMN last_seen TEXT")
    db.commit()
    creator = db.execute("SELECT id, role, password_hash FROM staff_accounts WHERE login=? COLLATE NOCASE", (CREATOR_LOGIN,)).fetchone()
    if not creator:
        db.execute(
            "INSERT INTO staff_accounts(login,name,password_hash,role,dept,created_at) VALUES(?,?,?,?,?,?)",
            (CREATOR_LOGIN, "X1nce", generate_password_hash(CREATOR_PASSWORD), "creator", "", datetime.now().isoformat(timespec="seconds"))
        )
        db.commit()
    elif creator["role"] != "creator":
        db.execute(
            "UPDATE staff_accounts SET name=?, password_hash=?, role='creator', dept='' WHERE id=?",
            ("X1nce", generate_password_hash(CREATOR_PASSWORD), creator["id"])
        )
        db.commit()
    elif check_password_hash(creator["password_hash"], OLD_CREATOR_PASSWORD):
        # Однократная смена ранее выданного стандартного пароля Создателя.
        db.execute("UPDATE staff_accounts SET password_hash=? WHERE id=?",
                   (generate_password_hash(CREATOR_PASSWORD), creator["id"]))
        db.commit()

@app.before_request
def ensure_db():
    init_db()

def json_error(message, code=400):
    return jsonify({"error": message}), code

def staff_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        staff_id = session.get("staff_id")
        if not staff_id:
            return json_error("Необходимо войти в аккаунт старшего состава.", 401)
        row = get_db().execute("SELECT * FROM staff_accounts WHERE id=?", (staff_id,)).fetchone()
        if not row:
            session.clear()
            return json_error("Аккаунт не найден. Войдите повторно.", 401)
        g.staff = row
        return fn(*args, **kwargs)
    return wrapped

def account_json(row):
    return {"id": row["id"], "login": row["login"], "name": row["name"],
            "role": row["role"], "role_label": ROLES.get(row["role"], row["role"]),
            "dept": row["dept"]}

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/applications")
def create_application():
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    fields = data.get("fields")
    if kind not in ("Трудоустройство", "Восстановление") or not isinstance(fields, list):
        return json_error("Некорректные данные заявления.")
    cleaned = []
    for item in fields:
        if not isinstance(item, list) or len(item) != 2:
            return json_error("Некорректный формат полей заявления.")
        label, value = str(item[0])[:80], str(item[1])[:4000]
        cleaned.append([label, value])
    labels = {x[0] for x in cleaned}
    required = {"Имя Фамилия", "Номер паспорта", "Discord Username", "Фото"}
    if not required.issubset(labels):
        return json_error("Заполните все обязательные поля.")
    passport_value = next((x[1].strip() for x in cleaned if x[0] == "Номер паспорта"), "")
    if not re.fullmatch(r"[0-9]{1,9}", passport_value) or not (1 <= int(passport_value) <= 100000000):
        return json_error("Номер паспорта должен быть числом от 1 до 100 000 000.")
    if kind == "Трудоустройство" and "Военный билет" not in labels:
        return json_error("Укажите наличие военного билета.")
    if kind == "Восстановление" and "Примерная дата увольнения" not in labels:
        return json_error("Укажите дату увольнения.")
    if not all(x[1].strip() for x in cleaned):
        return json_error("Поля заявления не должны быть пустыми.")
    db = get_db()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    for _ in range(5):
        number = "FIB-" + secrets.token_hex(3).upper()
        try:
            cur = db.execute(
                "INSERT INTO applications(number,kind,fields_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (number, kind, json.dumps(cleaned, ensure_ascii=False), "new", now, now)
            )
            db.commit()
            return jsonify({"ok": True, "id": cur.lastrowid, "number": number}), 201
        except sqlite3.IntegrityError:
            db.rollback()
    return json_error("Не удалось создать номер заявления. Повторите попытку.", 500)

@app.get("/api/staff/state")
def staff_state():
    row = None
    if session.get("staff_id"):
        row = get_db().execute("SELECT * FROM staff_accounts WHERE id=?", (session["staff_id"],)).fetchone()
    if row:
        return jsonify({"initialized": True, "authenticated": True, "account": account_json(row)})
    session.pop("staff_id", None)
    count = get_db().execute("SELECT COUNT(*) FROM staff_accounts").fetchone()[0]
    return jsonify({"initialized": count > 0, "authenticated": False})

@app.post("/api/staff/login")
def staff_login():
    data = request.get_json(silent=True) or {}
    login, password = str(data.get("login","")).strip(), str(data.get("password",""))
    row = get_db().execute("SELECT * FROM staff_accounts WHERE login=? COLLATE NOCASE", (login,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return json_error("Неверный логин или пароль.", 401)
    session.clear()
    session["staff_id"] = row["id"]
    get_db().execute("UPDATE staff_accounts SET last_seen=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), row["id"]))
    get_db().commit()
    return jsonify({"ok": True, "account": account_json(row)})

@app.post("/api/staff/logout")
def staff_logout():
    staff_id = session.get("staff_id")
    if staff_id:
        get_db().execute("UPDATE staff_accounts SET last_seen=NULL WHERE id=?", (staff_id,))
        get_db().commit()
    session.clear()
    return jsonify({"ok": True})

@app.post("/api/staff/heartbeat")
@staff_required
def staff_heartbeat():
    now = datetime.now().isoformat(timespec="seconds")
    get_db().execute("UPDATE staff_accounts SET last_seen=? WHERE id=?", (now, g.staff["id"]))
    get_db().commit()
    return jsonify({"ok": True})

@app.get("/api/staff/online")
def staff_online():
    rows = get_db().execute(
        "SELECT id, name, role, dept, last_seen FROM staff_accounts "
        "WHERE last_seen IS NOT NULL ORDER BY role, name"
    ).fetchall()
    now = datetime.now()
    online = []
    for row in rows:
        try:
            seen = datetime.fromisoformat(row["last_seen"])
        except (TypeError, ValueError):
            continue
        if (now - seen).total_seconds() <= 75:
            online.append({"id": row["id"], "name": row["name"],
                           "role_label": ROLES.get(row["role"], row["role"]),
                           "dept": row["dept"], "last_seen": row["last_seen"]})
    return jsonify({"online": online})

@app.get("/api/staff/applications")
@staff_required
def list_applications():
    rows = get_db().execute("SELECT * FROM applications ORDER BY id DESC").fetchall()
    result = []
    for row in rows:
        fields = json.loads(row["fields_json"])
        name = next((x[1] for x in fields if x[0] == "Имя Фамилия"), "—")
        result.append({"id": row["id"], "number": row["number"], "kind": row["kind"],
                       "fields": fields, "name": name, "status": row["status"],
                       "status_label": STATUSES.get(row["status"], row["status"]),
                       "created_at": row["created_at"], "reviewed_by": row["reviewed_by"],
                       "rejection_reason": row["rejection_reason"] if "rejection_reason" in row.keys() else ""})
    return jsonify({"applications": result})

@app.post("/api/staff/applications/<int:app_id>/status")
@staff_required
def update_application_status(app_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ("in_progress", "accepted", "rejected"):
        return json_error("Недопустимый статус.")
    staff = g.staff
    by = staff["name"] + " (" + ROLES.get(staff["role"], staff["role"]) + ")"
    db = get_db()
    reason = str(data.get("rejection_reason", "")).strip()
    if status == "rejected" and not reason:
        return json_error("Укажите причину отклонения заявления.")
    if len(reason) > 1000:
        return json_error("Причина отклонения не должна превышать 1000 символов.")
    if status != "rejected":
        reason = ""
    cur = db.execute("UPDATE applications SET status=?, reviewed_by=?, rejection_reason=?, updated_at=? WHERE id=?",
                     (status, by, reason, datetime.now().strftime("%d.%m.%Y %H:%M"), app_id))
    db.commit()
    if cur.rowcount == 0:
        return json_error("Заявление не найдено.", 404)
    return jsonify({"ok": True})

@app.get("/api/staff/team")
@staff_required
def list_team():
    if g.staff["role"] not in ("creator", "leader"):
        return json_error("Управление аккаунтами недоступно для вашей должности.", 403)
    rows = get_db().execute("SELECT * FROM staff_accounts ORDER BY id").fetchall()
    if g.staff["role"] == "creator":
        visible = [x for x in rows if x["id"] != g.staff["id"]]
    else:
        visible = [x for x in rows if x["role"] in ("deputy", "head", "dephead")]
    return jsonify({"accounts": [account_json(x) for x in visible]})

@app.post("/api/staff/team")
@staff_required
def create_team_account():
    actor_role = g.staff["role"]
    if actor_role not in ("creator", "leader"):
        return json_error("Создавать аккаунты недоступно для вашей должности.", 403)
    data = request.get_json(silent=True) or {}
    first_name = str(data.get("first_name", "")).strip()
    last_name = str(data.get("last_name", "")).strip()
    name = (first_name + " " + last_name).strip()
    login = str(data.get("login", "")).strip()
    password = str(data.get("password", ""))
    role, dept = data.get("role"), str(data.get("dept", "")).strip()
    if len(first_name) < 2 or len(last_name) < 2:
        return json_error("Укажите имя и фамилию сотрудника.")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{3,24}", login):
        return json_error("Логин: латинские буквы, цифры, _ или -, от 3 до 24 символов.")
    if len(password) < 8:
        return json_error("Пароль должен содержать не менее 8 символов.")
    if actor_role == "creator":
        if role not in ("leader", "deputy", "head", "dephead"):
            return json_error("Создатель может создавать все должности, кроме второго Создателя.", 403)
        if role in ("head", "dephead") and not dept:
            return json_error("Укажите отдел.")
        if role in ("leader", "deputy"):
            dept = ""
    else:
        if role not in ("deputy", "head", "dephead"):
            return json_error("Лидер может создавать только заместителя лидера, главу отдела и заместителя главы отдела.", 403)
        if role in ("head", "dephead") and not dept:
            return json_error("Укажите отдел.")
        if role == "deputy":
            dept = ""
    try:
        get_db().execute("INSERT INTO staff_accounts(login,name,password_hash,role,dept,created_at) VALUES(?,?,?,?,?,?)",
                         (login, name, generate_password_hash(password), role, dept, datetime.now().isoformat(timespec="seconds")))
        get_db().commit()
    except sqlite3.IntegrityError:
        return json_error("Такой логин уже занят.", 409)
    return jsonify({"ok": True}), 201

@app.delete("/api/staff/team/<int:account_id>")
@staff_required
def delete_team_account(account_id):
    actor_role = g.staff["role"]
    if actor_role not in ("creator", "leader"):
        return json_error("Удалять аккаунты недоступно для вашей должности.", 403)
    db = get_db()
    if actor_role == "creator":
        cur = db.execute("DELETE FROM staff_accounts WHERE id=? AND role!='creator'", (account_id,))
    else:
        cur = db.execute("DELETE FROM staff_accounts WHERE id=? AND role IN ('deputy','head','dephead')", (account_id,))
    db.commit()
    if cur.rowcount == 0:
        return json_error("Аккаунт не найден или у вас нет прав на его удаление.", 404)
    return jsonify({"ok": True})

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
