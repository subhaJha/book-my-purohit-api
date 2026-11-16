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

    def test_customer_session_and_booking_history_are_private(self):
        credentials = {"email": "family@example.test", "password": "sample passphrase"}
        registered = self.client.post("/api/account/register", json=dict(credentials, name="Test Family"))
        self.assertEqual(registered.status_code, 201)
        self.assertIn("HttpOnly", registered.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/account").json()["email"], credentials["email"])

        booking = self.client.post("/api/bookings", json=dict(self.payload, ritual="durga"))
        self.assertEqual(booking.status_code, 200)
        booking_id = booking.json()["id"]
        history = self.client.get("/api/account/bookings").json()
        self.assertEqual([record["id"] for record in history["bookings"]], [booking_id])
        with TestClient(create_app(self.database, self.password)) as other:
            self.assertEqual(other.get("/api/account/bookings").status_code, 401)
            other.post("/api/account/register", json=dict(credentials, email="other@example.test", name="Other"))
            self.assertEqual(other.get("/api/account/bookings").json()["bookings"], [])

        self.assertEqual(self.client.post("/api/account/logout", json={}).status_code, 200)
        self.assertEqual(self.client.get("/api/account").status_code, 401)
        self.assertEqual(self.client.post("/api/account/login", json=dict(credentials, password="wrong password")).status_code, 401)
        self.assertEqual(self.client.post("/api/account/login", json=credentials).status_code, 200)
        self.assertEqual(self.client.get("/api/account/bookings").json()["bookings"][0]["id"], booking_id)

    def test_staff_can_manage_directory_and_purohits_only_see_own_assignments(self):
        directory = "/api/admin/directory"
        profile = {
            "name": "Test Purohit", "email": "priest@example.test", "password": "purohit passphrase",
            "phone": "9876543210", "area": "oldMalda",
        }
        credentials = {key: profile[key] for key in ("email", "password")}
        self.assertEqual(self.client.post(f"{directory}/purohit", json=profile).status_code, 401)
        created = self.client.post(f"{directory}/purohit", json=profile, headers=self.auth)
        self.assertEqual(created.status_code, 201)
        assigned = created.json()
        self.assertNotIn("password", assigned)
        self.assertEqual(self.client.post(f"{directory}/purohit", json=profile, headers=self.auth).status_code, 409)
        self.assertEqual(self.client.get(f"{directory}/purohit", headers=self.auth).json()["records"], [assigned])
        self.assertEqual(self.client.get(f"{directory}/customer", headers=self.auth).json()["records"], [])

        booking_id = self.client.post("/api/bookings", json=self.payload).json()["id"]
        admin_path = f"/api/admin/{booking_id}"
        result = self.client.patch(admin_path, headers=self.auth, json={
            "status": "quoted", "revision": 0, "quote": self.quote, "purohitId": assigned["id"],
        })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["quote"]["purohit"], profile["name"])

        with TestClient(create_app(self.database, self.password)) as priest:
            self.assertEqual(priest.post("/api/account/login", json=credentials).status_code, 401)
            self.assertEqual(priest.post("/api/purohit/login", json=credentials).status_code, 200)
            self.assertEqual(priest.get("/api/account/bookings").status_code, 401)
            assignments = priest.get("/api/purohit/schedule").json()["bookings"]
            self.assertEqual(len(assignments), 1)
            self.assertEqual(assignments[0]["ritual"], self.payload["ritual"])
            self.assertNotIn(booking_id, json.dumps(assignments))
            self.assertIsNone(assignments[0]["locality"])
            self.client.post(f"/api/bookings/{booking_id}/accept", json={"revision": 1})
            confirmed = priest.get("/api/purohit/schedule").json()["bookings"][0]
            self.assertEqual(confirmed["locality"], self.payload["locality"])
            self.assertNotIn("phone", confirmed)
            updated = self.client.patch(f"{directory}/purohit/{assigned['id']}", headers=self.auth,
                                        json=dict(profile, name="Renamed", active=False, password=None))
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(priest.get("/api/purohit/schedule").status_code, 401)

        registration = self.client.post("/api/account/register", json={
            "name": "Customer", "email": "edit@example.test", "password": "customer passphrase",
        })
        self.assertEqual(registration.status_code, 201)
        self.assertEqual(self.client.get("/api/purohit/schedule").status_code, 401)
        record = self.client.get(f"{directory}/customer", headers=self.auth).json()["records"][0]
        changed = self.client.patch(f"{directory}/customer/{record['id']}", headers=self.auth, json={
            "name": "Updated Customer", "email": "updated@example.test", "active": True,
        })
        self.assertEqual(changed.json()["name"], "Updated Customer")
        self.assertEqual(self.client.get("/api/account").status_code, 401)
