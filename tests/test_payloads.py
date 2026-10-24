"""Pure functions that decide what leaves the machine.

No network, no device, no audio: the payload shapes are the part that breaks silently
(a wrong voice field changes the speaker, an undecoded envelope sounds like noise), so
they are checked here.
"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hermes_pe.bridge import (  # noqa: E402
    DEFAULT_ANSWER_CMD,
    DEFAULT_BIAS,
    apply_config_defaults,
    build_parser,
    build_answer_argv,
    normalize_paths,
    resolve_path,
    build_stt_fields,
    build_tts_body,
)


class TtsBody(unittest.TestCase):
    def test_mistral_uses_voice_id_and_no_voice(self):
        body = build_tts_body("Hallo", "voxtral-mini-tts-3-0", "voice_481", "mistral")
        self.assertEqual(body["voice_id"], "voice_481")
        self.assertNotIn("voice", body)
        self.assertEqual(body["input"], "Hallo")
        self.assertEqual(body["response_format"], "wav")

    def test_openai_uses_voice(self):
        body = build_tts_body("Hallo", "piper", "de_DE-thorsten-high", "openai")
        self.assertEqual(body["voice"], "de_DE-thorsten-high")
        self.assertNotIn("voice_id", body)

    def test_extra_overrides_a_field(self):
        body = build_tts_body("Hallo", "voxtral-mini-tts-3-0", "voice_481", "mistral", {"expressiveness": 4})
        self.assertEqual(body["expressiveness"], 4)

    def test_a_saved_voice_uuid_is_passed_through(self):
        uuid = "01a16d08-63da-7067-a298-9e691bcdf78d"
        self.assertEqual(build_tts_body("x", "m", uuid, "mistral")["voice_id"], uuid)


class SttFields(unittest.TestCase):
    def test_mistral_biases_per_term_and_pins_a_two_letter_language(self):
        fields = build_stt_fields("turn.wav", "de", "voxtral-mini-transcribe-3", "mistral", ["JSON", "Pi"])
        self.assertEqual(fields["language"], "de")
        self.assertEqual(fields["__context_bias__"], ["JSON", "Pi"])
        self.assertNotIn("prompt", fields)
        self.assertNotIn("response_format", fields)

    def test_openai_sends_one_prompt_and_the_response_format(self):
        fields = build_stt_fields("turn.wav", "de", "whisper-1", "openai", ["JSON", "Pi"])
        self.assertEqual(fields["prompt"], "JSON, Pi")
        self.assertEqual(fields["response_format"], "json")
        self.assertNotIn("__context_bias__", fields)

    def test_a_long_language_tag_is_dropped_for_mistral(self):
        fields = build_stt_fields("turn.wav", "de-DE", "voxtral-mini-transcribe-3", "mistral", None)
        self.assertNotIn("language", fields)


class Defaults(unittest.TestCase):
    def test_the_answer_command_is_hermes_oneshot(self):
        self.assertEqual(DEFAULT_ANSWER_CMD, "hermes -z {text}")

    def test_the_vocabulary_has_no_duplicates(self):
        self.assertEqual(len(DEFAULT_BIAS), len(set(DEFAULT_BIAS)))

    def test_parser_defaults_point_at_the_hosted_endpoints(self):
        options = build_parser().parse_args([])
        self.assertEqual(options.stt_dialect, "mistral")
        self.assertEqual(options.tts_dialect, "mistral")
        self.assertEqual(options.stt_url, "https://api.mistral.ai")
        self.assertEqual(options.tts_model, "voxtral-mini-tts-3-0")
        self.assertEqual(options.language, "de")


class Paths(unittest.TestCase):
    def test_a_home_relative_path_is_expanded(self):
        self.assertTrue(resolve_path("~/.config/voice-pe/secrets.yaml").startswith("/"))

    def test_an_environment_variable_is_expanded(self):
        import os

        os.environ["PI_TEST_DIR"] = "/tmp/pi-test"
        self.assertEqual(resolve_path("$PI_TEST_DIR/secrets.yaml"), "/tmp/pi-test/secrets.yaml")

    def test_parser_normalizes_the_paths_it_receives(self):
        options = build_parser().parse_args(["--secrets", "~/.config/voice-pe/secrets.yaml"])
        self.assertFalse(normalize_paths(options).secrets.startswith("~"))


class AnswerArgv(unittest.TestCase):
    """The regression that produced a wrong answer: the transcript must stay one argument."""

    def test_a_spoken_sentence_stays_a_single_argument(self):
        argv = build_answer_argv("hermes -z {text}", "Wie lange muss ich Spaghetti kochen?")
        self.assertEqual(argv, ["hermes", "-z", "Wie lange muss ich Spaghetti kochen?"])

    def test_arguments_after_the_placeholder_survive(self):
        argv = build_answer_argv("hermes -z {text} --continue", "und jetzt das andere")
        self.assertEqual(argv, ["hermes", "-z", "und jetzt das andere", "--continue"])

    def test_quotes_in_a_transcript_do_not_start_a_second_command(self):
        argv = build_answer_argv("hermes -z {text}", "sag \"hallo\"; rm -rf /tmp/x")
        self.assertIn("sag \"hallo\"; rm -rf /tmp/x", argv)
        self.assertEqual(len(argv), 3)

    def test_a_template_without_the_placeholder_still_gets_the_text(self):
        self.assertEqual(build_answer_argv("hermes", "hallo"), ["hermes", "hallo"])


class ConfigFile(unittest.TestCase):
    def test_a_config_file_fills_defaults_and_flags_still_win(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"host": "10.0.0.42", "silence_ms": 1200, "stt_bias": ["A", "B"]}, handle)
            path = handle.name

        parser = build_parser()
        apply_config_defaults(parser, ["--config", path, "--host", "voice.local"])
        options = parser.parse_args(["--config", path, "--host", "voice.local"])

        self.assertEqual(options.host, "voice.local")
        self.assertEqual(options.silence_ms, 1200)
        self.assertEqual(json.loads(options.stt_bias), ["A", "B"])

    def test_a_missing_config_file_is_ignored(self):
        parser = build_parser()
        apply_config_defaults(parser, ["--config", "/nope/missing.json"])
        self.assertEqual(parser.parse_args([]).language, "de")


if __name__ == "__main__":
    unittest.main()
