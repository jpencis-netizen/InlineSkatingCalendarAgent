import urllib.request
import urllib.error
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from google import genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
import re
import time
from datetime import datetime, timedelta
import json
import os
import base64

# --- CONFIGURATION ---
RUN_CLEANUP = os.getenv('RUN_CLEANUP', 'false').lower() == 'true'
RUN_MIGRATION = os.getenv('RUN_MIGRATION', 'false').lower() == 'true'

# --- 1. SETUP & AUTHENTICATION ---
print("Autentifikācija...")

SERVICE_ACCOUNT_JSON_B64 = os.getenv('SERVICE_ACCOUNT_JSON_B64')
if SERVICE_ACCOUNT_JSON_B64:
    service_account_json = base64.b64decode(SERVICE_ACCOUNT_JSON_B64).decode('utf-8')
    service_account_dict = json.loads(service_account_json)
else:
    SERVICE_ACCOUNT_FILE = 'service_account.json'
    with open(SERVICE_ACCOUNT_FILE, 'r') as f:
        service_account_dict = json.load(f)

SCOPES = ['https://www.googleapis.com/auth/calendar']

try:
    creds = service_account.Credentials.from_service_account_info(
        service_account_dict, scopes=SCOPES)
    calendar_service = build('calendar', 'v3', credentials=creds)
    print("✓ Autentifikācija veiksmīga!")
except Exception as e:
    print(f"✗ Autentifikācijas kļūda: {e}")
    exit(1)

# --- GEMINI API SETUP ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    print("✗ GEMINI_API_KEY environment variable not set!")
    exit(1)

ai_client = genai.Client(api_key=GEMINI_API_KEY)
CALENDAR_ID = os.getenv('CALENDAR_ID', 'd51d3fcd4e9f373b3766d9198129f7af868315252002d7c69a9281d359946e51@group.calendar.google.com')

ACTIVE_MODEL = 'gemini-3.1-flash-lite-preview'

# --- 2. AI EVENT EXTRACTION FUNCTION ---
def extract_events_with_ai(original_url, original_title, retries=4):
    global ACTIVE_MODEL
    
    print(f"    > Detektīvs sagatavo saites no: {original_url}")

    # --- 1. SOLIS: Gudrā saišu ģenerēšana ---
    urls_to_try = []
    if '2025' in original_url:
        urls_to_try.append(original_url.replace('2025', '2026'))
    elif '25' in original_url:
        urls_to_try.append(original_url.replace('25', '26'))
    
    if original_url not in urls_to_try:
        urls_to_try.append(original_url) # Vienmēr atstājam oriģinālo saiti kā rezerves variantu

    working_url = None
    html_content = None
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }

    # --- 2. SOLIS: Pārbaudām, kura saite reāli eksistē ---
    for test_url in urls_to_try:
        try:
            print(f"      [?] Pārbauda lapas pieejamību: {test_url}")
            req = urllib.request.Request(test_url, headers=headers)
            response = urllib.request.urlopen(req, timeout=15)
            html_content = response.read()
            working_url = test_url
            print(f"      [V] Lapa ielādēta! Izmantosim šo saiti.")
            break # Līdzko atrod strādājošu saiti, pārtrauc meklēt citas
        except Exception as e:
            # Ja lapa neeksistē (404) vai liedz piekļuvi (403), klusām ejam pie nākamās saites
            print(f"      [-] Saite nav pieejama ({e}).")
            pass 

    # Ja mājaslapa galīgi nestrādā nevienā no variantiem
    if not working_url:
        print(f"      [!!!] Neviena no saitēm nav pieejama vai liedz piekļuvi. Izlaižam.")
        return [], original_url

    # --- 3. SOLIS: Sagatavojam tekstu MI (Tikai vienreiz!) ---
    soup = BeautifulSoup(html_content, 'html.parser')
    content = soup.get_text(separator=' ', strip=True)[:20000]

    prompt = f"""
Objective: Extract 2026 INLINE SPEED SKATING (skrituļslidošana) race dates.
Context: The user is interested in '{original_title}'.

Website content: {content}

STRICT RULES:
1. Extract ONLY inline speed skating events (look for terms like 'Inlineskaten', 'Rulluisutamine', 'Rychlobruslení').
2. STERNLY REJECT all other sports (Volleyball, Beach Volleyball, Tennis, Football, Ice skating, etc.).
3. For each event, return:
   "title": (string),
   "start_date": (YYYY-MM-DD),
   "end_date": (YYYY-MM-DD, if multi-day. Use start_date if single day),
   "location": (string).
4. 14-Day Rule: If the gap between start and end is > 14 days, set end_date = start_date.
5. Return STRICTLY a JSON array of objects. No markdown formatting.
"""

    # --- 4. SOLIS: Sūtām datus MI un apstrādājam kļūdas ---
    for attempt in range(retries):
        try:
            print(f"      [~] Mēģinājums {attempt+1} izmanto modeli: {ACTIVE_MODEL}")
            
            response = ai_client.models.generate_content(
                model=ACTIVE_MODEL,
                contents=prompt
            )

            clean_text = response.text.strip()
            if clean_text.startswith("```"):
                clean_text = clean_text.replace("```json", "").replace("```", "").strip()

            events = json.loads(clean_text)
            if isinstance(events, list):
                # ATGROŽAM TIKAI TO SAITI, KURA REĀLI STRĀDĀJA!
                return events, working_url 
            break
            
        except Exception as e:
            print(f"      [!] Mēģinājums {attempt+1} neizdevās ({ACTIVE_MODEL}): {e}")
            error_msg = str(e)
            
            # Pārslēdzējs paliek neskarts un strādās perfekti
            if ACTIVE_MODEL == 'gemini-3.1-flash-lite-preview':
                if '429' in error_msg or 'Quota' in error_msg or attempt >= 1:
                    print("      [!!!] Gemini limits sasniegts vai serveris atsakās strādāt.")
                    print("      [!!!] Pārslēdzamies uz Gemma 3 visām turpmākajām saitēm!")
                    ACTIVE_MODEL = 'gemma-3-27b-it'
           
            if attempt < retries - 1:
                time.sleep(30)

    return [], working_url

