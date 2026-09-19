import json
import sys
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from evals import run_eval
from src import llm


class EvalTests(unittest.TestCase):
    def test_score_case_uses_production_pipeline_decision(self):
        case = {
            "id": "case-1", "tag": "routine", "label": "legit", "sender": "a@example.com",
            "reply_to": "a@example.com", "subject": "Invoice", "body": "body", "claimed_vendor": "A",
        }
        decision = SimpleNamespace(assessment=SimpleNamespace(score=0.7, source="nemotron"), held=True)
        with patch.object(run_eval.pipeline, "process", return_value=decision) as process:
            row = run_eval.score_case(object(), case, use_llm=False)
        self.assertTrue(row["held"])
        self.assertTrue(row["predicted_fraud"])
        self.assertEqual(row["source"], "nemotron")
        self.assertEqual(process.call_args.kwargs, {"use_llm": False, "today": run_eval.EVAL_DATE})

    def test_json_results_converts_confusion_tuple_keys(self):
        result = run_eval._json_results({"judge": {"rows": [], "confusion": {("denied", "confirmed"): 1}}})
        self.assertEqual(result["judge"]["confusion"], {"denied->confirmed": 1})
        json.dumps(result)

    def test_forced_model_does_not_fall_back_to_job_routes(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        with patch.object(llm.config, "have_nemotron", return_value=True), patch.object(llm._CLIENT, "post", return_value=response) as post, patch.object(llm.telemetry, "record"):
            with llm.model_override("eval-only"):
                self.assertEqual(llm.complete_json("system", "user", job="score"), {"ok": True})
        self.assertEqual(post.call_args.kwargs["json"]["model"], "eval-only")

    def test_cli_model_override_keeps_full_model_id_for_both_evals(self):
        captured = []
        original_override = llm.model_override

        @contextmanager
        def capture_override(model):
            captured.append(model)
            with original_override(model):
                yield

        fraud = {
            "rows": [], "tp": 0, "fn": 0, "fp": 0, "tn": 0, "precision": 0,
            "recall": 0, "escaped": [], "change_friction": 0, "routine_friction": [],
            "by_tag": {}, "llm": {"calls": 0, "succeeded": 0, "failed": 0, "fallback_rate": 0, "reasons": {}},
        }
        judged = {
            "rows": [], "accuracy": 1, "catastrophic": [], "confusion": {},
            "llm": {"calls": 0, "succeeded": 0, "failed": 0, "fallback_rate": 0, "reasons": {}},
        }
        with patch.object(run_eval.config, "have_nemotron", return_value=True), \
             patch.object(run_eval, "fraud_eval", return_value=fraud), \
             patch.object(run_eval, "judge_eval", return_value=judged), \
             patch.object(run_eval.llm, "model_override", side_effect=capture_override), \
             patch.object(sys, "argv", ["run_eval.py", "--model", "nvidia/test-model"]):
            with redirect_stdout(StringIO()):
                run_eval.main()
        self.assertEqual(captured, [None, "nvidia/test-model", None, "nvidia/test-model"])

    def test_check_fails_when_fraud_escapes_a_hold(self):
        fraud = {
            "rows": [], "tp": 0, "fn": 0, "fp": 0, "tn": 0, "precision": 0,
            "recall": 0, "escaped": [{}], "change_friction": 0, "routine_friction": [],
            "by_tag": {}, "llm": {"calls": 0, "succeeded": 0, "failed": 0, "fallback_rate": 0, "reasons": {}},
        }
        judged = {
            "rows": [], "accuracy": 1, "catastrophic": [], "confusion": {},
            "llm": {"calls": 0, "succeeded": 0, "failed": 0, "fallback_rate": 0, "reasons": {}},
        }
        with patch.object(run_eval.config, "have_nemotron", return_value=False), \
             patch.object(run_eval, "fraud_eval", return_value=fraud), \
             patch.object(run_eval, "judge_eval", return_value=judged), \
             patch.object(sys, "argv", ["run_eval.py", "--check"]), \
             redirect_stdout(StringIO()):
            with self.assertRaisesRegex(SystemExit, "1 fraud escapes"):
                run_eval.main()


if __name__ == "__main__":
    unittest.main()
