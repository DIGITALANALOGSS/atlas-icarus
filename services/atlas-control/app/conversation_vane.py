"""Opt-in Vane research. No retries and no claim of backend cancellation."""
import asyncio
import json
import logging
import math
import os
import re
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.conversation_search import SearchError

log = logging.getLogger(__name__)
MAX_BYTES = 524288


def _url(value):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise SearchError("Vane URL is invalid")
    try:
        parts = urlsplit(value)
        parts.port
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError
    except ValueError:
        raise SearchError("Vane URL is invalid") from None
    return parts


def _configuration(query):
    base = os.environ.get("ATLAS_VANE_BASE_URL", "")
    parts = _url(base)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise SearchError("Vane base URL must be a service origin")

    def model(prefix):
        provider = os.environ.get(f"ATLAS_VANE_{prefix}_PROVIDER_ID", "")
        key = os.environ.get(f"ATLAS_VANE_{prefix}_MODEL", "").strip()
        try:
            UUID(provider)
        except (ValueError, AttributeError):
            raise SearchError("Vane provider configuration is invalid") from None
        if not key or len(key) > 256:
            raise SearchError("Vane model configuration is missing or invalid")
        return {"providerId": provider, "key": key}

    mode = os.environ.get("ATLAS_VANE_OPTIMIZATION_MODE", "speed")
    if mode not in ("speed", "balanced", "quality"):
        raise SearchError("Vane optimization mode is invalid")

    return base.rstrip("/") + "/api/search", {
        "chatModel": model("CHAT"),
        "embeddingModel": model("EMBEDDING"),
        "optimizationMode": mode,
        "sources": ["web"],
        "query": query,
        "history": [],
        "stream": False,
    }



def _validate_source_policy(allowed_hosts, required_urls):
    if (
        type(allowed_hosts) is not tuple
        or type(required_urls) is not tuple
        or len(allowed_hosts) > 32
        or len(required_urls) > 32
    ):
        raise SearchError("source policy requires bounded tuples")
    for host in allowed_hosts:
        if (
            not isinstance(host, str)
            or len(host) > 253
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                for part in host.split(".")
            )
        ):
            raise SearchError("source policy requires exact lowercase hosts")
    for url in required_urls:
        parts = _url(url)
        if len(url) > 8192 or parts.fragment:
            raise SearchError("required source URL is invalid")
        if allowed_hosts and parts.hostname not in allowed_hosts:
            raise SearchError("required URL is outside the allowed hosts")


def _check_cited_source_policy(
    source_urls, citations, allowed_hosts, required_urls
):
    _validate_source_policy(allowed_hosts, required_urls)
    cited_urls = {source_urls[int(number) - 1] for number in citations}
    if allowed_hosts and any(
        _url(url).hostname not in allowed_hosts for url in cited_urls
    ):
        raise SearchError("cited source is outside the allowed hosts")
    if not set(required_urls).issubset(cited_urls):
        raise SearchError("answer does not cite every required source URL")


def _answer(data, *, allowed_source_hosts=(), required_source_urls=()):
    if not isinstance(data, dict) or "error" in data:
        raise SearchError("Vane returned an invalid research response")
    message = data.get("message")
    sources = data.get("sources")
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > 65536
    ):
        raise SearchError("Vane answer is empty or exceeds the limit")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 128:
        raise SearchError("Vane returned no usable source mapping")

    lines = []
    for number, source in enumerate(sources, 1):
        if not isinstance(source, dict):
            raise SearchError("Vane source mapping is invalid")
        metadata = source.get("metadata")
        if not isinstance(metadata, dict):
            raise SearchError("Vane source metadata is invalid")
        url = metadata.get("url")
        _url(url)
        if len(url) > 8192:
            raise SearchError("Vane source URL exceeds the limit")
        title = metadata.get("title")
        if not isinstance(title, str) or not title.strip():
            title = "Untitled source"
        title = " ".join(title.split())[:500]
        lines.append(
            f"[{number}] {json.dumps(title, ensure_ascii=False)} — {url}"
        )

    citations = re.findall(r"\[(\d{1,10})\]", message)
    if not citations or any(
        not 1 <= int(number) <= len(sources) for number in citations
    ):
        raise SearchError("Vane citation numbers do not match returned sources")

    source_urls = [
        source["metadata"]["url"] for source in sources
    ]
    markdown_links = re.findall(
        r"\]\((https?://[^ \t\r\n]+?)\)",
        message,
        flags=re.IGNORECASE,
    )
    if any(target not in source_urls for target in markdown_links):
        raise SearchError(
            "Vane answer contains a link absent from returned sources"
        )

    linked_citations = re.findall(
        r"\]\((https?://[^ \t\r\n]+?)\)\s*\[(\d{1,10})\]",
        message,
        flags=re.IGNORECASE,
    )
    for target, number in linked_citations:
        index = int(number) - 1
        if not 0 <= index < len(source_urls) or source_urls[index] != target:
            raise SearchError(
                "Vane answer link does not match its citation number"
            )

    _check_cited_source_policy(
        source_urls, citations, allowed_source_hosts, required_source_urls
    )

    return (
        message.strip()
        + "\n\nVane sources "
        "(numbering checked; claims not independently verified):\n"
        + "\n".join(lines)
    )


async def vane_answer(
    query, *, remaining, allowed_source_hosts=(), required_source_urls=()
):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise SearchError("query must contain 1 to 500 characters")
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, (int, float))
        or not math.isfinite(remaining)
        or remaining <= 0
    ):
        raise SearchError("research deadline is invalid or expired")

    _validate_source_policy(allowed_source_hosts, required_source_urls)
    endpoint, payload = _configuration(query.strip())
    started = time.monotonic()

    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            log.info(
                "Vane research waiting; elapsed_seconds=%.1f",
                time.monotonic() - started,
            )

    async def work():
        timeout = httpx.Timeout(
            remaining, connect=min(5.0, remaining)
        )
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=False
        ) as client:
            async with client.stream(
                "POST", endpoint, json=payload
            ) as response:
                if response.status_code != 200:
                    raise SearchError(
                        f"Vane returned HTTP {response.status_code}"
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_BYTES:
                        raise SearchError("Vane response exceeds the size limit")
                    chunks.extend(chunk)
        try:
            data = json.loads(chunks)
        except (ValueError, UnicodeError, RecursionError):
            raise SearchError("Vane returned invalid JSON") from None
        return _answer(
            data,
            allowed_source_hosts=allowed_source_hosts,
            required_source_urls=required_source_urls,
        )

    outcome = "failed"
    ticker = asyncio.create_task(heartbeat())
    log.info("Vane research started")
    try:
        result = await asyncio.wait_for(work(), timeout=remaining)
        outcome = "completed"
        elapsed = time.monotonic() - started
        return result + f"\n\nAtlas research elapsed: {elapsed:.2f} seconds."
    except (asyncio.TimeoutError, httpx.TimeoutException):
        outcome = "timed_out"
        raise SearchError(
            "Vane request timed out; backend cancellation unverified"
        ) from None
    except httpx.RequestError:
        raise SearchError(
            "Vane transport failed; backend state unknown"
        ) from None
    except asyncio.CancelledError:
        outcome = "client_cancelled"
        raise
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        log.info(
            "Vane research ended; outcome=%s; elapsed_seconds=%.2f; "
            "backend_cancellation_unverified=true",
            outcome, time.monotonic() - started,
        )
