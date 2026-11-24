import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import asc  # noqa: E402
import testflight_notes  # noqa: E402

FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nMIGT\n-----END PRIVATE KEY-----"


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        return self.responses.pop(0)

    def all(self, path, params=None):
        return self.request("GET", path, params)["data"]


class NotesTests(unittest.TestCase):
    def test_header_format_is_stable(self):
        # BibleMarker's store submission matches on "<branch> @ " — keep it.
        with mock.patch.object(testflight_notes, "git", side_effect=["abc1234", "- one\n- two"]):
            notes = testflight_notes.compose_notes("app-v1.2.0", "42", 10)
        self.assertEqual(notes, "app-v1.2.0 @ abc1234  (build 42)\n\nRecent commits:\n- one\n- two")

    def test_no_sha_refuses_to_write(self):
        with mock.patch.object(testflight_notes, "git", return_value=""):
            with self.assertRaises(RuntimeError):
                testflight_notes.compose_notes("main", "1", 10)

    def test_truncate(self):
        out = testflight_notes.truncate("x" * 5000)
        self.assertLessEqual(len(out), testflight_notes.NOTES_MAX)
        self.assertTrue(out.endswith("\n..."))

    def test_build_number_or_newest_required(self):
        with self.assertRaises(SystemExit):
            testflight_notes.parse_args(["--bundle-id", "a"])
        with self.assertRaises(SystemExit):
            testflight_notes.parse_args(["--bundle-id", "a", "--newest", "--build-number", "3"])
        self.assertTrue(testflight_notes.parse_args(["--bundle-id", "a", "--newest"]).newest)

    def test_find_build_filters_by_marketing_version_only_when_given(self):
        client = FakeClient([{"data": [{"id": "b1"}]}, {"data": []}])
        self.assertEqual(testflight_notes.find_build(client, "app", "7", "1.2")["id"], "b1")
        self.assertIsNone(testflight_notes.find_build(client, "app", "7"))
        self.assertEqual(client.calls[0][2]["filter[preReleaseVersion.version]"], "1.2")
        self.assertNotIn("filter[preReleaseVersion.version]", client.calls[1][2])


class AscTests(unittest.TestCase):
    def test_resolve_app_id_requires_exact_bundle_id(self):
        client = FakeClient([{"data": [
            {"id": "1", "attributes": {"bundleId": "com.example.app.widget"}},
            {"id": "2", "attributes": {"bundleId": "com.example.app"}},
        ]}])
        self.assertEqual(asc.resolve_app_id(client, "com.example.app"), "2")

    def test_build_number_sorts_numerically(self):
        builds = [{"attributes": {"version": v}} for v in ("9", "10", "x")]
        self.assertEqual(max(asc.build_number(b) for b in builds), 10)


class KeyTests(unittest.TestCase):
    def test_pem_bytes_accepts_raw_and_base64(self):
        self.assertEqual(asc.pem_bytes(FAKE_PEM), FAKE_PEM.encode())
        wrapped = base64.encodebytes(FAKE_PEM.encode()).decode()  # 76-column lines
        self.assertEqual(asc.pem_bytes(wrapped), FAKE_PEM.encode())

    def test_pem_bytes_rejects_garbage(self):
        for value in ("bm90IGEga2V5", "not base64 at all \u00e9"):
            with self.assertRaises(SystemExit):
                asc.pem_bytes(value)

    def test_token_verifies_with_the_public_key(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        key = ec.generate_private_key(ec.SECP256R1())
        header, claims, sig = asc.mint_token("KID", "ISS", key).split(".")
        raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        key.public_key().verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


class WriteKeyTests(unittest.TestCase):
    def run_script(self, value):
        """Returns (result, key file contents or None, key file mode or None)."""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [str(ROOT / "scripts" / "write-key.sh"), "KEYID", tmp],
                env={**os.environ, "ASC_PRIVATE_KEY": value},
                capture_output=True,
                text=True,
            )
            key = Path(tmp, "AuthKey_KEYID.p8")
            if not key.exists():
                return result, None, None
            return result, key.read_text(), key.stat().st_mode & 0o777

    def test_raw_key(self):
        result, content, mode = self.run_script(FAKE_PEM)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(content.strip(), FAKE_PEM)
        self.assertEqual(mode, 0o600)

    def test_base64_key(self):
        result, content, _ = self.run_script(base64.b64encode(FAKE_PEM.encode()).decode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(content.strip(), FAKE_PEM)

    def test_garbage_is_rejected_and_removed(self):
        for value in ("bm90IGEga2V5", "!!not base64!!"):
            result, content, _ = self.run_script(value)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("neither a .p8", result.stderr)
            self.assertIsNone(content)


if __name__ == "__main__":
    unittest.main()
