from contextlib import asynccontextmanager
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import secrets
from threading import Lock
from time import monotonic
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .accounts import ACCOUNT_SCHEMA, account_routes, customer
from .database import Database
from .directory import DIRECTORY_SCHEMA, active_purohit, admin_directory, assignment, purohit_routes, set_assignment
from .models import (
    STATUS, AcceptQuote, AdminUpdateResult, BookingId, BookingPage, Code, NewBooking,
    PublicBooking, UpdateBooking, today,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
  id TEXT PRIMARY KEY,
  request_key TEXT NOT NULL UNIQUE,
  details TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'requested',
  revision INTEGER NOT NULL DEFAULT 0,
  quote TEXT NOT NULL DEFAULT 'null',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
TRANSITIONS = {
    "requested": {"quoted", "cancelled"},
    "quoted": {"quoted", "cancelled"},
    "confirmed": {"completed", "cancelled"},
}


def summary(row):
    details = json.loads(row["details"])
    return {
        "date": details["date"],
        "ritual": details["ritual"],
        "customRitual": details.get("customRitual"),
        "status": row["status"],
        "revision": row["revision"],
        "quote": json.loads(row["quote"]),
    }


def booking_row(db, booking_id):
    row = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Booking not found")
    return row


def save_booking_status(db, row, status, quote):
    db.execute(
        "UPDATE bookings SET status = ?, quote = ?, revision = revision + 1 WHERE id = ?",
        (status, quote, row["id"]),
    )
    return summary(booking_row(db, row["id"]))


def create_app(database: Path | None = None, password: str | None = None, *, hosted: bool = False) -> FastAPI:
    database_url = (os.getenv("BOOKING_DATABASE_URL") or os.getenv("DATABASE_URL")) if database is None else None
    on_vercel = hosted or os.getenv("VERCEL") == "1"
    store = Database(database or Path(os.getenv("BOOKING_DB") or ".data/bookings.sqlite"), database_url)
    password = os.getenv("BOOKING_ADMIN_PASSWORD", "") if password is None else password
    expected = sha256(password.encode()).digest() if len(password) >= 16 else None
    # Short passwords are opt-in for a local developer launch only.
    local_password = os.getenv("BOOKING_LOCAL_ADMIN_PASSWORD", "") if password == "" and not on_vercel else ""
    local_expected = sha256(local_password.encode()).digest() if local_password else None
    bearer = HTTPBearer(auto_error=False)
    rates = {}
    rates_lock = Lock()
    minute = -1

    @asynccontextmanager
    async def lifespan(_app):
        if on_vercel and not database_url and database is None:
            raise RuntimeError("Set a persistent PostgreSQL DATABASE_URL for the Vercel backend.")
        try:
            store.initialize(SCHEMA, ACCOUNT_SCHEMA, DIRECTORY_SCHEMA)
            yield
        finally:
            store.close()

    app = FastAPI(title="Book My Purohit API", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def private_responses(request, call_next):
        if request.method in {"POST", "PATCH"}:
            if request.url.path.startswith(("/api/account/", "/api/purohit/")) and request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON required"}, status_code=415)
            length = request.headers.get("content-length")
            if length is not None and (not length.isascii() or not length.isdecimal()):
                return JSONResponse({"detail": "Invalid request length"}, status_code=400)
            if length is None or len(length) > 5 or int(length) > 8192:
                return JSONResponse({"detail": "Request body too large or missing length"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # Validation messages exclude the original input and context.
        errors = [{key: item[key] for key in ("loc", "msg", "type")} for item in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    def limit(request: Request, group: str, maximum: int):
        nonlocal minute
        address = request.client.host if request.client else "unknown"
        with rates_lock:
            current = int(monotonic() // 60)
            if current != minute:
                rates.clear()
                minute = current
            key = (group, address)
            count = rates.get(key, 0) + 1
            rates[key] = count
        if count > maximum:
            raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})

    def require_staff(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        active_password = expected
        if active_password is None and request.client and request.client.host in {"127.0.0.1", "::1"}:
            active_password = local_expected
        if active_password is None:
            raise HTTPException(503, "Staff access has not been configured")
        supplied = credentials.credentials if credentials else ""
        if not hmac.compare_digest(sha256(supplied.encode()).digest(), active_password):
            limit(request, "staff", 12)
            raise HTTPException(401, "Invalid staff credentials", headers={"WWW-Authenticate": "Bearer"})

    app.include_router(account_routes(store, limit, summary))
    app.include_router(admin_directory(store, require_staff))
    app.include_router(purohit_routes(store, limit, summary))

    @app.get("/api/health")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1 FROM bookings LIMIT 1").fetchone()
        return {"status": "ok"}

    @app.post("/api/bookings", response_model=BookingId)
    def create_booking(details: NewBooking, request: Request):
        limit(request, "create", 24)
        with store.write() as db:
            previous = db.execute("SELECT id FROM bookings WHERE request_key = ?", (details.key,)).fetchone()
            if previous:
                return {"id": previous["id"]}
            if details.date < today():
                raise HTTPException(400, "Puja date has passed")
            booking_id = secrets.token_hex(16)
            db.execute(
                "INSERT INTO bookings (id, request_key, details) VALUES (?, ?, ?)",
                (booking_id, details.key, details.model_dump_json(exclude={"key"})),
            )
            owner = customer(db, request)
            if owner:
                db.execute("INSERT INTO booking_owners (booking_id, customer_id) VALUES (?, ?)", (booking_id, owner["id"]))
            return {"id": booking_id}

    @app.get("/api/bookings/{booking_id}", response_model=PublicBooking)
    def get_booking(booking_id: Code, request: Request):
        limit(request, "lookup", 180)
        with store.connect() as db:
            return summary(booking_row(db, booking_id))

    @app.post("/api/bookings/{booking_id}/accept", response_model=PublicBooking)
    def accept_quote(booking_id: Code, data: AcceptQuote, request: Request):
        limit(request, "accept", 40)
        with store.write() as db:
            row = booking_row(db, booking_id)
            if row["status"] == "confirmed" and data.revision == row["revision"] - 1:
                return summary(row)
            if (row["status"] != "quoted" or row["revision"] != data.revision
                    or json.loads(row["details"])["date"] < today().isoformat()):
                raise HTTPException(409, "Quote or booking status changed")
            purohit_id = assignment(db, booking_id)
            if purohit_id and not active_purohit(db, purohit_id):
                raise HTTPException(409, "Assigned purohit is no longer available")
            return save_booking_status(db, row, "confirmed", row["quote"])

    @app.get("/api/admin", response_model=BookingPage, dependencies=[Depends(require_staff)])
    def list_bookings(
        before: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)] = 9_007_199_254_740_991,
        status: STATUS | Literal[""] | None = None,
    ):
        with store.connect() as db:
            rows = db.execute(
                """SELECT b.rowid AS cursor, b.*, a.purohit_id FROM bookings b
                   LEFT JOIN booking_assignments a ON a.booking_id = b.id
                   WHERE b.rowid < ? AND (CAST(? AS TEXT) IS NULL OR b.status = ?)
                   ORDER BY b.rowid DESC LIMIT 51""",
                (before, status or None, status),
            ).fetchall()
        return {
            "bookings": [
                dict(json.loads(row["details"]), **summary(row), id=row["id"], createdAt=row["created_at"], purohitId=row["purohit_id"])
                for row in rows[:50]
            ],
            "next": rows[49]["cursor"] if len(rows) > 50 else None,
        }

    @app.patch("/api/admin/{booking_id}", response_model=AdminUpdateResult, dependencies=[Depends(require_staff)])
    def update_booking(booking_id: Code, data: UpdateBooking):
        with store.write() as db:
            row = booking_row(db, booking_id)
            if row["revision"] != data.revision or data.status not in TRANSITIONS.get(row["status"], set()):
                raise HTTPException(409, "Booking has changed")
            quote = row["quote"]
            purohit_id = assignment(db, booking_id)
            if data.status == "quoted":
                purohit_id = data.purohitId if "purohitId" in data.model_fields_set else purohit_id
                if purohit_id:
                    purohit = active_purohit(db, purohit_id)
                    if purohit is None:
                        raise HTTPException(422, "Select an active purohit")
                    data.quote.purohit = purohit["name"]
                set_assignment(db, booking_id, purohit_id)
                quote = data.quote.model_dump_json()
            return dict(save_booking_status(db, row, data.status, quote), purohitId=purohit_id)

    return app


app = create_app()
