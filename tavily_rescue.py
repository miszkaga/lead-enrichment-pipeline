import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from google.genai import types

from osint_pipeline import (
    COMMON_CONTACT_PATHS,
    extract_emails_phones,
    fetch_contact_raw_content,
    gemini_generate_json,
    normalize_domain,
)

load_dotenv()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

if not GEMINI_API_KEY or not TAVILY_API_KEY:
    raise ValueError("Brak kluczy API w pliku .env")

FILES = ["GUNB_Enriched_Scored_Leads.xlsx", "OLX_Enriched_Scored_Leads.xlsx"]
HTTP_TIMEOUT_SECONDS = int(os.environ.get("RESCUE_HTTP_TIMEOUT", "8"))
MIN_RAW_CONTENT_CHARS = int(os.environ.get("RESCUE_MIN_RAW_CONTENT", "500"))
GEMINI_MIN_INTERVAL_SECONDS = float(os.environ.get("GEMINI_MIN_INTERVAL_SECONDS", "1.0"))


def fetch_http_contact_content(domain: str) -> str:
    clean_domain = normalize_domain(domain)
    if not clean_domain:
        return ""

    urls = [f"https://{clean_domain}"] + [f"https://{clean_domain}/{path}" for path in COMMON_CONTACT_PATHS]
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; zielone-osint/1.0; +https://github.com/miszkaga/zielone-/)"
    }

    chunks: list[str] = []
    with requests.Session() as session:
        for url in urls:
            try:
                response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS, headers=headers, allow_redirects=True)
                if response.status_code < 400 and response.text:
                    chunks.append(response.text)
            except requests.RequestException as exc:
                print(f"  [HTTP ERROR] {url} -> {exc}")

    return "\n\n".join(chunks)


def get_raw_html(domain: str, company_name: str) -> tuple[str, str]:
    raw_html = fetch_contact_raw_content(domain, company_name=company_name)
    if raw_html:
        print("  [TAVILY RAW] Pobrano dane raw_content.")
    else:
        print("  [TAVILY EMPTY] Brak raw_content z Tavily.")

    if len(raw_html) >= MIN_RAW_CONTENT_CHARS:
        return raw_html, "tavily_raw"

    http_content = fetch_http_contact_content(domain)
    if http_content:
        merged = f"{raw_html}\n\n{http_content}".strip()
        print("  [HTTP FALLBACK] Dołączono lekkie pobieranie HTTP stron kontaktowych.")
        return merged, "tavily_raw+http"

    return raw_html, "tavily_raw"


def extract_contact_data(raw_html: str, company_name: str) -> dict:
    if not raw_html or len(raw_html) < 100:
        return {"email": "BRAK", "phone": "BRAK", "method": "NoContent"}

    regex_data = extract_emails_phones(raw_html)
    if regex_data["emails"] or regex_data["phones"]:
        return {
            "email": regex_data["emails"][0] if regex_data["emails"] else "BRAK",
            "phone": regex_data["phones"][0] if regex_data["phones"] else "BRAK",
            "method": "Regex",
        }

    prompt = (
        "Zidentyfikuj publicznie dostępny firmowy e-mail i telefon z podanego tekstu HTML/treści strony. "
        "Ignoruj prywatne dane i brakujące informacje.\n\n"
        f"Firma: {company_name}\n\n"
        f"Treść:\n{raw_html[:40000]}"
    )

    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "email": types.Schema(type=types.Type.STRING),
            "phone": types.Schema(type=types.Type.STRING),
        },
        required=["email", "phone"],
    )

    try:
        data = gemini_generate_json(
            payload_text=prompt,
            schema=schema,
            system_instruction="Zwracaj wyłącznie JSON. Jeśli brak danych, zwróć 'BRAK'.",
            model="gemini-2.5-flash",
            retries=2,
            backoff=3.0,
            min_interval_seconds=GEMINI_MIN_INTERVAL_SECONDS,
        )
        if isinstance(data, dict):
            data["method"] = "Gemini AI"
            return data
    except Exception as exc:  # noqa: BLE001
        print(f"  [GEMINI ERROR] {exc}")

    return {"email": "BRAK", "phone": "BRAK", "method": "AI Error"}


def main():
    for file_name in FILES:
        if not os.path.exists(file_name):
            continue

        print(f"\n--- URUCHAMIAM SKRYPT RATUNKOWY DLA: {file_name} ---")
        df = pd.read_excel(file_name)

        email_col = "Final_Email" if "Final_Email" in df.columns else "Email"
        mask = df[email_col].astype(str).str.upper().str.contains("BRAK|NONE|NAN|CHRONIONE|SPAM", na=True)
        mask = mask & (df["Domena WWW"].notnull()) & (df["Domena WWW"].astype(str).str.lower() != "brak")

        indices_to_fix = df[mask].index
        print(f"Znaleziono {len(indices_to_fix)} firm do uratowania.")

        for idx in indices_to_fix:
            company = df.at[idx, "Nazwa Firmy"]
            domain = df.at[idx, "Domena WWW"]

            print(f"[{company}] Skanowanie: {domain}...")

            raw_html, source = get_raw_html(domain, company)
            result = extract_contact_data(raw_html, company)

            found_email = result.get("email", "BRAK")
            found_phone = result.get("phone", "BRAK")
            method = result.get("method", "Unknown")

            if found_email and str(found_email).upper() != "BRAK":
                print(f"  🎯 SUKCES! Odzyskano mail: {found_email} (przez: {method})")
                df.at[idx, email_col] = found_email
                df.at[idx, "Final_Source"] = f"Tavily Rescue ({source}/{method})"
            else:
                print("  ❌ Brak maila (kod nie zawiera adresu).")

            if "Phone" in df.columns and found_phone and str(found_phone).upper() != "BRAK":
                df.at[idx, "Phone"] = found_phone

            time.sleep(1)

        df.to_excel(file_name, index=False)
        print(f"✅ Zapisano zmiany w {file_name}")


if __name__ == "__main__":
    main()
