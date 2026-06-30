# Free NSE/BSE Announcement Scanner with Telegram Alerts
# Fetches today's BSE corporate announcements, filters by keyword categories,
# and sends new (not-yet-seen) relevant ones to Telegram.
#
# Designed to run on a schedule (e.g. via GitHub Actions) - each run:
#  1. Fetches today's announcements from BSE
#  2. Filters for relevant ones using categorized keywords
#  3. Compares against a "seen" list stored in a local file (sent_alerts.json)
#  4. Sends only NEW relevant announcements to Telegram
#  5. Updates the seen list and saves it back to the file
#
# Requires: pip install bse requests

import json
import os
import requests
from datetime import datetime
from bse import BSE
from nse import NSE

# ---- CONFIG: token and chat ID are read from environment variables.
# In GitHub Actions, these are securely injected from repository Secrets.
# For local/Colab testing, set them with os.environ before running this script. ----
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables must be set.")
    raise SystemExit(1)

SEEN_FILE = "sent_alerts.json"

# ---- Categorized keyword sets ----
KEYWORDS = {
    "Order/MoU/Contract": [
        "order win", "bags order", "secures order", "wins order", "receives order",
        "order received", "new order", "award of order", "receipt of order",
        "mou", "memorandum of understanding", "letter of award", "loa",
        "letter of intent", "loi", "contract win", "awarded contract",
        "purchase order", "work order", "supply agreement", "definitive agreement"
    ],
    "Regulatory/Drug Approval": [
        "usfda", "us fda", "fda approval", "anda approval", "cdsco approval",
        "drug approval", "clinical trial", "phase iii", "phase 3", "phase ii",
        "marketing authorization", "import license", "manufacturing license",
        "who-gmp", "eu-gmp", "form 483", "warning letter", "import alert"
    ],
    "M&A/Corporate Action": [
        "definitive acquisition", "completes acquisition", "announces acquisition",
        "merger", "amalgamation", "stake sale", "divestment",
        "joint venture agreement", "strategic investment", "delisting", "buyback"
    ],
    "Capacity/Expansion": [
        "capacity expansion", "new plant", "greenfield", "brownfield",
        "commissioning", "capex", "expansion plan"
    ],
    "Financial/Results": [
        "credit rating upgrade", "credit rating downgrade", "rating upgrade",
        "rating downgrade", "default", "insolvency", "nclt", "ibc proceedings"
    ],
    "Litigation/Regulatory Action": [
        "sebi order", "show cause notice", "raid conducted", "fraud detected",
        "fir registered", "cbi investigation", "search and seizure",
        "regulatory penalty", "sebi penalty"
    ]
}


import re

def classify_announcement(headline, detailed_text, category_name, subcategory_name):
    combined_text = f"{headline} {detailed_text} {category_name} {subcategory_name}".lower()
    matched_categories = []
    for category, terms in KEYWORDS.items():
        term_idx = 0
        found_in_category = False
        while term_idx < len(terms):
            term = terms[term_idx]
            pattern = r"\b" + re.escape(term) + r"\b"
            found = re.search(pattern, combined_text) is not None
            if found:
                found_in_category = True
            term_idx = term_idx + 1
        if found_in_category:
            matched_categories.append(category)
    return matched_categories


def load_seen_ids():
    file_exists = os.path.exists(SEEN_FILE)
    if not file_exists:
        return set()
    with open(SEEN_FILE, "r") as f:
        data = json.load(f)
    return set(data.get("seen_ids", []))


def save_seen_ids(seen_ids):
    data = {"seen_ids": list(seen_ids)}
    with open(SEEN_FILE, "w") as f:
        json.dump(data, f)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    response = requests.post(url, json=payload)
    return response.json()


def fetch_today_nse_announcements():
    try:
        nse = NSE(download_folder="./")
        data = nse.announcements(index="equities")
        nse.exit()
        return data
    except Exception as e:
        print(f"NSE fetch failed (continuing with BSE only): {e}")
        return []


def fetch_today_announcements():
    bse = BSE(download_folder="./")
    all_rows = []
    page_no = 1
    total_count = None
    while True:
        data = bse.announcements(page_no=page_no)
        rows = data.get("Table", [])
        no_rows = len(rows) == 0
        if no_rows:
            break
        all_rows.extend(rows)
        if total_count is None:
            table1 = data.get("Table1", [])
            has_count = len(table1) > 0
            if has_count:
                total_count = table1[0].get("ROWCNT", len(all_rows))
        reached_total = total_count is not None and len(all_rows) >= total_count
        if reached_total:
            break
        page_no = page_no + 1
        too_many_pages = page_no > 60
        if too_many_pages:
            break
    bse.exit()
    return all_rows


def process_announcement_list(announcements, source_name, seen_ids):
    new_alerts_sent = 0
    ann_idx = 0
    while ann_idx < len(announcements):
        ann = announcements[ann_idx]
        if source_name == "BSE":
            ann_id = "BSE_" + str(ann.get("NEWSID", ""))
            headline = ann.get("HEADLINE", "")
            detailed_text = ann.get("MORE", "")
            display_text = detailed_text if len(detailed_text) > len(headline) else headline
            company = ann.get("SLONGNAME", "")
            category_name = ann.get("CATEGORYNAME", "")
            subcategory_name = ann.get("SUBCATNAME", "")
        else:
            ann_id = "NSE_" + str(ann.get("seq_id", ""))
            headline = ann.get("attchmntText", "") or ann.get("desc", "")
            display_text = headline
            company = ann.get("sm_name", "") or ann.get("symbol", "")
            category_name = ann.get("desc", "")
            subcategory_name = ""
        already_seen = ann_id in seen_ids
        if already_seen:
            ann_idx = ann_idx + 1
            continue
        categories = classify_announcement(headline, display_text, category_name, subcategory_name)
        is_relevant = len(categories) > 0
        if is_relevant:
            categories_str = ", ".join(categories)
            safe_text = display_text[:3500]
            text_was_cut = len(display_text) > 3500
            if text_was_cut:
                safe_text = safe_text + "... (truncated)"
            message = f"[{source_name}] <b>{company}</b>\n{safe_text}\n\nMatched: {categories_str}"
            send_result = send_telegram_message(message)
            send_succeeded = send_result.get("ok", False)
            if send_succeeded:
                new_alerts_sent = new_alerts_sent + 1
                seen_ids.add(ann_id)
            else:
                print(f"FAILED to send alert for {ann_id}: {send_result}")
        else:
            seen_ids.add(ann_id)
        ann_idx = ann_idx + 1
    return new_alerts_sent


def run_scan():
    seen_ids = load_seen_ids()

    bse_announcements = fetch_today_announcements()
    print(f"Fetched {len(bse_announcements)} total announcements from BSE today.")
    bse_alerts = process_announcement_list(bse_announcements, "BSE", seen_ids)

    nse_announcements = fetch_today_nse_announcements()
    print(f"Fetched {len(nse_announcements)} total announcements from NSE today.")
    nse_alerts = process_announcement_list(nse_announcements, "NSE", seen_ids)

    save_seen_ids(seen_ids)
    total_alerts = bse_alerts + nse_alerts
    print(f"Sent {total_alerts} new alerts ({bse_alerts} BSE, {nse_alerts} NSE).")
    timestamp = datetime.now().isoformat()
    print(f"Scan complete at {timestamp}")


run_scan()
