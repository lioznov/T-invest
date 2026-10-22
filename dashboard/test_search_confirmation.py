"""Correctness of candidate lock, not evidence of profitability."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from . import rotation_search
from .management.commands.confirm_rotation import select_candidate


class ConfirmationTests(TestCase):
    def test_no_passing_candidate_cannot_open_reserve(self):
        with self.assertRaises(CommandError):
            select_candidate({"results": [{"passes_screen": False}]})

    def test_selection_uses_stress_then_deterministic_key(self):
        report = {"results": [
            {"rule": {"key": key}, "validation": {"stress_return_pct": stress}, "passes_screen": True}
            for key, stress in (("z", 2), ("b", 3), ("a", 3))]}
        self.assertEqual(select_candidate(report)["rule"]["key"], "a")

    def test_choice_is_written_before_first_reserve_read(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"data").mkdir()
            code_hash = hashlib.sha256(Path(rotation_search.__file__).read_bytes()).hexdigest()
            report = {"protocol": {"version": rotation_search.VERSION, "code_sha256": code_hash},
                      "results": [{"rule": asdict(rotation_search.ROTATIONS[0]), "passes_screen": True,
                                   "validation": {"stress_return_pct": 1}}]}
            (root/"data"/"test.json").write_text(json.dumps(report), encoding="utf-8")
            frozen = root/"data"/"rotation-search-choice-v2.json"

            def reserved_read(*args):
                self.assertTrue(frozen.is_file())
                self.assertEqual(json.loads(frozen.read_text(encoding="utf-8"))["rule"], report["results"][0]["rule"])
                raise CommandError("fixture stopped before network")

            with override_settings(BASE_DIR=root), patch("dashboard.management.commands.confirm_rotation.acquire", side_effect=reserved_read) as fetch:
                with self.assertRaisesRegex(CommandError, "fixture stopped before network"):
                    call_command("confirm_rotation", input="data/test.json")
                fetch.assert_called_once()
