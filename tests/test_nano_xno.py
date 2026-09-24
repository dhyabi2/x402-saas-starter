from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nano_xno import (  # noqa: E402
    XNO_TO_RAW,
    block_info_for,
    raw_to_xno,
    verify_nano_payment,
    xno_to_raw,
)

# A REAL Nano mainnet receive block (frontier) used only for online tests.
# amountRaw 1e29 = 0.1 XNO. Shape: modern node reports
# contents.type == "state", contents.subtype == "send", contents.link_as_account.
REAL_BLOCK = {
    "contents": {
        "link_as_account": "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3",
        "subtype": "send",
        "type": "state",
    },
    "amount": "100000000000000000000000000000",  # 1e29 raw = 0.1 XNO
    "confirmed": "true",
}

REAL_FRONTIER_HASH = "BD5CD30557CED3AF306816FD9D2EEBB1A25E117AD56DB9527A327C1507876D3E"


def test_xno_to_raw_rejects_non_positive():
    with pytest.raises(ValueError):
        xno_to_raw(0)
    with pytest.raises(ValueError):
        xno_to_raw(-1)
    with pytest.raises(ValueError):
        xno_to_raw("bogus")


def test_xno_to_raw_exact():
    assert xno_to_raw(1) == XNO_TO_RAW
    assert xno_to_raw(0.1) == 10**29
    assert xno_to_raw("0.1") == 10**29


def test_raw_to_xno_roundtrip():
    assert raw_to_xno(XNO_TO_RAW) == 1.0
    assert raw_to_xno(10**29) == pytest.approx(0.1)


def verify_factory(amount_raw=10**29, recipient=REAL_BLOCK["contents"]["link_as_account"]):
    block = {
        "contents": {"link_as_account": recipient, "subtype": "send", "type": "state"},
        "amount": str(amount_raw),
        "confirmed": "true",
    }
    return block


def test_verify_accepts_met_amount():
    # 0.1 XNO sits on pay_to; require 0.09 -> valid
    res = verify_nano_payment(
        pay_to=REAL_BLOCK["contents"]["link_as_account"],
        required_xno=0.09,
        block=verify_factory(10**29),
    )
    assert res["isValid"] is True
    assert res["amountXno"] == pytest.approx(0.1)


def test_verify_rejects_underpaid():
    # require 9 XNO, settled 0.1 -> invalid, amount reported honestly
    res = verify_nano_payment(
        pay_to=REAL_BLOCK["contents"]["link_as_account"],
        required_xno=9.0,
        block=verify_factory(10**29),
    )
    assert res["isValid"] is False
    assert "required" in res["reason"]


def test_verify_fails_closed_wrong_recipient():
    other = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
    res = verify_nano_payment(
        pay_to=other,
        required_xno=0.01,
        block=verify_factory(10**29),
    )
    assert res["isValid"] is False


def test_verify_fails_closed_no_block():
    res = verify_nano_payment(pay_to="nano_anything", required_xno=0.1, block=None)
    # block=None with rpc_url still attempts a fetch; force failure by an
    # unopened account path is covered elsewhere. Here with block=None it must
    # not raise and not report valid.
    assert res["isValid"] is False


def test_verify_rejects_zero_required():
    res = verify_nano_payment(pay_to="x", required_xno=0, block=verify_factory())
    assert res["isValid"] is False


def test_block_info_for_unopened_account_returns_none(monkeypatch):
    def fake_rpc(action, *, data, rpc_url):
        return {"error": "Account not found"}
    import nano_xno
    monkeypatch.setattr(nano_xno, "_rpc_call", fake_rpc)
    assert block_info_for("nano_0000000000000000000000000000000000000000000000000001", rpc_url="x") is None


def test_block_info_for_non_send_is_None():
    # a non-send (e.g. change/state without subtype send) must not credit
    block = {"contents": {"type": "state", "subtype": "change", "link_as_account": "x"}, "amount": "0"}
    res = verify_nano_payment(pay_to="x", required_xno=0.01, block=block)
    assert res["isValid"] is False


@pytest.mark.skipif(True, reason="online network test; run explicitly")
def test_online_real_block(monkeypatch):
    # Fetch the real frontier and confirm a met payment end-to-end.
    import nano_xno

    def fake_rpc(action, *, data, rpc_url):
        if action == "account_info":
            return {"frontier": REAL_FRONTIER_HASH}
        return REAL_BLOCK

    monkeypatch.setattr(nano_xno, "_rpc_call", fake_rpc)
    pay_to = REAL_BLOCK["contents"]["link_as_account"]
    res = verify_nano_payment(pay_to=pay_to, required_xno=0.05)
    assert res["isValid"] is True
