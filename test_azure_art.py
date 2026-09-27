#!/usr/bin/env python3
"""Focused tests for trusted-controller Azure image authentication."""

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import azure_art


PNG = azure_art.PNG_SIGNATURE + b"image"


class Response:
    ok = True

    def json(self):
        return {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}


def config(**overrides):
    value = {
        "endpoint": "https://dada.example.openai.azure.com",
        "deployment": "gpt-image-2",
        "fallback_deployment": "gpt-image-2",
    }
    value.update(overrides)
    return value


class AzureAuthenticationTests(unittest.TestCase):
    def test_owner_only_api_key_file_authenticates_without_entra(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "azure.key"
            path.write_text("secret-key\n", encoding="utf-8")
            path.chmod(0o600)
            cfg = config(
                auth_mode="api_key",
                api_key_env_var="TEST_AZURE_IMAGE_KEY_MISSING",
                api_key_file=str(path),
            )
            with mock.patch.object(azure_art, "_access_token") as token, \
                    mock.patch.object(
                        azure_art.requests, "post",
                        return_value=Response()) as post:
                image, deployment = azure_art.generate("brief", cfg)

        self.assertEqual(PNG, image)
        self.assertEqual("gpt-image-2", deployment)
        token.assert_not_called()
        headers = post.call_args.kwargs["headers"]
        self.assertEqual("secret-key", headers["api-key"])
        self.assertNotIn("Authorization", headers)

    def test_group_readable_api_key_file_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "azure.key"
            path.write_text("secret-key", encoding="utf-8")
            path.chmod(0o640)
            with self.assertRaises(azure_art.AzureArtError) as raised:
                azure_art.auth_headers(config(
                    auth_mode="api_key",
                    api_key_env_var="TEST_AZURE_IMAGE_KEY_MISSING",
                    api_key_file=str(path),
                ))
        self.assertIn("group or others", str(raised.exception))

    def test_environment_api_key_takes_precedence_over_file(self):
        cfg = config(
            auth_mode="api_key",
            api_key_env_var="TEST_AZURE_IMAGE_KEY",
            api_key_file="/does/not/exist",
        )
        with mock.patch.dict(
                os.environ, {"TEST_AZURE_IMAGE_KEY": "environment-key"}):
            headers, mode = azure_art.auth_headers(cfg)
        self.assertEqual("api_key", mode)
        self.assertEqual({"api-key": "environment-key"}, headers)

    def test_entra_remains_the_backward_compatible_default(self):
        with mock.patch.object(
                azure_art, "_access_token", return_value="entra-token"), \
                mock.patch.object(
                    azure_art.requests, "post",
                    return_value=Response()) as post:
            image, _ = azure_art.generate("brief", config())
        self.assertEqual(PNG, image)
        headers = post.call_args.kwargs["headers"]
        self.assertEqual("Bearer entra-token", headers["Authorization"])
        self.assertNotIn("api-key", headers)

    def test_unknown_auth_mode_is_rejected(self):
        with self.assertRaises(azure_art.AzureArtError) as raised:
            azure_art.auth_headers(config(auth_mode="magic"))
        self.assertIn("auth_mode", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
