"""Bounded public-web text comparison through a configured search API.

This module is server-only. It does not load archives, cookies, or local files.
Search snippets and fetched page bodies remain distinct evidence classes.
"""
from __future__ import annotations

import datetime as dt
import difflib
import email.utils
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

MAX_POSTS = 10
MIN_TEXT = 24
MAX_INPUT_TEXT = 50000
MAX_CHECKED_TEXT = 5000
MAX_QUERIES = 2
MAX_CANDIDATES = 5
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_BYTES = 1024 * 1024
MAX_PAGE_CHARS = 100000
TIMEOUT = 8
LIMITATIONS = [
    "只检索搜索服务当前收录、可公开访问的来源；全网覆盖率未知，未找到重复不证明原创。",
    "搜索摘要与成功抓取的正文分开标记；失败、未检索和超出范围的内容不能当作零重复。",
    "相似片段不是抄袭结论。页面或搜索服务声明的发布时间不证明最早出现时间，也不证明作者身份或授权。",
    "每条最多检索两个关键片段、读取五个候选；仅比对前 5,000 字符和来源前 100,000 字符。未检查图片、视频、跨语言和语义改写。",
]


class WebCheckError(Exception):
    """A stable public code, never a URL, response body, or credential."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass
class HttpResult:
    url: str
    status: int
    content_type: str
    body: bytes


def public_url(value):
    """Validate URL syntax separately from the DNS checks made on every hop."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise WebCheckError("source_blocked")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        host = parsed.hostname
        if not host or "\\" in value or "%" in host:
            raise ValueError()
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port != (443 if parsed.scheme == "https" else 80):
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        raise WebCheckError("source_blocked") from None


