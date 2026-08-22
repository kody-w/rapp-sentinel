"""Trusted-controller Azure image generation.

Adapted from the local ArtGenerator RAPP supplied by the operator. This module
is deliberately not a model tool: only evolve_worker imports it, and the image
maker never receives the Azure token or endpoint.
"""

import base64
import os
import subprocess
from urllib.parse import quote, urlencode, urlparse

import requests


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_API_VERSION = "2025-04-01-preview"


class AzureArtError(RuntimeError):
    pass


def _access_token(az_binary="az", timeout=60):
    try:
        result = subprocess.run(
            [az_binary, "account", "get-access-token",
             "--scope", TOKEN_SCOPE,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AzureArtError(f"Azure token acquisition failed: {exc}") from exc
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        detail = (result.stderr or result.stdout or "no token").strip()[:300]
        raise AzureArtError(f"Azure token acquisition failed: {detail}")
    return token


def _error_message(response):
    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError:
        return response.text[:500].strip() or response.reason
    error = payload.get("error") if isinstance(payload, dict) else payload
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    return str(error)[:500]


def _request(endpoint, deployment, api_version, token, prompt, size, quality,
             timeout):
    url = (
        f"{endpoint}/openai/deployments/{quote(deployment, safe='')}"
        f"/images/generations?{urlencode({'api-version': api_version})}"
    )
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": "{} {}".format("Bearer", token),
                "Content-Type": "application/json",
            },
            json={
                "prompt": prompt,
                "n": 1,
                "size": size,
                "quality": quality,
                "output_format": "png",
            },
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise AzureArtError(
            f"Azure image request failed on {deployment}: {exc}") from exc
    if not response.ok:
        raise AzureArtError(
            f"Azure image generation failed on {deployment} "
            f"({response.status_code}): {_error_message(response)}")
    try:
        payload = response.json()
        encoded = payload["data"][0]["b64_json"]
        image = base64.b64decode(encoded, validate=True)
    except (KeyError, IndexError, TypeError, ValueError,
            requests.exceptions.JSONDecodeError) as exc:
        raise AzureArtError(
            f"Azure image generation on {deployment} returned no valid PNG") from exc
    if not image.startswith(PNG_SIGNATURE):
        raise AzureArtError(
            f"Azure image generation on {deployment} returned a non-PNG payload")
    return image


def generate(prompt, config, token=None):
    endpoint = str(config.get("endpoint") or "").strip().rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise AzureArtError("azure_image.endpoint must be a valid HTTPS URL")
    deployments = [
        str(config.get("deployment") or "gpt-image-2").strip(),
        str(config.get("fallback_deployment") or "gpt-image").strip(),
    ]
    deployments = list(dict.fromkeys(item for item in deployments if item))
    if not deployments:
        raise AzureArtError("azure_image has no configured deployment")
    token = token or _access_token(
        str(config.get("az_binary") or "az"),
        int(config.get("auth_timeout_s") or 60))
    failures = []
    for deployment in deployments:
        try:
            return (
                _request(
                    endpoint, deployment,
                    str(config.get("api_version") or DEFAULT_API_VERSION),
                    token, prompt,
                    str(config.get("size") or "1536x1024"),
                    str(config.get("quality") or "high"),
                    int(config.get("request_timeout_s") or 240),
                ),
                deployment,
            )
        except AzureArtError as exc:
            failures.append(str(exc))
    raise AzureArtError(" | ".join(failures))
