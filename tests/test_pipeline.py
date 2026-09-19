import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src import config, db, pipeline
from src.extract.extractor import Extraction
from src.judge.judge import Judgment


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.sqlite'
        self.c = db.connect(self.path)
        self.addCleanup(self.c.close)
        db.init(self.c)
        db.seed(self.c)
        self.vendor = db.vendors(self.c)[0]
        self.addCleanup(patch.stopall)
        patch.object(config, 'NVIDIA_API_KEY', '').start()
        patch.object(config, 'ELEVENLABS_API_KEY', '').start()
        patch.object(config, 'DEMO_MODE', True).start()
        self.message = {
            'id': 'test', 'sender': self.vendor['known_senders'][0],
            'subject': 'Payment details', 'body': 'Please update our bank account to 999999991234.'}

    def process(self, **kwargs):
        return pipeline.process(self.c, self.message, today=date(2026, 9, 19), **kwargs)

    def test_model_cannot_discard_rules_change(self):
        with patch.object(pipeline.extractor, 'extract', return_value=Extraction()):
            decision = self.process()
        self.assertTrue(decision.held)
        self.assertTrue(decision.extraction.requests_payment_change)

    def test_account_change_survives_non_payment_classification(self):
        self.message['body'] = 'Hello there'
        with patch.object(pipeline.extractor, 'extract', return_value=Extraction(bank_account='999999991234')):
            self.assertTrue(self.process().held)

    def test_routing_only_change_is_held_and_updated(self):
        self.message['body'] = f"Please remit to account {self.vendor['account']}, routing 999999999."
        decision = self.process(use_llm=False)
        self.assertTrue(decision.held)
        result = pipeline.verify(self.c, decision.hold_id, scripted_reply='Yes we sent the request. That is correct.')
        self.assertEqual(result['hold_status'], 'approved')
        v = self.c.execute('SELECT * FROM vendor WHERE id=?', (self.vendor['id'],)).fetchone()
        self.assertEqual(v['account'], self.vendor['account'])
        self.assertEqual(v['routing'], '999999999')

    def test_conflicting_extraction_cannot_approve(self):
        with patch.object(pipeline.extractor, 'extract', return_value=Extraction(bank_account='888888881234')):
            decision = self.process()
        result = pipeline.verify(self.c, decision.hold_id, scripted_reply='Yes we sent the request.')
        self.assertEqual(result['hold_status'], 'escalated')

    def test_settled_hold_cannot_be_reverified(self):
        decision = self.process(use_llm=False)
        pipeline.verify(self.c, decision.hold_id, scripted_reply='We never sent that.')
        with self.assertRaises(pipeline.VerificationConflict):
            pipeline.verify(self.c, decision.hold_id, scripted_reply='Yes we sent that.')
        self.assertEqual(self.c.execute('SELECT status FROM hold').fetchone()[0], 'blocked')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM verification').fetchone()[0], 1)

    def test_second_connection_cannot_claim_inflight_hold(self):
        decision = self.process(use_llm=False)
        other = db.connect(self.path)
        self.addCleanup(other.close)
        def during_judge(_):
            with self.assertRaises(pipeline.VerificationConflict):
                pipeline.verify(other, decision.hold_id, scripted_reply='Yes we sent that.')
            return Judgment('denied', 'never sent', 'Denied', 'rules')
        with patch.object(pipeline.judging, 'judge', side_effect=during_judge):
            pipeline.verify(self.c, decision.hold_id, scripted_reply='We never sent that.')

    def test_approval_and_bank_update_roll_back_on_failure(self):
        decision = self.process(use_llm=False)
        self.c.execute("CREATE TRIGGER fail_bank BEFORE UPDATE OF account ON vendor BEGIN SELECT RAISE(ABORT, 'failure'); END")
        self.c.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            pipeline.verify(self.c, decision.hold_id, scripted_reply='Yes we sent that.')
        self.assertEqual(self.c.execute('SELECT status FROM hold').fetchone()[0], 'escalated')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM verification').fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit WHERE action='callback_confirmed'").fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT account FROM vendor WHERE id=?', (self.vendor['id'],)).fetchone()[0], self.vendor['account'])

    def test_ingestion_failure_does_not_mark_message_seen(self):
        self.c.execute("CREATE TRIGGER fail_hold BEFORE INSERT ON hold BEGIN SELECT RAISE(ABORT, 'failure'); END")
        self.c.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.process(use_llm=False)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM message').fetchone()[0], 0)

    def test_recording_receipt_deduplicates_escalated_verification(self):
        decision = self.process(use_llm=False)
        self.c.execute("UPDATE hold SET call_sid='CA-test'")
        self.c.commit()
        with patch.object(pipeline.voice, 'transcribe', return_value=('I am not sure.', None)):
            pipeline.verify(self.c, decision.hold_id, reply_audio=b'audio', recording_sid='RE-test', call_sid='CA-test')
            with self.assertRaises(pipeline.VerificationConflict):
                pipeline.verify(self.c, decision.hold_id, reply_audio=b'audio', recording_sid='RE-test', call_sid='CA-test')
        self.assertEqual(self.c.execute('SELECT status FROM hold').fetchone()[0], 'escalated')

    def test_live_mode_rejects_simulated_reply(self):
        decision = self.process(use_llm=False)
        with patch.object(config, 'DEMO_MODE', False):
            with self.assertRaisesRegex(ValueError, 'signed phone-call'):
                pipeline.verify(self.c, decision.hold_id, scripted_reply='Yes we sent that.')


if __name__ == '__main__':
    unittest.main()
