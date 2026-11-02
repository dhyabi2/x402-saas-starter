"""Receive-only Nano (XNO) settlement verification for the x402 SaaS starter.

Ships a second, feeless settlement rail beside the USDC-on-Base x402 path. The
buyer pays on the Nano network (instant, zero fee, no gas, no issuer); the
server verifies the settled block over a public keyless RPC and serves the
download -- exactly as it already verifies a USDC payment through the x402
facilitator. No facilitator, no signer, no private key is ever held on the
server.

A Nano receive block settles at ~1s (soft) finality for free. Both halves of the
flow are covered so an operator can switch rails without touching the paid
download logic:
  - `xno_to_raw` / amount helpers -- exact XNO <-> raw conversion, never 1:1.
  - `verify_nano_payment` -- fail-closed: returns True only when a confirmed
    receive block for `pay_to` exists with amountRaw >= the required amount.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

XNO_TO_RAW: int = 10**30  # 1 XNO = 10^30 raw (30 decimals)
DEFAULT_RPC_URL: str = "https://rpc.nano.to"  # keyless public RPC

# A Nano account can be unopened (no blocks yet) before its first receive.
OPENED_RPC_ERRORS: tuple[str, ...] = ("Account not found", "Account not opened")


def xno_to_raw(xno: float | int | str) -> int:
    """Convert a decimal XNO string/number to raw (int), rejecting <= 0.

    Uses Decimal arithmetic for exactness (XNO has 30 decimals; floats cannot
    represent 10**30 exactly), so an XNO price is never silently rounded.
    """
    from decimal import Decimal, InvalidOperation

    if not isinstance(xno, str):
        xno = str(xno)
    try:
        d = Decimal(xno)
    except InvalidOperation as exc:
        raise ValueError(f"invalid XNO amount '{xno}'") from exc
    if d <= 0:
        raise ValueError(f"amount must be positive, got {xno}")
    return int(d * XNO_TO_RAW)


def raw_to_xno(raw: int | str) -> float:
    """Convert raw (int or numeric string) back to decimal XNO."""
    return int(raw) / XNO_TO_RAW


RPC_TIMEOUT_S = 10
RPC_MAX_BYTES = 64 * 1024


def assert_safe_rpc_url(rpc_url: str) -> None:
    """https, or plain http only to a loopback host; anything else is refused."""
    u = urlparse(rpc_url)
    loopback = u.hostname in ("localhost", "127.0.0.1", "::1")
    if not (u.scheme == "https" or (u.scheme == "http" and loopback)):
        raise ValueError("Nano RPC URL must be https (plain http only for loopback)")


def _rpc_call(action: str, *, data: dict[str, Any], rpc_url: str) -> dict[str, Any]:
    assert_safe_rpc_url(rpc_url)
    body = json.dumps({"action": action, **data}).encode("utf-8")
    req = Request(
        rpc_url,
        data=body,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=RPC_TIMEOUT_S) as resp:
        raw = resp.read(RPC_MAX_BYTES + 1)
    if len(raw) > RPC_MAX_BYTES:
        raise ValueError("Nano RPC response too large")
    payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return payload


def block_info_for(
    account: str,
    *,
    rpc_url: str = DEFAULT_RPC_URL,
) -> dict[str, Any] | None:
    """Return the latest confirmed block info for `account`, or None if empty.

    Uses the `block_info` RPC action with the account's frontier block. A valid,
    opened Nano account normally returns a block; an unopened/empty account
    or a network error returns None so callers can fail closed cleanly.
    """
    # Resolve the frontier so block_info targets (and therefore checks) the
    # account's own latest block, not an arbitrary hash.
    try:
        frontier = _rpc_call(
            "account_info",
            data={"account": account, "representative": "false"},
            rpc_url=rpc_url,
        )
    except Exception:
        frontier = {"error": "unreachable"}
    if "error" in frontier:
        msg = str(frontier.get("error", ""))
        if any(e in msg for e in OPENED_RPC_ERRORS):
            return None
        return None  # unreachable or unknown error -- fail closed

    frontier_hash = frontier.get("frontier")
    if not frontier_hash:
        return None

    try:
        info = _rpc_call(
            "block_info",
            data={"json_block": "true", "hash": frontier_hash},
            rpc_url=rpc_url,
        )
    except Exception:
        info = {"error": "unreachable"}
    if "error" in info:
        return None
    return info


def _settled_amount_xno(block: dict[str, Any] | None) -> tuple[float, str] | None:
    """Extract (amount_xno, link_as_account) from a confirmed Nano block.

    Modern Nano state blocks report `contents.type == "state"` with
    `contents.subtype == "send"` (a payment that left the sender) and the
    recipient as `contents.link_as_account` (already a nano_ string -- compare
    strings, never hand-decode base32). The amount lives at the top level as
    `amount` (raw). Returns None when the block is not a send to anyone we can
    attribute.
    """
    if not block:
        return None
    contents = block.get("contents") or {}
    if contents.get("type") != "state" or contents.get("subtype") != "send":
        return None
    recipient = contents.get("link_as_account")
    amount_raw = block.get("amount")
    if not recipient or not amount_raw:
        return None
    try:
        amount_xno = raw_to_xno(amount_raw)
    except (ValueError, TypeError):
        return None
    return amount_xno, recipient


def verify_nano_payment(
    *,
    pay_to: str,
    required_xno: float,
    block: dict[str, Any] | None = None,
    rpc_url: str = DEFAULT_RPC_URL,
) -> dict[str, Any]:
    """UNBOUND helper: it does not know which order a block pays for, so it must
    not be the access gate. Use `verify_order_payment` for that.

    Fail-closed: True only when a confirmed send settles to `pay_to` for the
    required amount. Returns {'isValid': bool, 'amountXno': float, 'payer': str, 'reason': str}.

    Pass `block` to test against a known block; otherwise it is fetched by
    account_info + block_info over the public RPC.
    """
    if required_xno <= 0:
        return {"isValid": False, "amountXno": 0.0, "payer": "", "reason": "required amount must be positive"}
    if not (block or rpc_url):
        return {"isValid": False, "amountXno": 0.0, "payer": "", "reason": "no block and no rpc_url"}

    info = block if block is not None else block_info_for(pay_to, rpc_url=rpc_url)
    settled = _settled_amount_xno(info)
    if settled is None:
        return {
            "isValid": False,
            "amountXno": 0.0,
            "payer": "",
            "reason": "no confirmed Nano send block found for pay_to",
        }
    amount_xno, recipient = settled
    met = amount_xno >= required_xno and recipient == pay_to
    return {
        "isValid": met,
        "amountXno": amount_xno,
        "payer": recipient,
        "reason": "" if met else f"settled {amount_xno} XNO to {recipient}, required {required_xno}",
    }


# ---------------------------------------------------------------------------
# Order-bound verification (xno-call-binding, option C + seller ledger rules)
#
# A Nano block has no memo, so "a confirmed send of the right amount exists" does
# not say which order it paid. Each order gets a random id and a commitment the
# buyer signs into the send block's `representative`; the block hash is then
# claimed one-to-one with the order in sqlite (UNIQUE), before access is granted.
# ---------------------------------------------------------------------------

_B32 = "13456789abcdefghijkmnopqrstuwxyz"


def order_commitment(order_id: str, pay_to: str, price_raw: int) -> bytes:
    """SHA-256("xno-binding/v1/rep|" + order_id + "|" + pay_to + "|" + price_raw)."""
    if isinstance(price_raw, bool) or not isinstance(price_raw, int):
        raise TypeError("price_raw must be an int of raw")
    msg = b"xno-binding/v1/rep|" + order_id.encode() + b"|" + pay_to.encode() + b"|" + str(price_raw).encode()
    return hashlib.sha256(msg).digest()


def account_to_pubkey(addr: str) -> bytes | None:
    """32-byte public key of a nano_/xrb_ account (checksum verified), or None."""
    if not isinstance(addr, str) or not addr.startswith(("nano_", "xrb_")):
        return None
    body = addr.split("_", 1)[1]
    if len(body) != 60 or any(c not in _B32 for c in body):
        return None
    n = 0
    for ch in body:
        n = n * 32 + _B32.index(ch)
    if n >> 296:  # the 4 leading pad bits must be zero
        return None
    key = (n >> 40).to_bytes(32, "big")
    checksum = (n & ((1 << 40) - 1)).to_bytes(5, "big")
    if hashlib.blake2b(key, digest_size=5).digest()[::-1] != checksum:
        return None
    return key


def init_order_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        "create table if not exists nano_orders(order_id text primary key, pay_to text, "
        "price_raw text, expires integer)"
    )
    conn.execute(
        "create table if not exists nano_claims(block_hash text primary key, order_id text unique, "
        "claimed_at integer)"
    )
    conn.commit()


def issue_order(conn: sqlite3.Connection, pay_to: str, price_raw: int, ttl_s: int = 600) -> dict[str, Any]:
    """Create an order with an opaque 128-bit id. Tell the buyer `representative_pubkey`."""
    init_order_db(conn)
    order_id = secrets.token_hex(16)
    expires = int(time.time()) + ttl_s
    with conn:
        conn.execute("insert into nano_orders values(?,?,?,?)", (order_id, pay_to, str(price_raw), expires))
    return {
        "order_id": order_id,
        "pay_to": pay_to,
        "price_raw": str(price_raw),
        "expires": expires,
        # The buyer signs the send block with this 32-byte value (hex) as its representative.
        "representative_pubkey": order_commitment(order_id, pay_to, price_raw).hex(),
    }


def verify_order_payment(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    block_hash: str,
    rpc_url: str = DEFAULT_RPC_URL,
    block: dict[str, Any] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Fail-closed: ok only for a confirmed send of exactly the order's price to its pay_to whose
    representative is this order's commitment, and whose hash is not claimed by another order.

    `block` may be passed for offline tests; otherwise block_info is fetched (bounded)."""
    init_order_db(conn)

    def fail(reason: str) -> dict[str, Any]:
        return {"ok": False, "reason": reason}

    if not (isinstance(block_hash, str) and len(block_hash) == 64 and all(c in "0123456789abcdefABCDEF" for c in block_hash)):
        return fail("invalid block hash")
    block_hash = block_hash.upper()
    row = conn.execute("select pay_to, price_raw, expires from nano_orders where order_id=?", (str(order_id),)).fetchone()
    if row is None:
        return fail("unknown order")
    pay_to, price_raw, expires = row[0], int(row[1]), int(row[2])
    claim = conn.execute("select block_hash from nano_claims where order_id=?", (order_id,)).fetchone()
    if claim is not None:
        return {"ok": claim[0] == block_hash, "reason": "" if claim[0] == block_hash else "order already paid by another block"}
    if (now if now is not None else int(time.time())) > expires:
        return fail("order expired")
    owner = conn.execute("select order_id from nano_claims where block_hash=?", (block_hash,)).fetchone()
    if owner is not None:
        return fail("block already claimed by another order")

    if block is None:
        try:
            block = _rpc_call("block_info", data={"json_block": "true", "hash": block_hash}, rpc_url=rpc_url)
        except Exception:
            return fail("node lookup failed")
    if not isinstance(block, dict) or "error" in block:
        return fail("block not found")
    contents = block.get("contents") or {}
    if str(block.get("confirmed")).lower() != "true":
        return fail("block not confirmed")
    if block.get("subtype") != "send":
        return fail("block is not a send")
    if contents.get("link_as_account") != pay_to:
        return fail("paid to wrong address")
    if str(block.get("amount")) != str(price_raw):
        return fail("amount does not match the order")
    rep = account_to_pubkey(contents.get("representative", ""))
    if rep is None or rep != order_commitment(order_id, pay_to, price_raw):
        return fail("block is not bound to this order")
    try:
        with conn:  # UNIQUE(block_hash) and UNIQUE(order_id) make the claim atomic
            conn.execute("insert into nano_claims values(?,?,?)", (block_hash, order_id, int(time.time())))
    except sqlite3.IntegrityError:
        return fail("block already claimed by another order")
    return {"ok": True, "reason": "", "payer": block.get("block_account") or contents.get("account", "")}


__all__ = [
    "DEFAULT_RPC_URL",
    "OPENED_RPC_ERRORS",
    "XNO_TO_RAW",
    "account_to_pubkey",
    "assert_safe_rpc_url",
    "block_info_for",
    "init_order_db",
    "issue_order",
    "order_commitment",
    "raw_to_xno",
    "verify_nano_payment",
    "verify_order_payment",
    "xno_to_raw",
]