# --- 3. HELPER FUNCTION FOR EVENT PROCESSING & DEDUPLICATION ---
def process_found_events(found_events, source_url, existing_events, original_desc=""):
    for f_event in found_events:
        start_date = f_event.get('start_date')
        end_date = f_event.get('end_date') or start_date
        title = f_event.get('title') or 'Unknown event'
        loc = (f_event.get('location') or '').strip()

        if not start_date or not isinstance(start_date, str) or not start_date.startswith("2026"):
            continue

        # --- 14 DIENU PĀRBAUDE (Pievienots atpakaļ) ---
        try:
            start_dt_check = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt_check = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Aprēķina starpību dienās
            delta_days = (end_dt_check - start_dt_check).days
            
            if delta_days > 14 or delta_days < 0:
                print(f"      [!] Datumu kļūda izlabota ({delta_days} dienas). Beigu datums = sākuma datums.")
                end_date = start_date # Ignorējam kļūdaino beigu datumu
        except ValueError:
            print(f"      [!] Kļūdains datuma formāts: {start_date} vai {end_date}")
            continue # Izlaižam šo notikumu, ja datums ir pilnīgi nesaprotams
        # ---------------------------------------------

        print(f"    -> Apstrādā: {title} ({start_date} - {end_date})")

        # Smart duplicate detection
        is_duplicate = False
        matched_event = None
        base_title_search = re.sub(r'2025|2026|25|26', '', title).strip().lower()
        loc_words = set(re.findall(r'\b\w{4,}\b', loc.lower())) if loc and loc != "NOT_FOUND" else set()

        for ext in existing_events:
            ext_start = ext.get('start', {}).get('date')
            ext_title = ext.get('summary', '').lower()
            ext_loc = ext.get('location', '').lower()

            if ext_start == start_date:
                title_match = base_title_search and base_title_search in ext_title
                loc_match = False
                if loc_words:
                    ext_words = set(re.findall(r'\b\w{4,}\b', ext_loc))
                    if loc_words.intersection(ext_words):
                        loc_match = True

                if title_match or loc_match:
                    is_duplicate = True
                    matched_event = ext
                    break

        if is_duplicate:
            print(f"      [IZLAISTS] Jau ir kalendārā. Atjaunojam saiti...")
            # Pārējā dublikātu loģika paliek nemainīga
            desc = matched_event.get('description', '')
            if source_url not in desc:
                new_desc = desc + f"\n2026 link: {source_url}"
                matched_event['description'] = new_desc
                try:
                    calendar_service.events().update(calendarId=CALENDAR_ID, eventId=matched_event['id'], body=matched_event).execute()
                except Exception as e:
                    print(f"      [!] Kļūda atjaunojot: {e}")
        else:
            # Create new calendar entry with proper end date handling
            try:
                # Šeit mēs izmantojam jau pārbaudīto/izlaboto end_date
                end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                final_end = end_dt.strftime("%Y-%m-%d")

                new_desc = f"---Automātiski atjaunots---\n\n{original_desc}\n\n2026 link: {source_url}"

                body = {
                    'summary': title,
                    'location': loc if loc != "NOT_FOUND" else "",
                    'description': new_desc,
                    'start': {'date': start_date},
                    'end': {'date': final_end}
                }

                new_item = calendar_service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
                existing_events.append(new_item)
                print(f"      [+] PIEVIENOTS: {title}")
            except Exception as e:
                print(f"      [!] Kļūda veidojot ierakstu: {e}")
                
