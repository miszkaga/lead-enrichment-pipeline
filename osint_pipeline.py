import html
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

from google import genai
from google.genai import types
from tavily import TavilyClient

LOGGER = logging.getLogger("osint_pipeline")

_TAVILY_CLIENT: TavilyClient | None = None
_GEMINI_CLIENT: genai.Client | None = None
_GEMINI_LOCK = threading.Lock()
_LAST_GEMINI_CALL_TS = 0.0

COMMON_CONTACT_PATHS = [
    "kontakt",
    "contact",
    "o-nas",
    "about",
    "kontakt-2",
    "kontakt.html",
    "kontakt.php",
]


def _get_tavily_client() -> TavilyClient:
    global _TAVILY_CLIENT
    if _TAVILY_CLIENT is None:
        _TAVILY_CLIENT = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY"))
    return _TAVILY_CLIENT


def _get_gemini_client() -> genai.Client:
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        _GEMINI_CLIENT = genai.Client()
    return _GEMINI_CLIENT


def build_search_queries(company_name: str) -> list[str]:
    base = company_name.strip()
    if not base:
        return []
    return [
        f'"{base}" NIP KRS zarząd kontakt email produkcja branża',
        f'"{base}" strona www kontakt',
        f'"{base}" właściciel prezes KRS',
        f'"{base}" email telefon',
    ]


def normalize_domain(domain_or_url: str | None) -> str:
    if not domain_or_url:
        return ""
    value = str(domain_or_url).strip().lower()
    if value in {"", "brak", "none", "nan", "błąd", "blad"}:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc or parsed.path
    host = host.split("/")[0].strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def extract_domain_from_url(url: str | None) -> str:
    return normalize_domain(url)


def infer_domain_from_results(results: list[dict[str, Any]]) -> str:
    for item in results:
        domain = extract_domain_from_url(item.get("url", ""))
        if domain and not domain.endswith(("gov.pl", "gov", "wikipedia.org")):
            return domain
    for item in results:
        domain = extract_domain_from_url(item.get("url", ""))
        if domain:
            return domain
    return ""


