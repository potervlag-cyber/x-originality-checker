"""Verify a deployed service with a public quotation, not fixture search results."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webcheck import SafeHTTPTransport, WebCheckError, public_url

SAMPLE_ID = "live-python-tutorial"
SAMPLE_TEXT = ("Python is an easy to learn, powerful programming language. "
               "It has efficient high-level data structures and a simple but effective approach to object-oriented programming.")
REFERENCE_URL = "https://docs.python.org/3/tutorial/"
PAGE_ORIGIN = "https://potervlag-cyber.github.io"


def nonlocal_url(value):
    value = public_url(value)
    parsed = urlsplit(value)
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("private_target")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if all(part.isdigit() or part.startswith("0x") for part in host.split(".")):
            raise ValueError("private_target")
    else:
        if not address.is_global:
            raise ValueError("private_target")
    return value


def service_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        raise ValueError("Service endpoint must be a public HTTPS URL without credentials, query, or fragment.")
    return nonlocal_url(value).rstrip("/")


def assess_report(report):
    if not isinstance(report, dict) or isinstance(report.get("schema_version"), bool) or report.get("schema_version") != 1 or report.get("provider") not in {"tavily", "brave"}:
        raise ValueError("invalid_search_report")
    posts = report.get("posts")
    if not isinstance(posts, list) or len(posts) != 1 or not isinstance(posts[0], dict) or posts[0].get("id") != SAMPLE_ID:
        raise ValueError("search_result_mismatch")
    post = posts[0]
    if post.get("status") not in {"matched", "partial"} or isinstance(post.get("successful_queries"), bool) or not isinstance(post.get("successful_queries"), int) or post["successful_queries"] < 1:
        raise ValueError("no_successful_search")
    matches = post.get("matches")
    if not isinstance(matches, list):
        raise ValueError("invalid_source_evidence")
    evidence = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        score = match.get("score")
        chars = match.get("matched_chars")
        if (match.get("source_kind") == "page_body" and match.get("page_status") == "fetched"
                and isinstance(score, (int, float)) and not isinstance(score, bool) and 0.7 <= score <= 1
                and isinstance(chars, int) and not isinstance(chars, bool) and chars >= 24
                and isinstance(match.get("source_excerpt"), str) and len(match["source_excerpt"]) >= 24):
            evidence.append({"url": nonlocal_url(match.get("url")), "score": score, "matched_chars": chars,
                             "source_kind": "page_body", "page_status": "fetched",
                             "source_excerpt_sha256": hashlib.sha256(match["source_excerpt"].encode()).hexdigest()})
    if not evidence or isinstance(post.get("sources_checked"), bool) or not isinstance(post.get("sources_checked"), int) or post["sources_checked"] < 1:
        raise ValueError("no_fetched_comparable_source")
    return {"provider": report["provider"], "post_status": post["status"],
            "successful_queries": post["successful_queries"], "sources_checked": post["sources_checked"],
            "source_evidence": evidence,
            "issues": [item.get("code") for item in post.get("issues", []) if isinstance(item, dict)]}


def check(endpoint, access_token, transport=None):
    endpoint = service_url(endpoint)
    if not isinstance(access_token, str) or not access_token or any(ord(c) < 32 for c in access_token):
        raise ValueError("service_access_token_missing_or_invalid")
    transport = transport or SafeHTTPTransport(timeout=90)
    headers = {"Origin": PAGE_ORIGIN, "Authorization": "Bearer " + access_token,
               "Accept": "application/json", "Content-Type": "application/json"}
    status = json.loads(transport.request(endpoint + "/api/webcheck/status", headers=headers, max_bytes=16384, redirects=0).body)
    if not isinstance(status, dict) or status.get("ready") is not True:
        raise ValueError("service_provider_not_configured")
    payload = {"consent": True, "posts": [{"id": SAMPLE_ID, "text": SAMPLE_TEXT, "url": "", "created_at": ""}]}
    response = transport.request(endpoint + "/api/webcheck", "POST", headers=headers,
                                 body=json.dumps(payload).encode("utf-8"), max_bytes=2 * 1024 * 1024, redirects=0)
    report = json.loads(response.body)
    return {"status": "PASS", "endpoint": endpoint, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "sample_reference": REFERENCE_URL, "sample_text_sha256": hashlib.sha256(SAMPLE_TEXT.encode()).hexdigest(),
            **assess_report(report),
            "scope": "One real deployed API request with a public quotation and fetched body evidence. Browser UI/CORS and deployment identity require separate verification.",
            "web_coverage": "unknown"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, help="Write a non-secret verification receipt outside the source tree.")
    args = parser.parse_args()
    token = os.environ.get("WEBCHECK_ACCESS_TOKEN", "")
    try:
        receipt = check(args.endpoint, token)
        code = 0
    except WebCheckError as exc:
        receipt, code = {"status": "FAIL", "code": exc.code}, 1
    except (OSError, ValueError, TypeError, KeyError):
        # Do not echo exceptions, credentials, upstream bodies, or command arguments.
        receipt, code = {"status": "FAIL", "code": "live_verification_failed"}, 1
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
