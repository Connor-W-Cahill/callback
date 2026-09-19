import json
import unittest
from pathlib import Path
from unittest.mock import patch

import src.judge.judge as judge_module
from src.judge.judge import judge, judge_rules, vendor_turns


class JudgeRulesTest(unittest.TestCase):
    def test_seeded_transcripts_keep_their_labels(self):
        cases = json.loads(
            (Path(__file__).parents[1] / "evals" / "transcripts" / "cases.json").read_text()
        )
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(judge_rules(case["transcript"]).judgment, case["label"])

    def test_agent_only_confirmation_is_not_vendor_evidence(self):
        transcript = "AGENT: Please do not change anything until I confirm with accounting."
        self.assertEqual(vendor_turns(transcript), "")
        self.assertEqual(judge_rules(transcript).judgment, "unclear")

    def test_live_judge_rejects_agent_only_confirmation_before_calling_model(self):
        with patch.object(judge_module.llm, "complete_json") as complete:
            verdict = judge("AGENT: Confirm the new account.")
        self.assertEqual(verdict.judgment, "unclear")
        complete.assert_not_called()

    def test_negated_or_uncertain_confirmation_is_not_approved(self):
        for reply in (
            "I cannot confirm that account.",
            "Yes we sent it, but I cannot confirm that account.",
            "I cannot say that was us.",
            "We did not say we switched banks.",
            "Please do not change anything until I confirm with accounting.",
        ):
            with self.subTest(reply=reply):
                transcript = f"AGENT: Did you send the change?\nVENDOR: {reply}"
                self.assertNotEqual(judge_rules(transcript).judgment, "confirmed")

    def test_model_cannot_approve_without_confirmation_evidence(self):
        with patch.object(judge_module.llm, "complete_json", return_value={
            "judgment": "confirmed", "quote": "I received the invoice", "reasoning": "ok"
        }):
            self.assertEqual(judge_module.judge("VENDOR: I received the invoice.").judgment, "unclear")

    def test_multiline_vendor_turn_can_confirm(self):
        transcript = (
            "AGENT: Did you send the change?\n"
            "VENDOR: Yes we sent that request.\n"
            "We switched banks last month.\n"
            "AGENT: Thank you."
        )
        self.assertEqual(judge_rules(transcript).judgment, "confirmed")


if __name__ == "__main__":
    unittest.main()
