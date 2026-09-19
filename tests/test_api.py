import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api import app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = patch.multiple(
            app.config,
            DB_PATH=Path(self.tempdir.name) / "callback.sqlite",
            API_PASSWORD="",
            DEMO_MODE=True,
            MAX_AUDIO_BYTES=3,
            NVIDIA_API_KEY="",
            ELEVENLABS_API_KEY="",
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        self.addCleanup(self.tempdir.cleanup)
        # Deliberately do not use TestClient as a context manager: poller startup
        # is not part of these request tests.
        self.client = TestClient(app.app)
        self.addCleanup(self.client.close)

    def test_live_mode_without_password_is_unavailable(self):
        with patch.object(app.config, "DEMO_MODE", False), patch.object(app.config, "API_PASSWORD", ""):
            self.assertEqual(self.client.get("/api/status").status_code, 503)

    def test_live_mode_accepts_basic_password(self):
        with patch.object(app.config, "DEMO_MODE", False), patch.object(app.config, "API_PASSWORD", "secret"):
            self.assertEqual(self.client.get("/api/status").status_code, 401)
            self.assertEqual(self.client.get("/api/status", auth=("clerk", "secret")).status_code, 200)

    def test_live_mode_rejects_demo_routes(self):
        with patch.object(app.config, "DEMO_MODE", False), patch.object(app.config, "API_PASSWORD", "secret"):
            response = self.client.post("/api/reset", auth=("clerk", "secret"))
        self.assertEqual(response.status_code, 403)

    def test_unsigned_twilio_callback_is_rejected(self):
        with patch.object(app.telephony, "valid_signature", return_value=False):
            response = self.client.post("/api/twilio/recording?hold_id=hold-1", data={"CallSid": "call-1"})
        self.assertEqual(response.status_code, 403)

    def test_reply_rejects_empty_and_oversize_audio(self):
        empty = self.client.post(
            "/api/holds/hold-1/reply",
            files={"audio": ("reply.webm", b"", "audio/webm")},
        )
        oversized = self.client.post(
            "/api/holds/hold-1/reply",
            files={"audio": ("reply.webm", b"1234", "audio/webm")},
        )
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(oversized.status_code, 413)

    def test_settled_hold_reply_is_a_conflict(self):
        with patch.object(app, "verify", side_effect=app.VerificationConflict("hold is settled")):
            response = self.client.post("/api/holds/hold-1/reply", data={"text": "We confirm it."})
        self.assertEqual(response.status_code, 409)

    def test_latest_verification_uses_timestamp_not_random_id(self):
        with app.conn() as c:
            c.execute(
                "INSERT INTO message (id, received_at, sender, body, extracted, status) VALUES (?,?,?,?,?,?)",
                ("message-1", "2026-01-01T00:00:00+00:00", "a@example.com", "body", "{}", "held"),
            )
            c.execute(
                "INSERT INTO hold (id, message_id, score, rationale, signals, status, created_at) VALUES (?,?,?,?,?,?,?)",
                ("hold-1", "message-1", 0.9, "risk", json.dumps([]), "held", "2026-01-01T00:00:00+00:00"),
            )
            for verification_id, created_at, judgment in (
                ("zzz-old", "2026-01-01T00:00:00+00:00", "denied"),
                ("aaa-new", "2026-01-02T00:00:00+00:00", "confirmed"),
            ):
                c.execute(
                    "INSERT INTO verification (id, hold_id, dialed_number, judgment, created_at) VALUES (?,?,?,?,?)",
                    (verification_id, "hold-1", "+15555550100", judgment, created_at),
                )
            c.commit()
        hold = self.client.get("/api/holds").json()[0]
        self.assertEqual(hold["verification"]["id"], "aaa-new")
        board = self.client.get("/api/board").json()
        self.assertEqual(board["open"]["needs_review"][0]["judgment"], "confirmed")

    def test_demo_mode_cannot_place_real_calls(self):
        with patch.object(app.telephony, "place_call") as call:
            response = self.client.post("/api/holds/hold-1/dial")
        self.assertEqual(response.status_code, 403)
        call.assert_not_called()

    def test_recording_can_arrive_before_dial_response(self):
        with app.conn(ensure_seeded=True) as c:
            vendor = app.db.vendors(c)[0]
            decision = app.process(c, {"id": "fast", "sender": vendor["known_senders"][0],
                "body": "Please update bank account to 999999991234."}, use_llm=False)

        def fast_call(**kwargs):
            token = parse_qs(urlparse(kwargs["status_callback"]).query)["call_token"][0]
            self.assertIn(token, kwargs["twiml"])
            response = app._recording_received({"RecordingUrl": "https://example.invalid/audio",
                "RecordingSid": "RE-fast", "CallSid": "CA-fast"}, decision.hold_id, token)
            self.assertEqual(response.status_code, 200)
            # A duplicate and late status notification cannot alter the result.
            app._recording_received({"RecordingUrl": "https://example.invalid/audio",
                "RecordingSid": "RE-fast", "CallSid": "CA-fast"}, decision.hold_id, token)
            app._call_status_received({"CallSid": "CA-fast", "CallStatus": "completed"}, decision.hold_id, token)
            return SimpleNamespace(sid="CA-fast", to=vendor["phone_on_file"], status="queued")

        with patch.multiple(app.config, DEMO_MODE=False, API_PASSWORD="secret"), \
             patch.object(app.telephony, "configured", return_value=True), \
             patch.object(app.telephony, "place_call", side_effect=fast_call), \
             patch.object(app.telephony, "fetch_recording", return_value=b"audio") as fetch, \
             patch.object(app.voice, "transcribe", return_value=("We never sent that request.", None)):
            response = self.client.post(f"/api/holds/{decision.hold_id}/dial", auth=("clerk", "secret"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_count, 1)
        with app.conn() as c:
            hold = c.execute("SELECT * FROM hold").fetchone()
            self.assertEqual(hold["status"], "blocked")
            self.assertEqual(hold["call_sid"], "CA-fast")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM verification").fetchone()[0], 1)

    def test_read_endpoints_and_demo_reset(self):
        self.assertEqual(self.client.post("/api/reset").status_code, 200)
        for endpoint in ("/api/board", "/api/holds", "/api/messages", "/api/vendors", "/api/audit", "/api/status", "/api/mailbox", "/api/activity"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.get(endpoint).status_code, 200)

    def test_conn_context_closes_database_connection(self):
        with app.conn() as connection:
            connection.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
