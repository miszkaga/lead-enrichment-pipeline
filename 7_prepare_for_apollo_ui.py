import os
import re
import pandas as pd

# Używamy dokładnie Twoich czystych baz wyjściowych
FILES = ["GUNB_Enriched_Scored_Leads.xlsx", "OLX_Enriched_Scored_Leads.xlsx"]

def clean_company_name(name):
    """Uproszczona nazwa handlowa firmy dla lepszego dopasowania w bazie Apollo."""
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

def prepare_files():
    for file_name in FILES:
        if not os.path.exists(file_name):
            print(f"❌ Pominięto {file_name} - plik nie istnieje w tym katalogu.")
            continue

        print(f"Przetwarzanie pliku: {file_name}...")
        df = pd.read_excel(file_name)

        # Unifikacja nazw kolumn mailowych (sprawdzamy Final_Email lub Email)
        email_col = 'Final_Email' if 'Final_Email' in df.columns else 'Email'

        if email_col not in df.columns:
            print(f"⚠️ W pliku {file_name} nie znaleziono kolumny z mailami. Przetwarzam wszystkie wiersze.")
            df_missing = df.copy()
        else:
            # Wyciągamy tylko wiersze, gdzie nie ma jeszcze maila (wartość 'BRAK' lub pusta komórka)
            df_missing = df[df[email_col].astype(str).str.upper().str.contains('BRAK|NONE|NAN', na=True)].copy()

        if df_missing.empty:
            print(f"✨ Brak firm bez maila w pliku {file_name}. Wszystkie są już zaopiekowane!")
            continue

        export_data = []

        for idx, row in df_missing.iterrows():
            # Szukamy kolumny z osobą decyzyjną
            raw_person = row.get("Zarząd / Właściciel") or row.get("Zarząd") or ""
            raw_person = str(raw_person).strip()

            if raw_person.lower() in ["brak", "brak danych", "none", "nan", "", "brak danych w dostarczonych wynikach"]:
                raw_person = ""

            # Czyszczenie nawiasów np. Radosław Groth (Właściciel) -> Radosław Groth
            raw_person = re.sub(r"\(.*\)", "", raw_person).strip()

            # Jeśli jest kilku prezesów po przecinku, bierzemy pierwszego do weryfikacji w Apollo
            first_person = raw_person.split(",")[0].strip() if "," in raw_person else raw_person
            name_parts = first_person.split()

            first_name = name_parts[0] if name_parts else ""
            last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

            # Pobieranie i czyszczenie domeny
            domain = str(row.get("Domena WWW", "")).strip()
            if domain.lower() in ["brak", "brak danych", "none", "nan", ""]:
                domain = ""
            domain = re.sub(r"^www\.", "", domain, flags=re.IGNORECASE)

            # Budujemy czysty wiersz, który Apollo zrozumie bezbłędnie
            export_data.append({
                "First Name": first_name,
                "Last Name": last_name,
                "Company Domain": domain,
                "Company Name": clean_company_name(row.get("Nazwa Firmy")),
                "Original Company Name": row.get("Nazwa Firmy") # Zostawiamy dla Twojej późniejszej kontroli
            })

        # Zapis do CSV z obsługą polskich znaków dla Excela (utf-8-sig)
        df_out = pd.DataFrame(export_data)
        output_csv_name = f"Apollo_Import_{file_name.replace('.xlsx', '')}.csv"
        df_out.to_csv(output_csv_name, index=False, encoding='utf-8-sig')
        print(f"✅ Wygenerowano czysty plik importu: {output_csv_name} ({len(df_out)} firm do wzbogacenia)")

if __name__ == "__main__":
    prepare_files()