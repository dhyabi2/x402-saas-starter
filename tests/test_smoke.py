from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DOWNLOAD_SECRET", "test-secret-not-changeme")
os.environ.setdefault("RECIPIENT_WALLET", "0xd779cE46567d21b9918F24f0640cA5Ad6058C893")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("CDP_API_KEY", "test-key")
os.environ.setdefault("ASSETS_DIR", str(ROOT / "assets"))

from server import AppConfig, TIER_PRICING, _build_app  # noqa: E402
from signed_url import SignedUrlIssuer  # noqa: E402
from x402_middleware import (  # noqa: E402
    USDC_DECIMALS,
    VerifyResult,
    build_402_response,
    build_requirement,
    decode_payment_header,
    usd_to_micro,
)


@pytest.fixture()
def app_client(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    for tier in TIER_PRICING:
        (assets / f"{tier}.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    cfg = AppConfig(
        recipient_wallet="0xd779cE46567d21b9918F24f0640cA5Ad6058C893",
        download_secret="test-secret-not-changeme",
        facilitator_url="https://api.cdp.coinbase.com",
        cdp_api_key="test-key",
        network="base",
        asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        assets_dir=assets,
        public_base_url="http://testserver",
    )
    app = _build_app(config=cfg)
    return TestClient(app)


def test_env_loading_rejects_default_secret(tmp_path):
    with patch.dict(os.environ, {"DOWNLOAD_SECRET": "changeme"}):
        with pytest.raises(RuntimeError, match="DOWNLOAD_SECRET"):
            AppConfig.from_env()


def test_env_loading_rejects_bad_wallet():
    with patch.dict(
        os.environ,
        {"RECIPIENT_WALLET": "0xshort", "DOWNLOAD_SECRET": "ok-secret"},
    ):
        with pytest.raises(RuntimeError, match="RECIPIENT_WALLET"):
            AppConfig.from_env()


def test_usd_to_micro_conversion():
    assert usd_to_micro(49.0) == "49000000"
    assert usd_to_micro(99.0) == "99000000"
    assert usd_to_micro(199.0) == "199000000"
    assert usd_to_micro(0.05) == "50000"
    with pytest.raises(ValueError):
        usd_to_micro(0)


@pytest.mark.parametrize(
    "tier,micro",
    [("preorder", "20000000"), ("intro", "49000000"), ("std", "99000000"), ("pro", "199000000")],
)
def test_buy_returns_402_with_x402_v05_shape(app_client, tier, micro):
    r = app_client.get(f"/buy/{tier}")
    assert r.status_code == 402
    assert "payment-required" in r.headers
    body = r.json()
    assert body["x402Version"] == 1
    assert body["error"] == "Payment required"
    assert len(body["accepts"]) == 1
    req = body["accepts"][0]
    assert req["scheme"] == "exact"
    assert req["network"] == "base"
    assert req["asset"] == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    assert req["payTo"] == "0xd779cE46567d21b9918F24f0640cA5Ad6058C893"
    assert req["maxAmountRequired"] == micro
    assert req["maxTimeoutSeconds"] == 300
    assert req["extra"]["decimals"] == USDC_DECIMALS
    assert req["extra"]["symbol"] == "USDC"


def test_payment_required_header_decodes_to_same_body(app_client):
    r = app_client.get("/buy/intro")
    raw = base64.b64decode(r.headers["payment-required"] + "==").decode()
    decoded = json.loads(raw)
    assert decoded == r.json()


def test_buy_unknown_tier_404(app_client):
    r = app_client.get("/buy/enterprise")
    assert r.status_code == 422 or r.status_code == 404


def test_download_without_payment_header_returns_402(app_client):
    r = app_client.post("/download/intro")
    assert r.status_code == 402


def test_download_invalid_payment_header_returns_402(app_client):
    r = app_client.post("/download/intro", headers={"X-PAYMENT": "!!!not-base64!!!"})
    assert r.status_code == 402


def test_download_happy_path_returns_signed_url(app_client):
    payment_json = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": {
            "signature": "0x" + "ab" * 32,
            "authorization": {
                "from": "0x" + "11" * 20,
                "to": "0xd779cE46567d21b9918F24f0640cA5Ad6058C893",
                "value": "49000000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "cd" * 32,
            },
        },
    }
    header = base64.b64encode(json.dumps(payment_json).encode()).decode()

    mock_verify = AsyncMock(
        return_value=VerifyResult(isValid=True, payer="0x" + "11" * 20)
    )
    with patch("x402_middleware.X402Facilitator.verify", mock_verify):
        r = app_client.post("/download/intro", headers={"X-PAYMENT": header})

    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "intro"
    assert body["download_url"].startswith("http://testserver/download/file/intro/")
    assert body["expires_at"] > int(time.time())

    token = body["download_url"].rsplit("/", 1)[-1]
    f1 = app_client.get(f"/download/file/intro/{token}")
    assert f1.status_code == 200
    assert f1.headers["content-type"] == "application/zip"

    f2 = app_client.get(f"/download/file/intro/{token}")
    assert f2.status_code == 403


def test_download_facilitator_rejects_payment(app_client):
    header = base64.b64encode(b'{"x402Version":1}').decode()
    mock_verify = AsyncMock(
        return_value=VerifyResult(isValid=False, invalidReason="insufficient funds")
    )
    with patch("x402_middleware.X402Facilitator.verify", mock_verify):
        r = app_client.post("/download/intro", headers={"X-PAYMENT": header})
    assert r.status_code == 402
    assert "insufficient funds" in r.text


def test_signed_url_hmac_valid_and_invalid():
    issuer = SignedUrlIssuer("test-secret-not-changeme")
    token = issuer.issue("intro")
    encoded = issuer.encode(token)

    assert issuer.verify("intro", encoded, consume=False) is True
    assert issuer.verify("std", encoded, consume=False) is False
    assert issuer.verify("intro", encoded + "tamper", consume=False) is False
    assert issuer.verify("intro", "not-base64-at-all", consume=False) is False
    assert issuer.verify("intro", encoded, consume=True) is True
    assert issuer.verify("intro", encoded, consume=True) is False


def test_signed_url_tamper_signature_rejected():
    issuer = SignedUrlIssuer("test-secret-not-changeme")
    token = issuer.issue("intro")
    bad_sig = "X" * len(token.signature)
    raw = f"{token.nonce}.{token.expires_at}.{bad_sig}".encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert issuer.verify("intro", encoded) is False


def test_signed_url_rejects_default_secret():
    with pytest.raises(ValueError):
        SignedUrlIssuer("changeme")


def test_decode_payment_header_rejects_non_object():
    with pytest.raises(Exception):
        decode_payment_header(base64.b64encode(b'"not-an-object"').decode())


def test_build_requirement_shape():
    req = build_requirement(
        usd_price=49.0,
        resource_url="https://x.example/buy/intro",
        description="d",
        pay_to="0x" + "11" * 20,
    )
    assert req.maxAmountRequired == "49000000"
    assert req.scheme == "exact"
    assert req.network == "base"


def test_build_402_response_includes_header():
    req = build_requirement(
        usd_price=49.0,
        resource_url="https://x.example/buy/intro",
        description="d",
        pay_to="0x" + "11" * 20,
    )
    resp = build_402_response(req)
    assert resp.status_code == 402
    assert "payment-required" in resp.headers


def test_landing_page_renders(app_client):
    r = app_client.get("/")
    assert r.status_code == 200
    assert "x402 SaaS Starter" in r.text
    rendered = {f"/buy/{t}" for t in TIER_PRICING if t != "preorder"}
    for tier_path in rendered:
        assert tier_path in r.text
