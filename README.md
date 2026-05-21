# zielone-

## Tavily/Gemini verification flow

- Verifiers (`4_verifier_tavily_olx.py`, `verifier_tavily_gunb.py`, `verifier_failed.py`) now use shared helpers from `osint_pipeline.py`.
- Flow:
  1. Run multiple Tavily snippet queries for company context.
  2. Infer domain from Tavily URLs.
  3. Extract emails/phones via regex from snippets.
  4. If data is missing, fetch on-site Tavily `include_raw_content` (`kontakt/contact/about/@`) and re-extract.
  5. Send Gemini structured context (domain + regex results) and parse with resilient JSON loader.

## Knobs (env vars)

- `MAX_WORKERS` (default `5`) — parallel workers in `verifier_tavily_gunb.py`
- `TAVILY_SEARCH_DEPTH` (default `basic`)
- `TAVILY_MAX_RESULTS` (default `5`)
- `GEMINI_MIN_INTERVAL_SECONDS` (default `1.0` or `1.2` in GUNB verifier) — simple rate limiting between Gemini calls
- `RESCUE_HTTP_TIMEOUT` (default `8`) and `RESCUE_MIN_RAW_CONTENT` (default `500`) for `tavily_rescue.py`
