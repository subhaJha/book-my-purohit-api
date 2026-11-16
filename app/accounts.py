from contextlib import closing
from hashlib import pbkdf2_hmac, sha256
from hmac import compare_digest
import os
import secrets
from time import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, StringConstraints, field_validator

COOKIE = "purohit_session"
PUROHIT_COOKIE = "purohit_staff_session"
LIFETIME = 7 * 24 * 60 * 60
ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_sessions (
    token_hash BLOB PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS customer_sessions_expiration ON customer_sessions(expires_at);
CREATE TABLE IF NOT EXISTS booking_owners (
    booking_id TEXT PRIMARY KEY REFERENCES bookings(id),
    customer_id TEXT NOT NULL REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS booking_owners_customer ON booking_owners(customer_id);
"""


class EmailAddress(BaseModel):
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class Credentials(EmailAddress):
    password: str = Field(min_length=10, max_length=128)


class Identity(EmailAddress):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class Register(Credentials, Identity):
    pass


def hash_password(password, salt):
    return pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def cookie_name(role):
    return PUROHIT_COOKIE if role == "purohit" else COOKIE


def session_digest(request, role="customer"):
    token = request.cookies.get(cookie_name(role), "")
    if len(token) != 43 or not token.isascii():
        return None
    return sha256(token.encode()).digest()


def customer(db, request, role="customer"):
    return db.execute(
        """SELECT c.id, c.email, c.name
           FROM customer_sessions s JOIN customers c ON s.customer_id = c.id
           LEFT JOIN account_directory d ON d.customer_id = c.id
           WHERE s.token_hash = ? AND s.expires_at > ?
           AND COALESCE(d.role, 'customer') = ? AND COALESCE(d.active, 1) = 1""",
        (session_digest(request, role), int(time()), role),
    ).fetchone()


def cookie_is_secure(request):
    return request.url.scheme == "https" or os.getenv("BOOKING_SECURE_COOKIES") == "1" or os.getenv("VERCEL") == "1"


def start_session(db, request, response, user_id, role="customer"):
    now = int(time())
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM customer_sessions WHERE expires_at <= ? OR token_hash = ?", (now, session_digest(request, role)))
    db.execute(
        "INSERT INTO customer_sessions (token_hash, customer_id, expires_at) VALUES (?, ?, ?)",
        (sha256(token.encode()).digest(), user_id, now + LIFETIME),
    )
    response.set_cookie(cookie_name(role), token, max_age=LIFETIME, httponly=True, secure=cookie_is_secure(request), samesite="lax")


def authenticate(store, limit, data, request, response, role):
    limit(request, f"{role}_login", 15)
    with closing(store.connect()) as db:
        user = db.execute(
            """SELECT c.*, COALESCE(d.role, 'customer') AS role, COALESCE(d.active, 1) AS active
               FROM customers c LEFT JOIN account_directory d ON d.customer_id = c.id WHERE c.email = ?""",
            (data.email,),
        ).fetchone()
    candidate = hash_password(data.password, user["salt"] if user else bytes(16))
    matched = compare_digest(candidate, user["password_hash"] if user else bytes(32))
    if not matched or not user or user["role"] != role or not user["active"]:
        raise HTTPException(401, "Invalid email or password")
    with store.write() as db:
        allowed = db.execute(
            """SELECT 1 FROM customers c LEFT JOIN account_directory d ON d.customer_id = c.id
               WHERE c.id = ? AND c.email = ? AND c.password_hash = ? AND COALESCE(d.active, 1) = 1""",
            (user["id"], data.email, candidate),
        ).fetchone()
        if not allowed:
            raise HTTPException(401, "Invalid email or password")
        start_session(db, request, response, user["id"], role)
    return {"name": user["name"], "email": user["email"]}


def end_session(store, request, response, role="customer"):
    with store.write() as db:
        db.execute("DELETE FROM customer_sessions WHERE token_hash = ?", (session_digest(request, role),))
    response.delete_cookie(cookie_name(role), httponly=True, secure=cookie_is_secure(request), samesite="lax")
    return {"status": "signed_out"}


def account_routes(store, limit, booking_summary):
    router = APIRouter(prefix="/api/account", tags=["Customer accounts"])

    @router.post("/register", status_code=201)
    def register(data: Register, request: Request, response: Response):
        limit(request, "registration", 8)
        if customer_exists(store, data.email):
            raise HTTPException(409, "Account could not be created")
        salt = secrets.token_bytes(16)
        password_hash = hash_password(data.password, salt)
        user_id = secrets.token_hex(16)
        with store.write() as db:
            result = db.execute(
                """INSERT INTO customers (id, email, name, salt, password_hash) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(email) DO NOTHING""",
                (user_id, data.email, data.name, salt, password_hash),
            )
            if not result.rowcount:
                raise HTTPException(409, "Account could not be created")
            start_session(db, request, response, user_id)
        return {"name": data.name, "email": data.email}

    @router.post("/login")
    def login(data: Credentials, request: Request, response: Response):
        return authenticate(store, limit, data, request, response, "customer")

    @router.post("/logout")
    def logout(request: Request, response: Response):
        return end_session(store, request, response)

    @router.get("")
    def account(request: Request):
        with closing(store.connect()) as db:
            user = customer(db, request)
            if user is None:
                raise HTTPException(401, "Sign in required")
            return {"name": user["name"], "email": user["email"]}

    @router.get("/bookings")
    def history(request: Request, before: int = 9_007_199_254_740_991):
        if not 0 < before <= 9_007_199_254_740_991:
            raise HTTPException(400, "Invalid page cursor")
        with closing(store.connect()) as db:
            user = customer(db, request)
            if user is None:
                raise HTTPException(401, "Sign in required")
            rows = db.execute(
                """SELECT b.rowid AS cursor, b.*
                   FROM booking_owners o JOIN bookings b ON b.id = o.booking_id
                   WHERE o.customer_id = ? AND b.rowid < ? ORDER BY b.rowid DESC LIMIT 31""",
                (user["id"], before),
            ).fetchall()
        return {
            "bookings": [{"id": row["id"], **booking_summary(row)} for row in rows[:30]],
            "next": rows[29]["cursor"] if len(rows) > 30 else None,
        }

    return router


def customer_exists(store, email):
    with closing(store.connect()) as db:
        return db.execute("SELECT 1 FROM customers WHERE email = ?", (email,)).fetchone() is not None
