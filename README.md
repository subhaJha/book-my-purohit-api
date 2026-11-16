# Book My Purohit API

FastAPI and SQLite backend for the Book My Purohit React app.

## Run on Windows

From this repository in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:BOOKING_ADMIN_PASSWORD = Read-Host "Set a unique staff password (at least 16 ASCII characters)" -MaskInput
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open `http://127.0.0.1:8000/docs` for the OpenAPI console. Start the React project separately. Its `/api` proxy targets port 8000 by default.

The default database is `.data/bookings.sqlite` in this repository. To reuse the previous Node backend's data, stop the old server, back up its SQLite database, and set `BOOKING_DB` to its absolute path before starting FastAPI. The table structure and booking links are compatible. Staff endpoints are disabled until a password is configured.

## Endpoints

| Method and path | Purpose |
| --- | --- |
| GET /api/health | Database readiness |
| POST /api/bookings | Submit a request with an idempotency key |
| GET /api/bookings/{id} | Retrieve public booking status through the private tracking code |
| POST /api/bookings/{id}/accept | Accept the current revision of the quote |
| GET /api/admin | List, filter and paginate bookings as staff |
| PATCH /api/admin/{id} | Post a quote, mark a booking complete, or cancel it as staff |

For a public deployment, run behind HTTPS with a persistent private database volume and SQLite-aware backups. Keep access logging disabled or redact booking IDs, enforce request-size and rate limits at the reverse proxy, and restrict access to the FastAPI upstream. In-process rate limits use the client address that Uvicorn sees. Configure trusted proxy addresses for a production reverse proxy.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Customer accounts

Customers register and sign in with email and password. Requests created while signed in appear in the customer's booking history. Existing guest bookings remain accessible using their private tracking links. Email verification and password recovery are not implemented.

For an HTTPS deployment, set BOOKING_SECURE_COOKIES=1 so customer session cookies require HTTPS even when the upstream proxy connects over HTTP.

## Purohit portal and staff directory

Staff sign in at /admin in the React app. Use Add new purohit to create a purohit account and give the purohit their credentials privately. Use Purohit list or User list to find and edit existing accounts. Disabling an account revokes its sessions. Staff can set a replacement password for a purohit.

When preparing a quote, choose a roster entry to assign the booking to that account. Purohits sign in at /purohit-portal to see their assigned pujas and statuses. External purohits entered by name have no portal assignment.