def resolve_public(host, port):
    """Reject the complete DNS answer if any address is not publicly routable."""
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise WebCheckError("source_dns_failed") from None
    addresses = []
    for answer in answers:
        raw = answer[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise WebCheckError("source_blocked") from None
        if not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified:
            raise WebCheckError("source_blocked")
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            mapped = address.ipv4_mapped
            if not mapped.is_global or mapped.is_multicast or mapped.is_reserved:
                raise WebCheckError("source_blocked")
        if isinstance(address, ipaddress.IPv6Address):
            # Translation/tunnel ranges may hide a non-public IPv4 destination.
            if address.sixtofour is not None or address.teredo is not None or address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network("64:ff9b:1::/48"):
                raise WebCheckError("source_blocked")
        if raw not in addresses:
            addresses.append(raw)
    if not addresses:
        raise WebCheckError("source_dns_failed")
    return addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        # Connect to the checked numeric address. Never resolve the hostname again.
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def connect(self):
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


class SafeHTTPTransport:
    """No proxy, cookies, decompression, private addresses, or unbounded redirects."""

    def __init__(self, timeout=TIMEOUT):
        self.timeout = timeout

    def request(self, url, method="GET", headers=None, body=None, max_bytes=MAX_PAGE_BYTES, redirects=3):
        current = public_url(url)
        deadline = time.monotonic() + self.timeout
        for hop in range(redirects + 1):
            parsed = urlsplit(current)
            port = 443 if parsed.scheme == "https" else 80
            addresses = resolve_public(parsed.hostname, port)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebCheckError("source_timeout")
            connection_type = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
            connection = connection_type(parsed.hostname, port, addresses[0], remaining)
            request_headers = {"User-Agent": "OriginalityPublicTextCheck/1.0", "Accept-Encoding": "identity", "Accept": "text/html,text/plain,application/json"}
            request_headers.update(headers or {})
            response = None
            try:
                target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request(method, target, body=body, headers=request_headers)
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location", "")
                    if hop >= redirects or not location or method != "GET":
                        raise WebCheckError("source_redirect_limit")
                    # Provider calls use redirects=0. Auth headers are never forwarded.
                    if headers:
                        raise WebCheckError("source_redirect_limit")
                    current = public_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise WebCheckError("source_http_failed")
                encoding = response.getheader("Content-Encoding", "identity").lower()
                if encoding not in {"", "identity"}:
                    raise WebCheckError("source_unsupported_encoding")
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise WebCheckError("source_too_large")
                chunks, size = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WebCheckError("source_timeout")
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    # read1 avoids extending the deadline with a slowly streamed body.
                    chunk = response.read1(min(65536, max_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise WebCheckError("source_too_large")
                    chunks.append(chunk)
                if length is not None and size != int(length):
                    raise WebCheckError("source_incomplete")
                return HttpResult(current, response.status, response.getheader("Content-Type", ""), b"".join(chunks))
            except WebCheckError:
                raise
            except (TimeoutError, socket.timeout):
                raise WebCheckError("source_timeout") from None
            except (OSError, ssl.SSLError, http.client.HTTPException, UnicodeError, ValueError):
                raise WebCheckError("source_network_failed") from None
            finally:
                if response is not None:
                    response.close()
                connection.close()
        raise WebCheckError("source_redirect_limit")


@dataclass
class SearchCandidate:
    url: str
    title: str = ""
    snippet: str = ""
    published_at: str | None = None


@dataclass
class SearchProvider:
    name: str
    key: str = field(default="", repr=False)
    transport: object = field(default_factory=SafeHTTPTransport, repr=False)
    endpoint: str | None = None

    @property
    def ready(self):
        return self.name in {"tavily", "brave"} and bool(self.key)

    def search(self, query):
        if not self.ready:
            raise WebCheckError("provider_not_configured")
        try:
            if self.name == "tavily":
                payload = json.dumps({"api_key": self.key, "query": query, "search_depth": "basic", "max_results": MAX_CANDIDATES, "include_answer": False, "include_raw_content": False}).encode()
                response = self.transport.request(self.endpoint or "https://api.tavily.com/search", "POST", {"Content-Type": "application/json"}, payload, max_bytes=MAX_SEARCH_BYTES, redirects=0)
                data = json.loads(response.body)
                rows = data.get("results") if isinstance(data, dict) else None
            else:
                endpoint = self.endpoint or "https://api.search.brave.com/res/v1/web/search"
                response = self.transport.request(endpoint + "?" + urlencode({"q": query, "count": MAX_CANDIDATES}), headers={"X-Subscription-Token": self.key}, max_bytes=MAX_SEARCH_BYTES, redirects=0)
                data = json.loads(response.body)
                rows = data.get("web", {}).get("results", []) if isinstance(data, dict) and isinstance(data.get("web", {}), dict) else None
            if not isinstance(rows, list):
                raise WebCheckError("provider_invalid_response")
            candidates = []
            for row in rows[:MAX_CANDIDATES]:
                if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                    continue
                candidates.append(SearchCandidate(row["url"], str(row.get("title") or "")[:500], str(row.get("content", row.get("description")) or "")[:10000], str(row.get("published_date", row.get("published_at")) or "")[:100] or None))
            return candidates
        except WebCheckError as exc:
            if exc.code == "provider_not_configured" or exc.code.startswith("provider_"):
                raise
            raise WebCheckError("provider_request_failed") from None
        except (ValueError, UnicodeError, TypeError, KeyError):
            raise WebCheckError("provider_invalid_response") from None


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = []
        self.title = []
        self.in_title = False
        self.published = None
        self.json_ld = []
        self.json_ld_blocks = []
        self.json_ld_chars = 0
        self.in_json_ld = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "nav", "footer", "header", "svg"}:
            self.skip.append(tag)
        if tag == "title":
            self.in_title = True
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.in_json_ld = True
            self.json_ld = []
        if tag == "meta":
            label = attrs.get("property", attrs.get("name", attrs.get("itemprop", ""))).lower()
            if label in {"article:published_time", "datepublished", "pubdate", "publishdate", "date"}:
                self.published = self.published or attrs.get("content", "")[:100]
        if tag == "time" and attrs.get("itemprop", "").lower() == "datepublished":
            self.published = self.published or attrs.get("datetime", "")[:100]
        if tag in {"p", "div", "br", "li", "article", "section", "h1", "h2", "h3"} and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.skip:
            # Recover reasonably from malformed, non-nested public HTML.
            self.skip = self.skip[:self.skip.index(tag)]
        if tag == "title":
            self.in_title = False
        if tag == "script":
            if self.in_json_ld:
                self.json_ld_blocks.append("".join(self.json_ld))
            self.in_json_ld = False
        if tag in {"p", "div", "article", "li"} and not self.skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_json_ld and self.json_ld_chars < 100000:
            chunk = data[:100000 - self.json_ld_chars]
            self.json_ld.append(chunk)
            self.json_ld_chars += len(chunk)
        if self.in_title:
            self.title.append(data)
        elif not self.skip:
            self.parts.append(data)


def _json_date(value, depth=0):
    if depth > 8:
        return None
    if isinstance(value, dict):
        if isinstance(value.get("datePublished"), str):
            return value["datePublished"][:100]
        for child in list(value.values())[:100]:
            found = _json_date(child, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value[:100]:
            found = _json_date(child, depth + 1)
            if found:
                return found
    return None


def page_text(response):
    content_type = response.content_type.lower()
    media_type = content_type.split(";", 1)[0].strip()
    if media_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise WebCheckError("source_unsupported_type")
    charset = re.search(r"charset=[\"']?([a-z0-9_-]+)", content_type)
    try:
        content = response.body.decode(charset[1] if charset else "utf-8", errors="replace")
    except LookupError:
        content = response.body.decode("utf-8", errors="replace")
    if media_type == "text/plain":
        return {"text": content[:MAX_PAGE_CHARS], "title": "", "published_at": None, "text_truncated": len(content) > MAX_PAGE_CHARS}
    parser = PageParser()
    parser.feed(content)
    published = parser.published
    if not published:
        for block in parser.json_ld_blocks:
            try:
                published = _json_date(json.loads(block))
            except (ValueError, TypeError, RecursionError):
                continue
            if published:
                break
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts)).strip()
    return {"text": text[:MAX_PAGE_CHARS], "title": "".join(parser.title).strip()[:500], "published_at": published, "text_truncated": len(text) > MAX_PAGE_CHARS}


def clean(text):
    text = re.sub(r"https?://\S+", "", str(text))
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def _clean_map(text):
    normalized, positions = [], []
    hidden = [(m.start(), m.end()) for m in re.finditer(r"https?://\S+", text)]
    url_index = 0
    for index, char in enumerate(text):
        while url_index < len(hidden) and index >= hidden[url_index][1]:
            url_index += 1
        if url_index < len(hidden) and hidden[url_index][0] <= index < hidden[url_index][1]:
            continue
        for value in unicodedata.normalize("NFKC", char).casefold():
            if value.isalnum():
                normalized.append(value)
                positions.append(index)
    return "".join(normalized), positions


def compare_text(post, source):
    left, left_map = _clean_map(post)
    right, right_map = _clean_map(source)
    if len(left) < MIN_TEXT or len(right) < MIN_TEXT:
        return None
    exact = right.find(left)
    if exact >= 0:
        blocks, score = [(0, exact, len(left))], 1.0
    else:
        # Seed bounded windows with distinctive four-character fragments. The
        # comparison never runs an unbounded diff against an entire large page.
        positions = {}
        for index in range(len(right) - 3):
            gram = right[index:index + 4]
            bucket = positions.setdefault(gram, [])
            if len(bucket) < 4:
                bucket.append(index)
        seeds = []
        stride = max(4, len(left) // 30)
        for index in range(0, len(left) - 3, stride):
            for offset in positions.get(left[index:index + 4], []):
                seeds.append(max(0, offset - index))
        if not seeds:
            return None
        grouped = {}
        for offset in seeds:
            bucket = offset // max(32, len(left) // 4)
            grouped.setdefault(bucket, []).append(offset)
        windows = sorted(grouped.values(), key=len, reverse=True)[:4]
        best = (0, [])
        # Limit expensive diff input even when the submitted post is 5,000 chars.
        fragment = left[:2000]
        for offsets in windows:
            start = max(0, min(offsets) - 120)
            window = right[start:start + min(len(fragment) * 2 + 240, 4240)]
            matcher = difflib.SequenceMatcher(None, fragment, window, autojunk=False)
            found = [(block.a, block.b + start, block.size) for block in matcher.get_matching_blocks() if block.size]
            coverage = sum(block[2] for block in found) / len(left)
            if coverage > best[0]:
                best = coverage, found
        score, blocks = best
        if score < 0.5 or not blocks or max(block[2] for block in blocks) < MIN_TEXT:
            return None
    largest = max(blocks, key=lambda block: block[2])
    a, b, length = largest
    a_end = left_map[min(a + length - 1, len(left_map) - 1)] + 1
    b_end = right_map[min(b + length - 1, len(right_map) - 1)] + 1
    original_matched = {left_map[index] for start, _, size in blocks for index in range(start, start + size)}
    return {"score": round(score, 4), "matched_chars": len(original_matched), "post_excerpt": post[left_map[a]:a_end][:500], "source_excerpt": source[right_map[b]:b_end][:500]}


def search_queries(text):
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    parts = [p.strip() for p in re.split(r"[。！？.!?\n]", text) if len(clean(p)) >= MIN_TEXT]
    selected = sorted(enumerate(parts), key=lambda item: (-len(clean(item[1])), item[0]))[:MAX_QUERIES]
    if not selected:
        selected = [(0, text)]
    queries = []
    for _, part in sorted(selected):
        # Keep meaningful English words intact and limit data sent to the provider.
        fragment = " ".join(part[:180].split()[:24])
        if fragment and fragment not in queries:
            queries.append(fragment)
    if len(queries) == 1 and len(text) > 300:
        queries.append(text[len(text) // 2:len(text) // 2 + 180])
    return queries[:MAX_QUERIES]


def _moment(value):
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except (ValueError, TypeError, IndexError, OverflowError):
            return None
    # A date-only or timezone-free declaration cannot establish precise order.
    return moment if moment.tzinfo is not None else None


def temporal_relation(source, post):
    first, second = _moment(source), _moment(post)
    if first is None or second is None:
        return "unknown"
    return "earlier" if first < second else "later" if first > second else "same"


def _x_id(url):
    try:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
            return None
        found = re.search(r"/status/(\d+)(?:/|$)", parts.path)
        return found[1] if found else None
    except ValueError:
        return None


def same_post(candidate_url, post):
    source_id = _x_id(candidate_url)
    own_id = _x_id(post.get("url", "")) or (post["id"] if post["id"].isdigit() else None)
    if source_id and own_id and source_id == own_id:
        return True
    return bool(post.get("url") and candidate_url.rstrip("/") == post["url"].split("#", 1)[0].rstrip("/"))


def validate_posts(posts):
    if not isinstance(posts, list) or not 1 <= len(posts) <= MAX_POSTS:
        raise ValueError("每次联网检查需要 1–10 条帖子。")
    result, ids = [], set()
    for post in posts:
        if not isinstance(post, dict) or set(post) - {"id", "text", "url", "created_at", "text_complete"}:
            raise ValueError("联网接口仅接受 id、text、url、created_at 与 text_complete 字段。")
        if not isinstance(post.get("id"), str) or not post["id"] or len(post["id"]) > 200 or post["id"] in ids:
            raise ValueError("帖子 id 必须唯一，且不超过 200 字符。")
        if not isinstance(post.get("text"), str) or len(post["text"]) > MAX_INPUT_TEXT:
            raise ValueError("每条输入正文最多 50,000 字符；较长正文请分段并使用唯一 id。")
        for key, limit in (("url", 2048), ("created_at", 100)):
            if not isinstance(post.get(key, ""), str) or len(post.get(key, "")) > limit:
                raise ValueError("帖子链接或日期格式无效。")
        if "text_complete" in post and not isinstance(post["text_complete"], bool):
            raise ValueError("正文完整标记必须为布尔值。")
        ids.add(post["id"])
        result.append({**{key: post.get(key, "") for key in ("id", "text", "url", "created_at")},
                       "text_complete": post.get("text_complete", False)})
    return result


class WebChecker:
    def __init__(self, provider, transport=None, content_reviewer=None):
        self.provider = provider
        self.transport = transport or SafeHTTPTransport()
        from content_review import ContentReviewer
        self.content_reviewer = content_reviewer or ContentReviewer()

    def check(self, posts):
        posts = validate_posts(posts)
        checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
        results = []
        for post in posts:
            result = self._post(post)
            sources = result.pop("_review_sources")
            if result["status"] in {"failed", "skipped"} or result["checked_chars"] < len(post["text"]):
                # A failed/shortened search window cannot authenticate a whole-
                # post model score in the strict browser report contract.
                result["content_review"] = self.content_reviewer.unavailable_search()
            else:
                result["content_review"] = self.content_reviewer.review(post, sources, truncated=result["text_truncated"])
            # Authenticate a cited nonmatching source without returning its full body.
            for criterion in result["content_review"]["criteria"]:
                if criterion["source_url"] and criterion["source_excerpt"]:
                    for checked in result["source_checks"]:
                        if checked.get("url") == criterion["source_url"] and checked.get("page_status") == "fetched" and checked.get("source_kind") == "page_body":
                            checked["source_excerpt"] = criterion["source_excerpt"]
            results.append(result)
        searched = sum(p["successful_queries"] > 0 for p in results)
        return {"schema_version": 1, "provider": self.provider.name, "checked_at": checked_at,
                "coverage": {"requested": len(posts), "searched": searched, "compared": sum(p["sources_checked"] > 0 for p in results),
                             "failed": sum(p["status"] == "failed" for p in results), "partial": sum(p["status"] == "partial" for p in results),
                             "skipped": sum(p["status"] == "skipped" for p in results), "search_complete": all(p["successful_queries"] == p["query_count"] and p["query_count"] > 0 for p in results),
                             "web_coverage": "unknown", "checked_chars": sum(p["checked_chars"] for p in results), "original_chars": sum(p["original_chars"] for p in results)},
                "posts": results, "limitations": [*LIMITATIONS,
                    "可选模型评分仅评估已提供完整文字与有界实际来源正文，不代表官方认定；无来源、截断、模型未配置/失败及缺创作流程或许可的条款仍未知。"]}

    def _post(self, post):
        text = post["text"][:MAX_CHECKED_TEXT]
        result = {"id": post["id"], "status": "skipped", "query_count": 0, "successful_queries": 0,
                  "candidates_found": 0, "sources_checked": 0, "source_checks": [], "matches": [], "same_post": [], "issues": [],
                  "checked_chars": len(text), "original_chars": len(post["text"]), "text_truncated": len(text) != len(post["text"]), "max_similarity": None,
                  "_review_sources": []}
        if len(clean(text)) < MIN_TEXT:
            result["issues"].append({"code": "text_too_short"})
            result["checked_chars"] = 0
            return result
        candidates = {}
        candidate_groups = []
        queries = search_queries(text)
        for query in queries:
            result["query_count"] += 1
            group = []
            try:
                found = self.provider.search(query)
                result["successful_queries"] += 1
                for candidate in found:
                    try:
                        url = public_url(candidate.url)
                    except WebCheckError:
                        result["issues"].append({"code": "source_blocked"})
                        continue
                    candidate.url = url
                    if same_post(url, post):
                        if not any(item["url"] == url for item in result["same_post"]):
                            result["same_post"].append({"url": url, "title": candidate.title, "reason": "same_post"})
                    else:
                        candidates.setdefault(url, candidate)
                        if url not in group:
                            group.append(url)
            except WebCheckError as exc:
                result["issues"].append({"code": exc.code})
            candidate_groups.append(group)
        result["candidates_found"] = len(candidates)
        if not result["successful_queries"]:
            result["status"] = "failed"
            result["checked_chars"] = 0
            return result
        # Take each query's first result, then each query's second result, etc.
        # Deduplication keeps the five-source budget while allowing both queries
        # to contribute even when the first query returned five distinct URLs.
        selected = []
        for rank in range(max(map(len, candidate_groups), default=0)):
            for group in candidate_groups:
                if rank < len(group) and group[rank] not in selected:
                    selected.append(group[rank])
                    if len(selected) == MAX_CANDIDATES:
                        break
            if len(selected) == MAX_CANDIDATES:
                break
        for url in selected:
            candidate = candidates[url]
            page = None
            failure = None
            try:
                response = self.transport.request(candidate.url)
                if same_post(response.url, post):
                    result["same_post"].append({"url": response.url, "title": candidate.title, "reason": "same_post"})
                    result["source_checks"].append({"url": response.url, "title": candidate.title[:500],
                        "page_status": "same_post", "source_kind": None, "source_text_truncated": None,
                        "score": None, "matched_chars": 0})
                    continue
                page = page_text(response)
                if len(clean(page["text"])) < MIN_TEXT:
                    raise WebCheckError("source_no_comparable_text")
                result["sources_checked"] += 1
                result["_review_sources"].append({"url": response.url, "text": page["text"], "text_truncated": page["text_truncated"]})
                if page["text_truncated"]:
                    result["issues"].append({"code": "source_text_truncated"})
                matching = compare_text(text, page["text"])
                kind = "page_body"
            except WebCheckError as exc:
                page = None
                failure = exc.code
                result["issues"].append({"code": failure})
                # Blocked/private links must not become clickable snippet evidence.
                if failure in {"source_blocked", "source_dns_failed"}:
                    result["source_checks"].append({"page_status": failure, "source_kind": None,
                        "source_text_truncated": None, "score": None, "matched_chars": 0})
                    continue
                snippet = re.sub(r"<[^>]+>", "", candidate.snippet)
                matching = compare_text(text, snippet)
                kind = "search_snippet"
            result["source_checks"].append({"url": response.url if page else candidate.url,
                "title": (page["title"] if page and page["title"] else candidate.title)[:500],
                "page_status": "fetched" if page else failure, "source_kind": kind,
                "source_text_truncated": page["text_truncated"] if page else None,
                "score": matching["score"] if matching else None,
                "matched_chars": matching["matched_chars"] if matching else 0})
            if matching:
                publication = page["published_at"] if page and page["published_at"] else candidate.published_at
                result["matches"].append({**matching, "url": response.url if page else candidate.url,
                    "title": page["title"] if page and page["title"] else candidate.title,
                    "source_kind": kind, "page_status": "fetched" if page else failure,
                    "source_text_truncated": page["text_truncated"] if page else None,
                    "published_at": publication, "published_at_basis": "page_metadata" if page and page["published_at"] else "search_result" if publication else None,
                    "temporal_relation": temporal_relation(publication, post["created_at"])})
        result["matches"].sort(key=lambda match: (match["source_kind"] == "page_body", match["score"]), reverse=True)
        result["matches_total"] = len(result["matches"])
        result["matches"] = result["matches"][:3]
        result["max_similarity"] = max((m["score"] for m in result["matches"]), default=None)
        incomplete = bool(result["issues"] or result["text_truncated"] or len(candidates) > MAX_CANDIDATES)
        result["status"] = "partial" if incomplete else "matched" if result["matches"] else "no_match"
        return result
