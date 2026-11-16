from contextlib import closing
import json
import secrets
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field, model_validator

from .accounts import Credentials, Identity, authenticate, customer, end_session, hash_password
from .models import AREA, Code

DIRECTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_directory (
    customer_id TEXT PRIMARY KEY REFERENCES customers(id),
    role TEXT NOT NULL CHECK (role IN ('customer', 'purohit')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    phone TEXT,
    area TEXT
);
CREATE TABLE IF NOT EXISTS booking_assignments (
    booking_id TEXT PRIMARY KEY REFERENCES bookings(id),
    purohit_id TEXT NOT NULL REFERENCES account_directory(customer_id)
);
CREATE INDEX IF NOT EXISTS booking_assignments_purohit ON booking_assignments(purohit_id);
"""

SELECT_DIRECTORY = """
SELECT c.rowid AS cursor, c.id, c.name, c.email, COALESCE(d.active, 1) AS active, d.phone, d.area
FROM customers c LEFT JOIN account_directory d ON d.customer_id = c.id
"""
ROLE = "COALESCE(d.role, 'customer')"


class DirectoryUpdate(Identity):
    active: bool = True
    phone: str | None = Field(default=None, pattern=r"^(\+91)?[6-9][0-9]{9}$")
    area: AREA | None = None
    password: str | None = Field(default=None, min_length=10, max_length=128)


class NewPurohit(DirectoryUpdate):
    @model_validator(mode="after")
    def require_profile(self):
        if not self.password or not self.phone or not self.area:
            raise ValueError("A password, phone and area are required")
        return self


def account_result(row):
    result = dict(row)
    result.pop("cursor", None)
    result["active"] = bool(result["active"])
    return result


def active_purohit(db, user_id):
    return db.execute(
        """SELECT c.id, c.name FROM account_directory d JOIN customers c ON c.id = d.customer_id
           WHERE d.role = 'purohit' AND d.active = 1 AND c.id = ?""",
        (user_id,),
    ).fetchone()


def assignment(db, booking_id):
    row = db.execute(
        "SELECT purohit_id FROM booking_assignments WHERE booking_id = ?", (booking_id,)
    ).fetchone()
    return row["purohit_id"] if row else None


def set_assignment(db, booking_id, purohit_id):
    if purohit_id:
        db.execute(
            """INSERT INTO booking_assignments (booking_id, purohit_id) VALUES (?, ?)
               ON CONFLICT(booking_id) DO UPDATE SET purohit_id = excluded.purohit_id""",
            (booking_id, purohit_id),
        )
    else:
        db.execute("DELETE FROM booking_assignments WHERE booking_id = ?", (booking_id,))


def admin_directory(store, require_staff):
    router = APIRouter(prefix="/api/admin/directory", dependencies=[Depends(require_staff)], tags=["Staff directory"])

    @router.get("/roster")
    def roster():
        with closing(store.connect()) as db:
            rows = db.execute(
                """SELECT c.id, c.name, d.area FROM customers c
                   JOIN account_directory d ON d.customer_id = c.id
                   WHERE d.role = 'purohit' AND d.active = 1 ORDER BY c.name, c.id"""
            ).fetchall()
        return {"records": [dict(row) for row in rows]}

    @router.get("/{role}")
    def accounts(role: Literal["customer", "purohit"], before: int = 9_007_199_254_740_991,
                 search: str = Query(default="", max_length=100)):
        if not 0 < before <= 9_007_199_254_740_991:
            raise HTTPException(400, "Invalid page cursor")
        needle = search.strip().lower()
        with closing(store.connect()) as db:
            rows = db.execute(
                SELECT_DIRECTORY + f"""WHERE {ROLE} = ? AND c.rowid < ?
                AND (? = '' OR instr(lower(c.name), ?) > 0 OR instr(c.email, ?) > 0)
                ORDER BY c.rowid DESC LIMIT 31""",
                (role, before, needle, needle, needle),
            ).fetchall()
        return {"records": [account_result(row) for row in rows[:30]],
                "next": rows[29]["cursor"] if len(rows) > 30 else None}

    @router.post("/purohit", status_code=201)
    def add_purohit(data: NewPurohit):
        salt = secrets.token_bytes(16)
        password = hash_password(data.password, salt)
        user_id = secrets.token_hex(16)
        try:
            with store.write() as db:
                db.execute(
                    "INSERT INTO customers (id, email, name, salt, password_hash) VALUES (?, ?, ?, ?, ?)",
                    (user_id, data.email, data.name, salt, password),
                )
                save_profile(db, user_id, "purohit", data)
                return account_result(db.execute(SELECT_DIRECTORY + "WHERE c.id = ?", (user_id,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "This email is already in use") from exc

    @router.patch("/{role}/{user_id}")
    def update_account(role: Literal["customer", "purohit"], user_id: Code, data: DirectoryUpdate):
        if role == "purohit" and (not data.phone or not data.area):
            raise HTTPException(422, "Purohit phone and area are required")
        if role == "customer" and data.password:
            raise HTTPException(422, "Use the customer password recovery process")
        salt = secrets.token_bytes(16) if data.password else None
        password = hash_password(data.password, salt) if salt else None
        try:
            with store.write() as db:
                old = db.execute(SELECT_DIRECTORY + f"WHERE c.id = ? AND {ROLE} = ?", (user_id, role)).fetchone()
                if old is None:
                    raise HTTPException(404, "Account not found")
                db.execute("UPDATE customers SET name = ?, email = ? WHERE id = ?", (data.name, data.email, user_id))
                save_profile(db, user_id, role, data)
                if password:
                    db.execute("UPDATE customers SET salt = ?, password_hash = ? WHERE id = ?", (salt, password, user_id))
                if password or not data.active or old["email"] != data.email:
                    db.execute("DELETE FROM customer_sessions WHERE customer_id = ?", (user_id,))
                return account_result(db.execute(SELECT_DIRECTORY + "WHERE c.id = ?", (user_id,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "This email is already in use") from exc

    return router


def save_profile(db, user_id, role, data):
    phone = data.phone.removeprefix("+91") if role == "purohit" and data.phone else None
    area = data.area if role == "purohit" else None
    db.execute(
        """INSERT INTO account_directory (customer_id, role, active, phone, area) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(customer_id) DO UPDATE SET active = excluded.active, phone = excluded.phone, area = excluded.area""",
        (user_id, role, data.active, phone, area),
    )


def purohit_routes(store, limit, summary):
    router = APIRouter(prefix="/api/purohit", tags=["Purohit portal"])

    @router.post("/login")
    def login(data: Credentials, request: Request, response: Response):
        return authenticate(store, limit, data, request, response, "purohit")

    @router.post("/logout")
    def logout(request: Request, response: Response):
        return end_session(store, request, response, "purohit")

    @router.get("/schedule")
    def schedule(request: Request, before: int = 9_007_199_254_740_991):
        if not 0 < before <= 9_007_199_254_740_991:
            raise HTTPException(400, "Invalid page cursor")
        with closing(store.connect()) as db:
            user = customer(db, request, "purohit")
            if user is None:
                raise HTTPException(401, "Sign in to the purohit portal")
            rows = db.execute(
                """SELECT b.rowid AS cursor, b.* FROM booking_assignments a
                   JOIN bookings b ON b.id = a.booking_id
                   WHERE a.purohit_id = ? AND b.rowid < ?
                   ORDER BY b.rowid DESC LIMIT 31""",
                (user["id"], before),
            ).fetchall()
        records = []
        for row in rows[:30]:
            details = json.loads(row["details"])
            booking = summary(row)
            quote = booking["quote"] or {}
            records.append({
                "reference": row["cursor"], "date": booking["date"], "ritual": booking["ritual"],
                "customRitual": booking.get("customRitual"), "status": booking["status"],
                "area": details["area"], "locality": details["locality"] if booking["status"] == "confirmed" else None,
                "time": quote.get("time"),
                "dakshina": quote.get("dakshina"), "samagri": quote.get("samagri"),
            })
        return {"name": user["name"], "bookings": records, "next": rows[29]["cursor"] if len(rows) > 30 else None}

    return router
