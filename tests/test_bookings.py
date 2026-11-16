from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.main import SCHEMA, create_app
from app.models import today


class BookingTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.database = Path(directory) / "bookings.sqlite"
        self.password = secrets.token_hex(20)
        self.auth = {"Authorization": f"Bearer {self.password}"}
        self.client = self.enterContext(TestClient(create_app(self.database, self.password)))
        self.payload = {
            "key": secrets.token_hex(16),
            "lang": "bn",
            "name": "Test family",
            "phone": "+919876543210",
            "ritual": "griha",
            "date": (today() + timedelta(days=2)).isoformat(),
            "time": "morning",
            "area": "englishBazar",
            "locality": "Test locality",
        }
        self.quote = {"purohit": "Test purohit", "time": "09:30", "dakshina": 1000, "travel": 200, "materials": 50}

    def test_booking_lifecycle(self):
        response = self.client.post("/api/bookings", json=self.payload)
        self.assertEqual(response.status_code, 200)
        booking_id = response.json()["id"]
        self.assertEqual(self.client.post("/api/bookings", json=self.payload).json()["id"], booking_id)
        booking = f"/api/bookings/{booking_id}"
        admin = f"/api/admin/{booking_id}"
        visible = self.client.get(booking).json()
        self.assertEqual(visible["status"], "requested")
        self.assertNotIn("phone", visible)

        self.assertEqual(self.client.get("/api/admin").status_code, 401)
        listing = self.client.get("/api/admin?status=", headers=self.auth)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["bookings"][0]["phone"], "9876543210")
        for revision in range(2):
            result = self.client.patch(admin, headers=self.auth,
                                       json={"status": "quoted", "revision": revision, "quote": self.quote})
            self.assertEqual(result.status_code, 200)

        self.assertEqual(self.client.post(f"{booking}/accept", json={"revision": 1}).status_code, 409)
        confirmed = self.client.post(f"{booking}/accept", json={"revision": 2})
        self.assertEqual(confirmed.json()["status"], "confirmed")
        self.assertEqual(self.client.post(f"{booking}/accept", json={"revision": 2}).json(), confirmed.json())
        completed = self.client.patch(admin, headers=self.auth, json={"status": "completed", "revision": 3})
        self.assertEqual(completed.json()["status"], "completed")
        with TestClient(create_app(self.database, self.password)) as restarted:
            self.assertEqual(restarted.get(booking).json()["status"], "completed")

    def test_rejects_invalid_requests_without_echoing_input(self):
        invalid_phone = "this is invalid"
        rejected = self.client.post("/api/bookings", json=dict(self.payload, phone=invalid_phone))
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn(invalid_phone, rejected.text)
        past = self.client.post("/api/bookings", json=dict(self.payload, date="2020-01-01"))
        self.assertEqual(past.status_code, 400)

    def test_reads_legacy_database_rows(self):
        booking_id = secrets.token_hex(16)
        details = dict(self.payload, phone="9876543210")
        key = details.pop("key")
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute(SCHEMA)
            database.execute(
                "INSERT INTO bookings (id, request_key, details) VALUES (?, ?, ?)",
                (booking_id, key, json.dumps(details)),
            )
        self.assertEqual(self.client.get(f"/api/bookings/{booking_id}").json()["status"], "requested")
        listed = self.client.get("/api/admin", headers=self.auth).json()
        self.assertEqual(listed["bookings"][0]["id"], booking_id)