def guess_domain_from_company(company_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", company_name.lower())
    if len(slug) < 4:
        return ""
    return f"{slug}.pl"


def tavily_search(
    query: str,
    depth: str = "basic",
    max_results: int = 5,
    include_raw_content: bool = False,
    retries: int = 2,
    backoff: float = 2.0,
) -> dict[str, Any]:
    client = _get_tavily_client()
    payload = {
        "query": query,
        "search_depth": depth,
        "max_results": max_results,
        "include_raw_content": include_raw_content,
    }

    for attempt in range(retries + 1):
        try:
            response = client.search(**payload)
            results = response.get("results", []) if isinstance(response, dict) else []
            if not results:
                LOGGER.info("[Tavily:EMPTY] query='%s'", query)
            return {"results": results, "error": None}
        except Exception as exc:  # noqa: BLE001
            is_last_attempt = attempt >= retries
            LOGGER.warning(
                "[Tavily:ERROR] query='%s' attempt=%s/%s error=%s",
                query,
                attempt + 1,
                retries + 1,
                exc,
            )
            if is_last_attempt:
                return {"results": [], "error": str(exc)}
            time.sleep(backoff * (attempt + 1) + random.uniform(0, 0.7))

    return {"results": [], "error": "unknown_error"}


def _decode_cloudflare_email(hex_data: str) -> str:
    try:
        data = bytes.fromhex(hex_data)
        key = data[0]
        return "".join(chr(byte ^ key) for byte in data[1:])
    except ValueError:
        return ""


def _normalize_obfuscated_text(text: str) -> str:
    normalized = html.unescape(text or "")

    for m in re.finditer(r'data-cfemail=["\']([0-9a-fA-F]{6,})["\']', normalized):
        decoded = _decode_cloudflare_email(m.group(1))
        if decoded:
            normalized += f" {decoded} "

    substitutions = [
        (r"\s*\[\s*at\s*\]\s*", "@"),
        (r"\s*\(\s*at\s*\)\s*", "@"),
        (r"\s+at\s+", "@"),
        (r"\s*\[\s*dot\s*\]\s*", "."),
        (r"\s*\(\s*dot\s*\)\s*", "."),
        (r"\s+dot\s+", "."),
    ]
    for pattern, repl in substitutions:
        normalized = re.sub(pattern, repl, normalized, flags=re.IGNORECASE)

    return normalized


def extract_emails_phones(text: str) -> dict[str, list[str]]:
    normalized = _normalize_obfuscated_text(text)
    email_pattern = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
    phone_pattern = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")

    invalid_email_tokens = {
        "example.com",
        "w3.org",
        "sentry.io",
        "cloudflare.com",
        "email.com",
    }

    emails: list[str] = []
    for candidate in email_pattern.findall(normalized):
        value = candidate.strip(".,;:()[]{}<>").lower()
        if any(token in value for token in invalid_email_tokens):
            continue
        if value.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")):
            continue
        if value not in emails:
            emails.append(value)

    phones: list[str] = []
    for candidate in phone_pattern.findall(normalized):
        digits = re.sub(r"\D", "", candidate)
        if len(digits) < 9 or len(digits) > 15:
            continue
        formatted = candidate.strip()
        if formatted not in phones:
            phones.append(formatted)

    return {"emails": emails, "phones": phones}


def fetch_contact_raw_content(domain: str, company_name: str | None = None) -> str:
    clean_domain = normalize_domain(domain)
    if not clean_domain:
        return ""

    queries = [
        f"site:{clean_domain} kontakt",
        f"site:{clean_domain} contact",
        f"site:{clean_domain} o nas",
        f"site:{clean_domain} about",
        f"site:{clean_domain} email telefon",
        f"site:{clean_domain} \"@\"",
    ]

    if company_name and company_name.strip():
        queries.append(f'"{company_name.strip()}" site:{clean_domain} kontakt')

    merged_chunks: list[str] = []
    seen_urls: set[str] = set()

    for query in queries:
        response = tavily_search(
            query=query,
            depth="advanced",
            max_results=5,
            include_raw_content=True,
            retries=2,
            backoff=2.0,
        )
        for item in response.get("results", []):
            url = item.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            raw = item.get("raw_content", "")
            content = item.get("content", "")
            title = item.get("title", "")
            if raw:
                merged_chunks.append(raw)
            if content:
                merged_chunks.append(content)
            if title:
                merged_chunks.append(title)

    return "\n\n".join(chunk for chunk in merged_chunks if chunk)


def safe_json_loads(text: str | None) -> Any:
    if text is None:
        raise ValueError("empty response")

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    starts = [idx for idx in (cleaned.find("{"), cleaned.find("[")) if idx != -1]
    if not starts:
        raise ValueError("no json object/array found")
    start = min(starts)

    stack: list[str] = []
    in_string = False
    escaped = False

    for idx, char in enumerate(cleaned[start:], start=start):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack:
                continue
            open_char = stack[-1]
            if (open_char == "{" and char == "}") or (open_char == "[" and char == "]"):
                stack.pop()
            if not stack:
                candidate = cleaned[start : idx + 1]
                return json.loads(candidate)

    raise ValueError("json parsing failed")


def gemini_generate_json(
    payload_text: str,
    schema: types.Schema,
    system_instruction: str,
    model: str = "gemini-2.5-flash",
    retries: int = 3,
    backoff: float = 3.0,
    min_interval_seconds: float = 1.0,
) -> Any:
    client = _get_gemini_client()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=schema,
    )

    global _LAST_GEMINI_CALL_TS

    for attempt in range(retries + 1):
        try:
            with _GEMINI_LOCK:
                now = time.time()
                wait_time = max(0.0, min_interval_seconds - (now - _LAST_GEMINI_CALL_TS))
                if wait_time > 0:
                    time.sleep(wait_time)
                response = client.models.generate_content(model=model, contents=payload_text, config=config)
                _LAST_GEMINI_CALL_TS = time.time()

            return safe_json_loads(getattr(response, "text", ""))
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            is_quota = "429" in message or "quota" in message or "exhausted" in message
            is_parse = "json" in message or "parse" in message

            if is_quota:
                LOGGER.warning(
                    "[Gemini:429/QUOTA] attempt=%s/%s error=%s",
                    attempt + 1,
                    retries + 1,
                    exc,
                )
            elif is_parse:
                LOGGER.warning(
                    "[Gemini:JSON_PARSE] attempt=%s/%s error=%s",
                    attempt + 1,
                    retries + 1,
                    exc,
                )
            else:
                LOGGER.error(
                    "[Gemini:ERROR] attempt=%s/%s error=%s",
                    attempt + 1,
                    retries + 1,
                    exc,
                )

            if attempt >= retries:
                raise
            time.sleep(backoff * (attempt + 1) + random.uniform(0, 0.9))

    raise RuntimeError("Gemini retries exhausted")
