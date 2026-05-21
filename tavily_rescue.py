import os
import re
import time
import requests
import pandas as pd
from dotenv import load_dotenv
from google import genai

load_dotenv()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
FILES = ["GUNB_Enriched_Scored_Leads.xlsx", "OLX_Enriched_Scored_Leads.xlsx"]

def get_raw_html_via_tavily(domain):
    """Pobiera kod używając DOKŁADNIE tego zapytania, które zadziałało w teście diagnostycznym."""
    if not domain or str(domain).lower() in ["brak", "none", "nan", ""]:
        return ""

    url = f"https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": f"site:{domain} kontakt", # Usunięte "email telefon", żeby nie psuć wyników wyszukiwania!
        "include_raw_content": True,
        "max_results": 2
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            results = res.json().get("results", [])
            return " ".join([r.get("raw_content", "") for r in results if r.get("raw_content")])
    except Exception as e:
        print(f"Błąd Tavily dla {domain}: {e}")
    return ""

def extract_contact_data(raw_html, company_name):
    """Krok 1: Twardy Regex (nieomylny). Krok 2: Gemini (fallback)."""
    if not raw_html or len(raw_html) < 100:
        return {"email": "BRAK", "phone": "BRAK"}

    # --- KROK 1: BRUTALNA SIŁA (RegEx) ---
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    found_emails = re.findall(email_pattern, raw_html)

    # Odfiltrowujemy śmieci technologiczne
    valid_emails = []
    for e in found_emails:
        e_lower = e.lower()
        if not e_lower.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')) and \
                "w3.org" not in e_lower and "sentry" not in e_lower and "example.com" not in e_lower:
            valid_emails.append(e)

    if valid_emails:
        best_email = valid_emails[0]
        phone_pattern = r'(?:\+48[\s-]?)?(?:\d{3}[\s-]?\d{3}[\s-]?\d{3}|\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2})'
        found_phones = re.findall(phone_pattern, raw_html)
        # Bierzemy pierwszy lepszy numer, który wygląda jak telefon
        best_phone = [p for p in found_phones if len(re.sub(r'\D', '', p)) >= 9]
        best_phone = best_phone[0] if best_phone else "BRAK"

        return {"email": best_email, "phone": best_phone, "method": "Regex"}

    # --- KROK 2: SZTUCZNA INTELIGENCJA (Fallback) ---
    raw_html_truncated = raw_html[:40000]
    prompt = f"""
    Zignoruj komunikaty typu "adres chroniony przed spamowaniem". 
    Znajdź zdekodowany adres e-mail i telefon w kodzie źródłowym firmy {company_name}.
    Kod:
    {raw_html_truncated}
    
    Zwróć wynik WYŁĄCZNIE jako JSON:
    {{
        "email": "znaleziony_email_lub_BRAK",
        "phone": "znaleziony_telefon_lub_BRAK"
    }}
    """
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        text_clean = response.text.strip().replace("```json", "").replace("```", "").strip()
        import json
        data = json.loads(text_clean)
        if isinstance(data, str): return {"email": "BRAK", "phone": "BRAK", "method": "AI Failed"}
        data["method"] = "Gemini AI"
        return data
    except:
        return {"email": "BRAK", "phone": "BRAK", "method": "AI Error"}

def main():
    for file_name in FILES:
        if not os.path.exists(file_name):
            continue

        print(f"\n--- URUCHAMIAM SKRYPT RATUNKOWY DLA: {file_name} ---")
        df = pd.read_excel(file_name)

        email_col = 'Final_Email' if 'Final_Email' in df.columns else 'Email'
        mask = df[email_col].astype(str).str.upper().str.contains('BRAK|NONE|NAN|CHRONIONE|SPAM', na=True)
        mask = mask & (df['Domena WWW'].notnull()) & (df['Domena WWW'].astype(str).str.lower() != 'brak')

        indices_to_fix = df[mask].index
        print(f"Znaleziono {len(indices_to_fix)} firm do uratowania.")

        for idx in indices_to_fix:
            company = df.at[idx, 'Nazwa Firmy']
            domain = df.at[idx, 'Domena WWW']

            print(f"[{company}] Skanowanie: {domain}...")

            raw_html = get_raw_html_via_tavily(domain)
            result = extract_contact_data(raw_html, company)

            found_email = result.get("email", "BRAK")
            method = result.get("method", "Unknown")

            if found_email and found_email.upper() != "BRAK":
                print(f"  🎯 SUKCES! Odzyskano mail: {found_email} (przez: {method})")
                df.at[idx, email_col] = found_email
                df.at[idx, 'Final_Source'] = f"Tavily Rescue ({method})"
            else:
                print(f"  ❌ Brak maila (kod nie zawiera adresu).")

            time.sleep(1)

        df.to_excel(file_name, index=False)
        print(f"✅ Zapisano zmiany w {file_name}")

if __name__ == "__main__":
    main()