# --- 4. MAIN EXECUTION ---
def run_agent():
    print("Iegūst 2026. gada datus no kalendāra...")
    try:
        events_2026 = calendar_service.events().list(
            calendarId=CALENDAR_ID, timeMin="2026-01-01T00:00:00Z",
            timeMax="2026-12-31T23:59:59Z", singleEvents=True, maxResults=2500
        ).execute().get('items', [])
    except Exception as e:
        print(f"Kļūda iegūstot kalendāru: {e}")
        events_2026 = []

    if RUN_CLEANUP:
        print("Tīrīšana...")
        events_to_keep = []
        for e in events_2026:
            if "---Automātiski atjaunots---" in e.get('description', ''):
                try:
                    calendar_service.events().delete(calendarId=CALENDAR_ID, eventId=e['id']).execute()
                    time.sleep(0.3)
                except:
                    pass
            else:
                events_to_keep.append(e)
        events_2026 = events_to_keep
        print(f"✓ Tīrīšana pabeigta. Atrasti {len(events_2026)} manuāli veidoti ieraksti.\n")

    # A. MIGRATION FROM 2025
    if RUN_MIGRATION:
        print("\n--- Sākam migrāciju no 2025. gada ierakstiem ---")
        try:
            events_2025 = calendar_service.events().list(
                calendarId=CALENDAR_ID, timeMin="2025-01-01T00:00:00Z",
                timeMax="2025-12-31T23:59:59Z", singleEvents=True, orderBy='startTime'
            ).execute().get('items', [])
        except Exception as e:
            print(f"Kļūda iegūstot 2025. gada kalendāru: {e}")
            events_2025 = []

        for ev in events_2025:
            urls = re.findall(r'(https?://[^\s"<]+)', ev.get('description', ''))
            if urls:
                found, succ_url = extract_events_with_ai(urls[0], ev.get('summary'))
                if found:
                    process_found_events(found, succ_url, events_2026, ev.get('description', ''))
                time.sleep(12)
    else:
        print("\n--- A. Kalendāra migrācija IZLAISTA (Pēc lietotāja izvēles) ---")
        
    # B. ADDITIONAL SOURCES
    print("\n--- Pārbaudām papildu avotus ---")
    PAPILDU_SAITES = [
        "https://inlinespeed.co.uk/events/event/page/2/",
        "https://inlinespeed.co.uk/events/event/",
        "https://szybkiewrotki.pl/lista-zawodow-2026/",
        "https://bayerncup.de/news",
        "https://ffroller-skateboard.fr/coupe-de-france-marathon-roller/carte-cfmr/",
        "https://www.worldskate.org/speed/events-speed/competitions.html",
        "https://www.schaatsen.nl/inlineskaten/",
        "https://speedskater-gg.de/wettkaempfe/rennkalender/",
        "https://www.klubluigino.sk/kalendar",
        "https://sport-action.cz/inline-rychlobrusleni/inline-pohar-26/#skate",
        "https://spordisarjad.ee/en/temposari",
        "https://www.rekozemst.be/events/",
        "https://www.schaatsen.nl/kalender/?discipline=Inlineskaten",
        "https://www.world-inline-cup.com/races"
    ]

    for url in PAPILDU_SAITES:
        print(f"\nPārbauda papildu avotu: {url}")
        found, succ_url = extract_events_with_ai(url, "Inline skating calendar")
        if found:
            print(f"  [SUCCESS] Atrasti {len(found)} pasākumi jaunajā avotā!")
            process_found_events(found, succ_url, events_2026, "Atrasts papildu avotā.")
        else:
            print(f"  [-] Šajā lapā vēl nav 2026. gada datumu.")
        time.sleep(12)

    print("\n✓ Migrācija pabeigta!")

if __name__ == "__main__":
    run_agent()
