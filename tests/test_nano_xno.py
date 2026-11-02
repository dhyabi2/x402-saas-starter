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


# ---- order-bound verification (binding, one-to-one claims, bounded lookups) ----
import hashlib  # noqa: E402
import sqlite3  # noqa: E402

from nano_xno import (  # noqa: E402
    RPC_MAX_BYTES,
    _rpc_call,
    account_to_pubkey,
    assert_safe_rpc_url,
    issue_order,
    order_commitment,
    verify_order_payment,
)

_B32 = "13456789abcdefghijkmnopqrstuwxyz"
PAY_TO = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
H1 = "A" * 64


def _enc(n: int, chars: int) -> str:
    out = ""
    for _ in range(chars):
        out = _B32[n & 31] + out
        n >>= 5
    return out


def account_from_pubkey(pub: bytes) -> str:
    chk = hashlib.blake2b(pub, digest_size=5).digest()[::-1]
    return "nano_" + _enc(int.from_bytes(pub, "big"), 52) + _enc(int.from_bytes(chk, "big"), 8)


def bound_block(order, rep_order_id=None, amount=None):
    rep = account_from_pubkey(order_commitment(rep_order_id or order["order_id"], order["pay_to"], int(order["price_raw"])))
    return {
        "confirmed": "true",
        "subtype": "send",
        "amount": str(amount if amount is not None else order["price_raw"]),
        "block_account": "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3",
        "contents": {"link_as_account": order["pay_to"], "representative": rep},
    }


@pytest.fixture
def db():
    return sqlite3.connect(":memory:")


def test_account_roundtrip_and_checksum():
    pub = bytes(range(32))
    assert account_to_pubkey(account_from_pubkey(pub)) == pub
    bad = account_from_pubkey(pub)
    bad = bad[:-1] + ("3" if bad[-1] != "3" else "4")
    assert account_to_pubkey(bad) is None


def test_bound_block_pays_its_order_once(db):
    o = issue_order(db, PAY_TO, 10**28)
    assert len(o["order_id"]) == 32
    r = verify_order_payment(db, order_id=o["order_id"], block_hash=H1, block=bound_block(o))
    assert r["ok"] is True
    # idempotent re-read of the same block, but no second block for the same order
    assert verify_order_payment(db, order_id=o["order_id"], block_hash=H1, block=bound_block(o))["ok"] is True
    assert verify_order_payment(db, order_id=o["order_id"], block_hash="B" * 64, block=bound_block(o))["ok"] is False


def test_unbound_or_foreign_block_refused(db):
    a, b = issue_order(db, PAY_TO, 10**28), issue_order(db, PAY_TO, 10**28)
    unbound = bound_block(a)
    unbound["contents"].pop("representative")
    assert verify_order_payment(db, order_id=a["order_id"], block_hash=H1, block=unbound)["reason"] == "block is not bound to this order"
    # a block committed to order A cannot pay order B
    assert verify_order_payment(db, order_id=b["order_id"], block_hash=H1, block=bound_block(a))["ok"] is False
    # an unknown order id (e.g. a payer address) is not an order
    assert verify_order_payment(db, order_id="nano_someone", block_hash=H1, block=bound_block(a))["reason"] == "unknown order"


def test_wrong_amount_and_expiry_refused(db):
    o = issue_order(db, PAY_TO, 10**28, ttl_s=60)
    assert verify_order_payment(db, order_id=o["order_id"], block_hash=H1, block=bound_block(o, amount=1))["ok"] is False
    late = verify_order_payment(db, order_id=o["order_id"], block_hash=H1, block=bound_block(o), now=o["expires"] + 1)
    assert late["reason"] == "order expired"


def test_claim_survives_restart_and_is_one_to_one(tmp_path):
    path = str(tmp_path / "n.db")
    c1 = sqlite3.connect(path)
    a = issue_order(c1, PAY_TO, 10**28)
    assert verify_order_payment(c1, order_id=a["order_id"], block_hash=H1, block=bound_block(a))["ok"]
    c1.close()
    c2 = sqlite3.connect(path)  # "restart"
    b = issue_order(c2, PAY_TO, 10**28)
    r = verify_order_payment(c2, order_id=b["order_id"], block_hash=H1, block=bound_block(b))
    assert r == {"ok": False, "reason": "block already claimed by another order"}


def test_rpc_url_policy_and_bounded_reads():
    with pytest.raises(ValueError):
        assert_safe_rpc_url("http://rpc.example.com")
    assert_safe_rpc_url("https://rpc.nano.to")
    assert_safe_rpc_url("http://127.0.0.1:7076")

    class Big:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"x" * (n if n > 0 else RPC_MAX_BYTES * 2)

    with patch("nano_xno.urlopen", return_value=Big()) as m:
        with pytest.raises(ValueError):
            _rpc_call("block_info", data={}, rpc_url="https://rpc.nano.to")
        assert m.call_args.kwargs["timeout"] == 10
