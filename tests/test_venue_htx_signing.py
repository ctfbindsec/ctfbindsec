"""HTX HMAC-SHA256 signing tests — no network required."""

from __future__ import annotations

import base64
import hashlib
import hmac

from ctfquant.venue_htx import _coerce_client_order_id, _sign


def test_sign_known_input():
    """Replicates the HTX docs' signature shape and confirms our impl matches.

    Spec: signature = base64(hmac_sha256(secret, METHOD\\nhost\\npath\\nsorted_query))
    We don't have a published vector from HTX so we sanity-check by
    recomputing in-test and confirming our function matches.
    """

    method = "POST"
    host = "api.hbdm.com"
    path = "/linear-swap-api/v1/swap_cross_account_info"
    params = {
        "AccessKeyId": "AKID",
        "SignatureMethod": "HmacSHA256",
        "SignatureVersion": "2",
        "Timestamp": "2026-05-06T22:00:00",
    }
    secret = "SECRET"

    sig = _sign(method, host, path, params, secret)

    # recompute manually to confirm — use percent-encoded query like _sign
    from urllib.parse import quote
    sorted_kvs = sorted(params.items())
    encoded = "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in sorted_kvs
    )
    expected_payload = f"POST\napi.hbdm.com\n{path}\n{encoded}"
    expected_digest = hmac.new(
        secret.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_sig = base64.b64encode(expected_digest).decode("utf-8")

    assert sig == expected_sig


def test_sign_url_encoding_special_chars():
    """ISO timestamps contain ':' which MUST be percent-encoded."""

    sig = _sign(
        "GET", "api.hbdm.com", "/path",
        {"Timestamp": "2026-05-06T22:00:00", "AccessKeyId": "AKID"},
        "S",
    )
    # ':' → %3A, 'T' stays as 'T'
    expected_payload = (
        "GET\napi.hbdm.com\n/path\n"
        "AccessKeyId=AKID&Timestamp=2026-05-06T22%3A00%3A00"
    )
    expected = base64.b64encode(
        hmac.new(b"S", expected_payload.encode(), hashlib.sha256).digest()
    ).decode("utf-8")
    assert sig == expected


def test_coerce_client_order_id_hex():
    coid = "f" * 64
    n = _coerce_client_order_id(coid)
    assert isinstance(n, int)
    assert n > 0


def test_coerce_client_order_id_falls_back_for_non_hex():
    n = _coerce_client_order_id("not-a-hex-string-at-all")
    assert isinstance(n, int)
    assert n > 0
