from __future__ import annotations

import hashlib
import ipaddress
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .blocks import CollectionDigestConfig, WebCollectionConfig
from .durable_jobs import CollectionReceipt, DurableJobStore
from .models import utc_now
from .platform_harness import PlatformHarness


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "article", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)

    def result(self) -> tuple[str, str]:
        title = re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()
        text = re.sub(r"[ \t\f\v]+", " ", "".join(self.parts))
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return title, text


class ControlledWebCollector:
    def __init__(self, *, jobs: DurableJobStore, harness: PlatformHarness) -> None:
        self.jobs = jobs
        self.harness = harness

    async def collect(
        self,
        *,
        config: WebCollectionConfig,
        sources: Any,
        application_id: str,
        run_id: str,
        job_context: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(sources, list):
            raise TypeError("web_collection sources must resolve to an array")
        job_id = str(job_context.get("job_id") or "")
        worker_id = str(job_context.get("worker_id") or "")
        lease_version = int(job_context.get("lease_version") or 0)
        if not job_id or not worker_id or lease_version <= 0:
            if not sources:
                return {
                    "items": [],
                    "receipts": [],
                    "counts": {},
                    "job_id": "",
                    "run_id": run_id,
                    "claim_scope": (
                        "Empty-source contract path only; durable source collection was not exercised."
                    ),
                    "excluded_claims": [
                        "source collection without durable job context",
                        "permission to scrape arbitrary sites",
                        "external notification delivery",
                    ],
                }
            raise ValueError(
                "web_collection requires durable __job__ context with job_id, worker_id, and lease_version"
            )
        if len(sources) > config.max_sources:
            raise ValueError(
                f"web_collection source limit exceeded: {len(sources)} > {config.max_sources}"
            )

        allowed_hosts = {item.strip().casefold() for item in config.allowed_hosts if item.strip()}
        if not allowed_hosts:
            raise ValueError("web_collection requires at least one allowed host")
        normalized = [self._source(item, config.permission_basis) for item in sources]
        receipts: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        robots_cache: dict[str, tuple[str, str]] = {}
        async with httpx.AsyncClient(
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": config.user_agent},
        ) as client:
            for index, source in enumerate(normalized):
                result = await self._collect_one(
                    client=client,
                    config=config,
                    source=source,
                    allowed_hosts=allowed_hosts,
                    robots_cache=robots_cache,
                    application_id=application_id,
                    run_id=run_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    lease_version=lease_version,
                )
                receipts.append(result)
                if result["status"] in {"new", "changed", "unchanged", "resumed"}:
                    items.append(
                        {
                            "title": result.get("title") or result["canonical_url"],
                            "url": result["canonical_url"],
                            "status": result["status"],
                            "content_hash": result.get("content_hash", ""),
                            "excerpt": result.get("excerpt", ""),
                            "collected_at": result["collected_at"],
                            "citation": result["canonical_url"],
                        }
                    )
                if result["status"] not in {"failed"}:
                    current = await self.jobs.get(job_id)
                    completed = list(current.checkpoint.get("completed_source_keys", []))
                    if result["source_key"] not in completed:
                        completed.append(result["source_key"])
                    await self.jobs.checkpoint(
                        job_id,
                        worker_id=worker_id,
                        lease_version=lease_version,
                        values={
                            "completed_source_keys": completed,
                            "last_source_index": index,
                            "last_source_status": result["status"],
                            "receipt_count": len(receipts),
                        },
                    )
                if config.fail_on_source_error and result["status"] in {
                    "failed",
                    "denied",
                    "oversized",
                }:
                    raise RuntimeError(
                        f"source collection {result['status']}: {result['requested_url']}"
                    )

        counts: dict[str, int] = {}
        for receipt in receipts:
            status = str(receipt["status"])
            counts[status] = counts.get(status, 0) + 1
        return {
            "items": items,
            "receipts": receipts,
            "counts": counts,
            "job_id": job_id,
            "run_id": run_id,
            "claim_scope": (
                "Controlled allowlisted-source collection with declared permission basis; "
                "not permission to collect arbitrary sites."
            ),
            "excluded_claims": [
                "permission to scrape arbitrary sites",
                "external notification delivery",
                "production unattended reliability",
            ],
        }

    async def _collect_one(
        self,
        *,
        client: httpx.AsyncClient,
        config: WebCollectionConfig,
        source: dict[str, str],
        allowed_hosts: set[str],
        robots_cache: dict[str, tuple[str, str]],
        application_id: str,
        run_id: str,
        job_id: str,
        worker_id: str,
        lease_version: int,
    ) -> dict[str, Any]:
        requested_url = source["url"]
        permission_basis = source["permission_basis"]
        source_key = hashlib.sha256(
            self._canonical_url(requested_url).encode()
        ).hexdigest()[:24]
        existing = await self.jobs.receipt_for_source(job_id, source_key)
        if existing and existing.status in {"new", "changed", "unchanged"}:
            await self.jobs.append_event(
                job_id,
                "collection.source_resumed",
                {"source_key": source_key, "receipt_id": existing.id},
            )
            return {
                **existing.model_dump(mode="json"),
                "status": "resumed",
                "original_status": existing.status,
            }
        if existing and existing.status in {"denied", "oversized"}:
            await self.jobs.append_event(
                job_id,
                "collection.source_problem_reused",
                {
                    "source_key": source_key,
                    "receipt_id": existing.id,
                    "status": existing.status,
                },
            )
            return existing.model_dump(mode="json")

        collected_at = utc_now()
        parsed = urlsplit(requested_url)
        host = (parsed.hostname or "").casefold()
        denied_reason = self._url_denial(parsed, host, allowed_hosts)
        if denied_reason:
            return await self._save_problem_receipt(
                status="denied",
                error=denied_reason,
                source_key=source_key,
                requested_url=requested_url,
                canonical_url=requested_url,
                final_url="",
                host=host,
                permission_basis=permission_basis,
                robots_checked=False,
                robots_allowed=None,
                application_id=application_id,
                run_id=run_id,
                job_id=job_id,
                worker_id=worker_id,
                lease_version=lease_version,
                collected_at=collected_at,
            )

        self.harness.enforce_network_egress_policy(
            surface="web_collection",
            hostname=host,
        )
        robots_allowed: bool | None = None
        robots_error = ""
        if config.respect_robots:
            robots_allowed, robots_error = await self._robots_allowed(
                client,
                requested_url,
                config.user_agent,
                robots_cache,
            )
            if robots_allowed is False or (
                robots_allowed is None and config.robots_failure_policy == "deny"
            ):
                reason = robots_error or "robots.txt denies this collector"
                return await self._save_problem_receipt(
                    status="denied",
                    error=reason,
                    source_key=source_key,
                    requested_url=requested_url,
                    canonical_url=self._canonical_url(requested_url),
                    final_url="",
                    host=host,
                    permission_basis=permission_basis,
                    robots_checked=True,
                    robots_allowed=robots_allowed,
                    application_id=application_id,
                    run_id=run_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    lease_version=lease_version,
                    collected_at=collected_at,
                )

        try:
            response = await client.get(requested_url)
            response.raise_for_status()
            final_url = str(response.url)
            final_host = (urlsplit(final_url).hostname or "").casefold()
            if final_host not in allowed_hosts:
                raise RuntimeError(f"redirect target is outside allowed hosts: {final_host}")
            content = response.content
            if len(content) > config.max_content_bytes:
                return await self._save_problem_receipt(
                    status="oversized",
                    error=(
                        f"response size {len(content)} exceeds {config.max_content_bytes} bytes"
                    ),
                    source_key=source_key,
                    requested_url=requested_url,
                    canonical_url=self._canonical_url(final_url),
                    final_url=final_url,
                    host=final_host,
                    permission_basis=permission_basis,
                    robots_checked=config.respect_robots,
                    robots_allowed=robots_allowed,
                    application_id=application_id,
                    run_id=run_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    lease_version=lease_version,
                    collected_at=collected_at,
                    http_status=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    content_bytes=len(content),
                )
            title, text = self._extract(response)
            normalized_text = re.sub(r"\s+", " ", text).strip()
            content_hash = hashlib.sha256(normalized_text.encode()).hexdigest()
            canonical_url = self._canonical_url(final_url)
            previous = await self.jobs.latest_receipt(
                application_id,
                canonical_url,
                exclude_job_id=job_id,
            )
            status = (
                "unchanged"
                if previous and previous.content_hash == content_hash
                else "changed"
                if previous
                else "new"
            )
            now = utc_now()
            receipt = CollectionReceipt(
                id=self._receipt_id(job_id, source_key),
                job_id=job_id,
                application_id=application_id,
                run_id=run_id,
                source_key=source_key,
                requested_url=requested_url,
                final_url=final_url,
                canonical_url=canonical_url,
                host=final_host,
                permission_basis=permission_basis,
                robots_checked=config.respect_robots,
                robots_allowed=robots_allowed,
                status=status,
                http_status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                content_bytes=len(content),
                content_hash=content_hash,
                previous_receipt_id=previous.id if previous else None,
                previous_content_hash=previous.content_hash if previous else "",
                title=title,
                excerpt=normalized_text[:1000],
                transformation={
                    "extractor": "readable_html_v1",
                    "normalized_characters": len(normalized_text),
                    "robots_error": robots_error,
                },
                collected_at=collected_at,
                created_at=now,
                updated_at=now,
            )
            saved = await self.jobs.save_receipt(
                receipt,
                worker_id=worker_id,
                lease_version=lease_version,
            )
            return saved.model_dump(mode="json")
        except Exception as error:
            return await self._save_problem_receipt(
                status="failed",
                error=str(error),
                source_key=source_key,
                requested_url=requested_url,
                canonical_url=self._canonical_url(requested_url),
                final_url="",
                host=host,
                permission_basis=permission_basis,
                robots_checked=config.respect_robots,
                robots_allowed=robots_allowed,
                application_id=application_id,
                run_id=run_id,
                job_id=job_id,
                worker_id=worker_id,
                lease_version=lease_version,
                collected_at=collected_at,
            )

    async def _save_problem_receipt(
        self,
        *,
        status: str,
        error: str,
        source_key: str,
        requested_url: str,
        canonical_url: str,
        final_url: str,
        host: str,
        permission_basis: str,
        robots_checked: bool,
        robots_allowed: bool | None,
        application_id: str,
        run_id: str,
        job_id: str,
        worker_id: str,
        lease_version: int,
        collected_at: str,
        http_status: int | None = None,
        content_type: str = "",
        content_bytes: int = 0,
    ) -> dict[str, Any]:
        now = utc_now()
        receipt = CollectionReceipt(
            id=self._receipt_id(job_id, source_key),
            job_id=job_id,
            application_id=application_id,
            run_id=run_id,
            source_key=source_key,
            requested_url=requested_url,
            final_url=final_url,
            canonical_url=canonical_url,
            host=host,
            permission_basis=permission_basis,
            robots_checked=robots_checked,
            robots_allowed=robots_allowed,
            status=status,
            http_status=http_status,
            content_type=content_type,
            content_bytes=content_bytes,
            transformation={"extractor": "none", "reason": status},
            error=error,
            collected_at=collected_at,
            created_at=now,
            updated_at=now,
        )
        saved = await self.jobs.save_receipt(
            receipt,
            worker_id=worker_id,
            lease_version=lease_version,
        )
        return saved.model_dump(mode="json")

    @staticmethod
    def render_digest(config: CollectionDigestConfig, collection: Any, topic: Any) -> dict[str, Any]:
        if not isinstance(collection, dict):
            raise TypeError("collection_digest requires an object result")
        items = collection.get("items", [])
        receipts = collection.get("receipts", [])
        if not isinstance(items, list) or not isinstance(receipts, list):
            raise TypeError("collection_digest requires items and receipts arrays")
        selected = [
            item
            for item in items
            if config.include_unchanged or item.get("status") != "unchanged"
        ][: config.max_items]
        lines = [f"# {str(topic or 'Daily collection')}", ""]
        if selected:
            for item in selected:
                title = str(item.get("title") or item.get("url") or "Collected source")
                url = str(item.get("url") or "")
                status = str(item.get("status") or "collected")
                excerpt = str(item.get("excerpt") or "").strip()
                lines.append(f"## {title}")
                lines.append(f"- Status: `{status}`")
                if url:
                    lines.append(f"- Source: [{url}]({url})")
                if excerpt:
                    lines.append("")
                    lines.append(excerpt)
                lines.append("")
        else:
            lines.extend(["No new or changed source content was collected.", ""])
        counts = collection.get("counts", {})
        lines.extend(["## Collection record", ""])
        if isinstance(counts, dict) and counts:
            for key in sorted(counts):
                lines.append(f"- {key}: {counts[key]}")
        else:
            lines.append("- No source receipts")
        denied = [item for item in receipts if item.get("status") in {"denied", "failed", "oversized"}]
        if denied:
            lines.extend(["", "## Sources requiring attention", ""])
            for item in denied:
                lines.append(
                    f"- `{item.get('status')}` {item.get('requested_url')}: {item.get('error') or 'No detail'}"
                )
        lines.extend(
            [
                "",
                "> Scope: controlled allowlisted-source collection. This result does not grant permission to collect arbitrary sites or prove external notification delivery.",
            ]
        )
        return {
            "text": "\n".join(lines).strip(),
            "summary": {
                "topic": str(topic),
                "item_count": len(selected),
                "receipt_count": len(receipts),
                "attention_count": len(denied),
                "counts": counts,
            },
        }

    @staticmethod
    def _source(value: Any, default_permission_basis: str) -> dict[str, str]:
        if isinstance(value, str):
            url = value.strip()
            permission_basis = default_permission_basis
        elif isinstance(value, dict):
            url = str(value.get("url") or "").strip()
            permission_basis = str(
                value.get("permission_basis") or default_permission_basis
            ).strip()
        else:
            raise TypeError("each web_collection source must be a URL string or object")
        if not url:
            raise ValueError("web_collection source URL cannot be empty")
        if not permission_basis:
            raise ValueError("web_collection source permission basis cannot be empty")
        return {"url": url, "permission_basis": permission_basis}

    @staticmethod
    def _url_denial(parsed: Any, host: str, allowed_hosts: set[str]) -> str:
        if parsed.scheme not in {"http", "https"} or not host:
            return "source must use an http or https URL with a hostname"
        if parsed.username or parsed.password:
            return "credentials embedded in a source URL are not allowed"
        if host not in allowed_hosts:
            return f"source host is not declared in allowed_hosts: {host}"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return ""
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            return f"source address class is not allowed: {host}"
        if address.is_private and not address.is_loopback:
            return f"private network source requires the later Connector boundary: {host}"
        return ""

    @staticmethod
    async def _robots_allowed(
        client: httpx.AsyncClient,
        url: str,
        user_agent: str,
        cache: dict[str, tuple[str, str]],
    ) -> tuple[bool | None, str]:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cache_key = f"{origin}|{user_agent}"
        cached = cache.get(cache_key)
        if cached is not None:
            state, value = cached
            if state == "allow_all":
                return True, ""
            if state == "error":
                return None, value
            parser = RobotFileParser()
            parser.parse(value.splitlines())
            return parser.can_fetch(user_agent, url), ""
        robots_url = f"{origin}/robots.txt"
        try:
            response = await client.get(robots_url)
            if (response.url.host or "").casefold() != (parsed.hostname or "").casefold():
                raise RuntimeError("robots.txt redirect left the declared source origin")
            if response.status_code == 404:
                cache[cache_key] = ("allow_all", "")
                return True, ""
            response.raise_for_status()
            text = response.text
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            cache[cache_key] = ("rules", text)
            return parser.can_fetch(user_agent, url), ""
        except Exception as error:
            message = f"robots.txt check failed: {error}"
            cache[cache_key] = ("error", message)
            return None, message

    @staticmethod
    def _extract(response: httpx.Response) -> tuple[str, str]:
        content_type = response.headers.get("content-type", "").casefold()
        if "html" not in content_type:
            text = response.text
            return "", text
        parser = _ReadableHTML()
        parser.feed(response.text)
        return parser.result()

    @staticmethod
    def _canonical_url(value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
        netloc = host
        if port and not (
            (parsed.scheme == "http" and port == 80)
            or (parsed.scheme == "https" and port == 443)
        ):
            netloc = f"{host}:{port}"
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        path = parsed.path or "/"
        return urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))

    @staticmethod
    def _receipt_id(job_id: str, source_key: str) -> str:
        return "receipt-" + hashlib.sha256(f"{job_id}|{source_key}".encode()).hexdigest()[:32]
