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

import json
from typing import Any
from urllib.parse import urlencode
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


def _rpc_call(action: str, *, data: dict[str, Any], rpc_url: str) -> dict[str, Any]:
    body = json.dumps({"action": action, **data}).encode("utf-8")
    req = Request(
        rpc_url,
        data=body,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
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
    """Fail-closed: True only when a confirmed send settles to `pay_to` for the
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


__all__ = [
    "DEFAULT_RPC_URL",
    "OPENED_RPC_ERRORS",
    "XNO_TO_RAW",
    "block_info_for",
    "raw_to_xno",
    "verify_nano_payment",
    "xno_to_raw",
]
