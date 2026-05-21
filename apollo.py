import os
import re
import time
import logging
import requests
import pandas as pd
from dotenv import load_dotenv
from google import genai

# Konfiguracja środowiska
load_dotenv()
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

FILES = ["GUNB_Enriched_Scored_Leads.xlsx", "OLX_Enriched_Scored_Leads.xlsx"]

client = genai.Client(api_key=GEMINI_API_KEY)
HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-cache", "x-api-key": APOLLO_API_KEY}

# Endpointy
SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search" # Darmowy - znajduje ludzi, bez maili
MATCH_URL = "https://api.apollo.io/v1/people/match"         # Płatny - odkrywa maile

def clean_company_name(name):
    if not isinstance(name, str) or pd.isna(name): return ""
    suffixes = [
        r"SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ", r"SPÓŁKA KOMANDYTOWA", r"SPÓŁKA AKCYJNA",
        r"PROSTA SPÓŁKA AKCYJNA", r"SPÓŁKA PARTNERSKA", r"SPÓŁKA JAWNA",
        r"SP\. Z O\.O\.", r"SP\. Z O O", r"SP\.Z O\.O\.", r"SP Z O\.O\.",
        r"SP\.K\.", r"SP\. K\.", r"S\.A\.", r"P\.S\.A\.", r"SP\.P\.", r"SP\.J\.",
        r"P\.P\.H\.U\.", r"PHU"
    ]
    pattern = r"\b(" + "|".join(suffixes) + r")\b"
    clean = re.sub(pattern, "", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", clean).strip(" ,.-")

def verify_match_with_gemini(apollo_data, clean_company):
    p_company = apollo_data.get('organization', {}).get('name', 'Brak')
    p_title = apollo_data.get('title', 'Brak')
    prompt = f"Firma docelowa: {clean_company}\nZnaleziono na LinkedIn: {p_company}, {p_title}\nCzy to ta sama firma? Zwróć TYLKO: TAK lub NIE."
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return "TAK" in response.text.upper()
    except:
        return False

def search_and_enrich_apollo(domain, company_name, person_name):
    """Nowa architektura zgodna z dokumentacją: Match -> Search -> Enrich"""
    clean_name = clean_company_name(company_name)
    p_name = str(person_name).strip() if pd.notnull(person_name) else ""
    if p_name.lower() in ["brak", "brak danych", "none", "nan", ""]: p_name = ""
    dom = str(domain).strip() if pd.notnull(domain) else ""
    if dom.lower() in ["brak", "brak danych", "none", "nan", ""]: dom = ""

    # KROK 1: Jeśli mamy imię i domenę - uderzamy po maila (Match Endpoint)
    if p_name and dom:
        match_payload = {
            "name": p_name,
            "domain": dom,
            "organization_name": clean_name
        }
        try:
            res = requests.post(MATCH_URL, headers=HEADERS, json=match_payload)
            if res.status_code == 200 and res.json().get("person"):
                person = res.json()["person"]
                if person.get("email"):
                    logging.info(f"🎯 Odkryto e-mail z KRS (Match API) dla {clean_name}")
                    return person
        except Exception as e:
            logging.error(f"Błąd Match API: {e}")

    # KROK 2: Nie mamy imienia lub Krok 1 zawiódł? Używamy Search by znaleźć prezesa po tytule
    if dom:
        search_payload = {
            "q_organization_domains": [dom],
            "person_titles": ["Prezes", "CEO", "President", "Właściciel", "Owner", "Dyrektor", "Director", "Founder"],
            "page": 1, "per_page": 1
        }
        try:
            res = requests.post(SEARCH_URL, headers=HEADERS, json=search_payload)
            if res.status_code == 200:
                people = res.json().get("people", [])
                if people:
                    candidate = people[0]
                    # Weryfikacja
                    if verify_match_with_gemini(candidate, clean_name):
                        person_id = candidate.get("id")

                        # KROK 3: Mamy człowieka! Odkrywamy jego e-mail po ID (Enrichment)
                        if person_id:
                            enrich_payload = {"id": person_id}
                            enrich_res = requests.post(MATCH_URL, headers=HEADERS, json=enrich_payload)
                            if enrich_res.status_code == 200 and enrich_res.json().get("person"):
                                final_person = enrich_res.json()["person"]
                                logging.info(f"🎯 Znaleziono po stanowisku i odkryto e-mail dla {clean_name}")
                                return final_person
                            else:
                                # Znalazł profil, ale Apollo nie ma maila w bazie
                                return candidate
        except Exception as e:
            logging.error(f"Błąd Search API: {e}")

    return None

def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    for file_name in FILES:
        if not os.path.exists(file_name): continue

        print(f"\n--- Apollo Data Unlocker dla: {file_name} ---")
        df = pd.read_excel(file_name)
        df = df.astype(object).where(pd.notnull(df), None)
        final_results = []

        for idx, row in df.iterrows():
            company = row.get("Nazwa Firmy", "Nieznana firma")

            if row.get("Status") == "ODRZUCONY" or (row.get("Potencjał (1-10)") or 0) < 5:
                continue

            row_out = row.to_dict()

            if row.get("Email_Status") == "PERSONAL":
                logging.info(f"[{company}] Mamy personalny mail, oszczędzamy kredyty.")
                row_out["Final_Email"] = row.get("Email")
                row_out["Final_Source"] = "Tavily (Personal)"
                row_out["Apollo_Name"] = "N/A"
                row_out["LinkedIn_URL"] = "N/A"
            else:
                logging.info(f"[{company}] Polowanie na odblokowany email...")
                apollo_data = search_and_enrich_apollo(row.get("Domena WWW"), company, row.get("Zarząd / Właściciel"))

                if apollo_data:
                    apollo_email = apollo_data.get("email")
                    apollo_name = f"{apollo_data.get('first_name', '')} {apollo_data.get('last_name', '')}".strip()
                    apollo_linkedin = apollo_data.get("linkedin_url", "")

                    if apollo_email:
                        row_out["Final_Email"] = apollo_email
                        row_out["Final_Source"] = "Apollo.io (Enriched)"
                    else:
                        if row.get("Email_Status") == "GENERIC":
                            row_out["Final_Email"] = row.get("Email")
                            row_out["Final_Source"] = "Tavily (Generic) + Profil z Apollo"
                        else:
                            row_out["Final_Email"] = "LINKEDIN_ONLY"
                            row_out["Final_Source"] = "Apollo (Brak e-maila w bazie płatnej)"

                    row_out["Apollo_Name"] = apollo_name
                    row_out["LinkedIn_URL"] = apollo_linkedin
                else:
                    if row.get("Email_Status") == "GENERIC":
                        row_out["Final_Email"] = row.get("Email")
                        row_out["Final_Source"] = "Tavily (Generic)"
                    else:
                        row_out["Final_Email"] = "BRAK"
                        row_out["Final_Source"] = "None"

                    row_out["Apollo_Name"] = "N/A"
                    row_out["LinkedIn_URL"] = "N/A"

            final_results.append(row_out)
            time.sleep(0.5)

        output_name = f"Final_Campaign_{file_name}"
        pd.DataFrame(final_results).to_excel(output_name, index=False)
        print(f"✅ Zapisano plik z ODZYSKANYMI MAILAMI: {output_name}")

if __name__ == "__main__":
    main()