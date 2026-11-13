import os
import re
import io
import json
import time
import base64
import random
import email
import smtplib
import imaplib
import threading
import traceback
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import requests
import gspread
from pypdf import PdfReader
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, jsonify
from crewai import Agent, Task, Crew, LLM

app = Flask(__name__)

# --- ENVIRONMENT VARIABLES ---
RAW_GEMINI_KEYS = os.environ.get("GEMINI_KEYS", os.environ.get("GEMINI_API_KEY", ""))
GEMINI_KEY_LIST = [k.strip() for k in RAW_GEMINI_KEYS.split(",") if k.strip()]
CURRENT_KEY_INDEX = 0

GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
SHEET_NAME = os.environ.get("SHEET_NAME", "USA_Dispatch_Leads")

# Initial Gmail (baad me Telegram se /add_outreach ya /add_warmup se jitne marzi jod dein)
INITIAL_EMAIL = os.environ.get("EMAIL_USER", "").strip().lower()
INITIAL_PASS = os.environ.get("EMAIL_PASS", "").strip()
SAFE_MAIN_EMAIL = os.environ.get("SAFE_MAIN_EMAIL", INITIAL_EMAIL).strip().lower()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://motus.dot.gov/"
}

# --- MULTI-KEY GEMINI RUNNER ---
def run_crew_with_rotation(role, goal, backstory, task_desc, expected_out):
    global CURRENT_KEY_INDEX
    if not GEMINI_KEY_LIST:
        raise ValueError("GEMINI_KEYS set nahi hai!")

    last_error = None
    total_keys = len(GEMINI_KEY_LIST)

    for attempt in range(total_keys):
        idx = (CURRENT_KEY_INDEX + attempt) % total_keys
        active_key = GEMINI_KEY_LIST[idx]
        try:
            llm = LLM(
                model="gemini/gemini-2.5-flash",
                api_key=active_key,
                temperature=0.6
            )
            agent = Agent(role=role, goal=goal, backstory=backstory, llm=llm, verbose=False)
            task = Task(description=task_desc, expected_output=expected_out, agent=agent)
            crew = Crew(agents=[agent], tasks=[task], verbose=False)
            result = str(crew.kickoff()).strip()
            CURRENT_KEY_INDEX = idx
            return result
        except Exception as e:
            last_error = e
            print(f"Key #{idx+1} failed, switching...")
            time.sleep(1)

    raise RuntimeError(f"Saari Gemini keys fail ho gayin: {last_error}")

# --- GOOGLE SHEETS DATABASE ---
def get_workbook():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds).open(SHEET_NAME)

def get_leads_sheet():
    wb = get_workbook()
    sheet = wb.sheet1
    if not sheet.row_values(1):
        sheet.append_row([
            "Date", "USDOT", "Company Name", "Owner Name",
            "Email", "Phone", "Status", "Sender Email",
            "Last Contact Date", "Subject", "Last Reply", "AI Response"
        ])
    return sheet

def get_accounts_sheet():
    wb = get_workbook()
    try:
        acc_sheet = wb.worksheet("Email_Accounts")
    except gspread.WorksheetNotFound:
        acc_sheet = wb.add_worksheet(title="Email_Accounts", rows="50", cols="4")
        acc_sheet.append_row(["Email", "App_Password", "Mode", "Status"])
        if INITIAL_EMAIL and INITIAL_PASS:
            acc_sheet.append_row([INITIAL_EMAIL, INITIAL_PASS, "outreach", "active"])
    return acc_sheet

def get_all_email_accounts():
    acc_sheet = get_accounts_sheet()
    rows = acc_sheet.get_all_records()
    accounts = []
    for r in rows:
        if str(r.get("Status", "")).strip().lower() == "active":
            accounts.append({
                "email": str(r.get("Email", "")).strip().lower(),
                "pass": str(r.get("App_Password", "")).strip(),
                "mode": str(r.get("Mode", "outreach")).strip().lower()
            })
    return accounts

def get_live_settings():
    wb = get_workbook()
    try:
        s_sheet = wb.worksheet("Settings")
    except gspread.WorksheetNotFound:
        s_sheet = wb.add_worksheet(title="Settings", rows="20", cols="2")
        s_sheet.append_row(["Key", "Value"])
        s_sheet.append_row(["DAILY_LIMIT_PER_EMAIL", "15"])
        s_sheet.append_row(["STATUS", "ACTIVE"])
        s_sheet.append_row(["SAFE_MAIN_EMAIL", SAFE_MAIN_EMAIL])
        s_sheet.append_row([
            "DISPATCH_RULES",
            "Flat 5% dispatch fee, zero forced dispatch, high-paying loads for Dry Van, Reefer, and Flatbed."
        ])
    rows = s_sheet.get_all_records()
    return {str(r["Key"]).strip(): str(r["Value"]).strip() for r in rows if r.get("Key")}, s_sheet

def update_live_setting(key, value):
    _, s_sheet = get_live_settings()
    cell = s_sheet.find(key)
    if cell:
        s_sheet.update_cell(cell.row, 2, str(value))
    else:
        s_sheet.append_row([key, str(value)])

# --- TELEGRAM HELPER ---
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        for i in range(0, len(message), 4000):
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message[i:i+4000]}, timeout=10)
    except Exception as e:
        print("Telegram Error:", e)

# --- EMAIL SENDER (WITH ANTI-SUSPENSION BACKUP SIGNATURE) ---
def send_email_from_account(sender_email, sender_pass, to_email, subject, body, safe_backup_email=""):
    signature = "\n\n--\nDispatch Desk | Apex Lane Dispatch"
    if safe_backup_email and safe_backup_email != sender_email:
        signature += f"\nDirect Support Desk: {safe_backup_email}"

    full_body = body.strip() + signature
    msg = MIMEText(full_body, "plain", "utf-8")
    msg["From"] = sender_email
    msg["To"] = to_email
    msg["Subject"] = subject

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(sender_email, sender_pass)
        server.sendmail(sender_email, to_email, msg.as_string())

# --- MOTUS DAILY REGISTER EXTRACTOR ---
def get_motus_daily_usdots():
    usdots = []
    for days_ago in range(0, 3):
        target_date = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y%m%d")
        url = f"https://motus-public-prod.s3.us-east-1.amazonaws.com/register/REGISTER{target_date}.pdf"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200 and b"%PDF" in r.content[:10]:
                reader = PdfReader(io.BytesIO(r.content))
                full_text = ""
                for page in reader.pages:
                    txt = page.extract_text() or ""
                    if "BROKER OF PROPERTY" not in txt.upper():
                        full_text += "\n" + txt
                usdots.extend(re.findall(r"\b\d{7,8}\b", full_text))
        except Exception as e:
            print(f"PDF fetch error ({target_date}):", e)
        if usdots:
            break
    return list(dict.fromkeys(usdots))

def fetch_motus_customer_details(usdot):
    endpoints = [
        f"https://motus.dot.gov/api/public/customer/{usdot}/account",
        f"https://motus.dot.gov/api/customer/{usdot}/account",
        f"https://motus.dot.gov/api/public/customers/{usdot}"
    ]
    for api_url in endpoints:
        try:
            resp = requests.get(api_url, headers=HEADERS, timeout=12)
            if resp.status_code == 200 and resp.text.strip().startswith("{"):
                data = resp.json()
                emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", json.dumps(data))
                valid_emails = [e.lower() for e in emails if not e.lower().endswith(".gov")]
                if valid_emails:
                    company = data.get("legalName") or data.get("legal_name") or data.get("businessName") or "Carrier"
                    phone = data.get("telephone") or data.get("businessPhone") or data.get("phone") or ""
                    officials = data.get("officials") or data.get("companyOfficials") or []
                    owner = officials[0].get("name", "") if officials and isinstance(officials[0], dict) else ""
                    return {
                        "usdot": usdot,
                        "company": str(company).strip(),
                        "owner": str(owner).strip(),
                        "email": valid_emails[0],
                        "phone": str(phone).strip()
                    }
        except Exception:
            continue
    return None

# --- CREWAI GENERATORS ---
def generate_cold_email(company_name, owner_name, rules):
    first_name = owner_name.split()[0].title() if owner_name else "there"
    return run_crew_with_rotation(
        role="USA Truck Dispatch Specialist",
        goal="Write a short, plain-text, human-like cold email to a newly registered motor carrier.",
        backstory="You work for a trusted USA freight dispatch agency. You write casual, direct, spam-free emails.",
        task_desc=(
            f"Write a short 4-sentence plain-text cold email to {first_name} at '{company_name}'. "
            f"Offer details: {rules} "
            "Do NOT include links, emojis, or spam trigger words. "
            "End with a simple question asking what trailer type they run. Output ONLY the email body."
        ),
        expected_out="Clean plain-text email body."
    )

def generate_followup_email(company_name, owner_name, followup_num, rules):
    first_name = owner_name.split()[0].title() if owner_name else "there"
    return run_crew_with_rotation(
        role="USA Truck Dispatch Specialist",
        goal="Write a brief, polite follow-up email bumping up the previous dispatch offer.",
        backstory="You are a helpful US freight dispatcher checking in with an owner-operator.",
        task_desc=(
            f"Write follow-up #{followup_num} (2-3 short sentences max) to {first_name} at '{company_name}'. "
            f"Mention briefly: {rules}. Ask if they have a truck available this week that needs loads. "
            "No links, no emojis. Output ONLY the email body."
        ),
        expected_out="Short plain-text follow-up body."
    )

def generate_ai_reply(company_name, carrier_message, rules):
    return run_crew_with_rotation(
        role="Senior Freight Dispatcher & Onboarding Manager",
        goal="Answer the carrier's reply naturally and guide them to send onboarding documents.",
        backstory="You are an experienced US truck dispatcher who closes carriers smoothly.",
        task_desc=(
            f"A truck owner from '{company_name}' replied:\n\"{carrier_message}\"\n\n"
            f"Company rules: {rules}\n"
            "Write a short, polite, human-like plain-text reply answering their question and asking them to "
            "reply with their MC Number, COI (Certificate of Insurance), and W-9 so we can start booking loads today. "
            "No links or emojis. Output ONLY the reply text."
        ),
        expected_out="Direct plain-text email reply."
    )

# --- WARMUP ENGINE (WARMUP ACCOUNTS KE LIYE) ---
def run_warmup_round(all_accounts):
    warmup_accs = [a for a in all_accounts if a["mode"] == "warmup"]
    if len(warmup_accs) < 2:
        return 0
    topics = [
        ("Quick check on lane rates", "Hey, did you see the dry van rates out of Dallas this morning? Looking solid."),
        ("Schedule for next week", "Let me know once the driver confirms the Monday pickup window."),
        ("Carrier packet update", "Got the updated insurance certificate saved in the folder. Thanks!"),
        ("Route timing question", "How many miles are left on the Atlanta run before empty?")
    ]
    warmed = 0
    for sender in warmup_accs:
        receivers = [a for a in warmup_accs if a["email"] != sender["email"]]
        target = random.choice(receivers)
        subj, body = random.choice(topics)
        try:
            send_email_from_account(sender["email"], sender["pass"], target["email"], subj, body)
            warmed += 1
            time.sleep(5)
        except Exception as e:
            print(f"Warmup error ({sender['email']}):", e)
    return warmed

# --- DAILY PIPELINE (NEW LEADS + STICKY FOLLOWUPS + WARMUP) ---
def run_daily_pipeline():
    try:
        settings, _ = get_live_settings()
        if settings.get("STATUS", "ACTIVE").upper() != "ACTIVE":
            send_telegram("System abhi PAUSED hai. Chalu karne ke liye /resume bhejein.")
            return

        limit_per_email = int(settings.get("DAILY_LIMIT_PER_EMAIL", 15))
        rules = settings.get("DISPATCH_RULES", "5% dispatch fee, no forced dispatch.")
        safe_backup = settings.get("SAFE_MAIN_EMAIL", SAFE_MAIN_EMAIL)

        all_accounts = get_all_email_accounts()
        outreach_accs = [a for a in all_accounts if a["mode"] == "outreach"]
        if not outreach_accs:
            send_telegram("Koi active outreach Gmail nahi mila! /add_outreach se Gmail add karein.")
            return

        acc_pass_map = {a["email"]: a["pass"] for a in outreach_accs}
        sent_counter = {a["email"]: 0 for a in outreach_accs}

        sheet = get_leads_sheet()
        records = sheet.get_all_records()
        existing_dots = {str(r.get("USDOT", "")).strip() for r in records}
        existing_emails = {str(r.get("Email", "")).strip().lower() for r in records}

        # 1. Extract New MOTUS Leads
        total_capacity = len(outreach_accs) * limit_per_email
        usdots = get_motus_daily_usdots()
        added = 0
        today_str = datetime.now().strftime("%Y-%m-%d")

        for dot in usdots:
            if added >= total_capacity:
                break
            if dot in existing_dots:
                continue
            details = fetch_motus_customer_details(dot)
            if details and details["email"] not in existing_emails:
                sheet.append_row([
                    today_str, details["usdot"], details["company"],
                    details["owner"], details["email"], details["phone"],
                    "New", "", "", "", "", ""
                ])
                existing_dots.add(dot)
                existing_emails.add(details["email"])
                added += 1
            time.sleep(1)

        # 2. Process Follow-ups (EXACT SAME GMAIL SE) & New Cold Emails
        records = sheet.get_all_records()
        new_sent = 0
        followup_sent = 0
        today_dt = datetime.now()

        for idx, row in enumerate(records, start=2):
            status = str(row.get("Status", "")).strip()
            to_email = str(row.get("Email", "")).strip()
            company = str(row.get("Company Name", "Partner")).strip()
            owner = str(row.get("Owner Name", "")).strip()
            locked_sender = str(row.get("Sender Email", "")).strip().lower()
            last_date_str = str(row.get("Last Contact Date", "")).strip()
            prev_subj = str(row.get("Subject", f"Dispatch support for {company}")).strip()

            if not to_email:
                continue

            # A. Sticky Follow-Up (2 days & 4 days) from the EXACT SAME Gmail
            if status in ("Sent", "Followup 1") and locked_sender in acc_pass_map and last_date_str:
                if sent_counter[locked_sender] >= limit_per_email:
                    continue
                try:
                    days_passed = (today_dt - datetime.strptime(last_date_str, "%Y-%m-%d")).days
                except ValueError:
                    days_passed = 0

                if status == "Sent" and days_passed >= 2:
                    f_body = generate_followup_email(company, owner, 1, rules)
                    re_subj = prev_subj if prev_subj.lower().startswith("re:") else f"Re: {prev_subj}"
                    send_email_from_account(locked_sender, acc_pass_map[locked_sender], to_email, re_subj, f_body, safe_backup)
                    sheet.update_cell(idx, 7, "Followup 1")
                    sheet.update_cell(idx, 9, today_str)
                    sent_counter[locked_sender] += 1
                    followup_sent += 1
                    time.sleep(15)

                elif status == "Followup 1" and days_passed >= 2:
                    f_body = generate_followup_email(company, owner, 2, rules)
                    re_subj = prev_subj if prev_subj.lower().startswith("re:") else f"Re: {prev_subj}"
                    send_email_from_account(locked_sender, acc_pass_map[locked_sender], to_email, re_subj, f_body, safe_backup)
                    sheet.update_cell(idx, 7, "Followup 2")
                    sheet.update_cell(idx, 9, today_str)
                    sent_counter[locked_sender] += 1
                    followup_sent += 1
                    time.sleep(15)

            # B. New Cold Email (Pick available outreach Gmail and LOCK it to this row)
            elif status == "New":
                available_senders = [e for e, c in sent_counter.items() if c < limit_per_email]
                if not available_senders:
                    break
                chosen_sender = min(available_senders, key=lambda e: sent_counter[e])
                subj = f"Dispatch support for {company}"
                body = generate_cold_email(company, owner, rules)

                send_email_from_account(chosen_sender, acc_pass_map[chosen_sender], to_email, subj, body, safe_backup)
                sheet.update_cell(idx, 7, "Sent")
                sheet.update_cell(idx, 8, chosen_sender)  # LOCKED SENDER EMAIL
                sheet.update_cell(idx, 9, today_str)
                sheet.update_cell(idx, 10, subj)
                sent_counter[chosen_sender] += 1
                new_sent += 1
                time.sleep(15)

        # 3. Run Warmup Round for Warmup Accounts
        warmed_count = run_warmup_round(all_accounts)

        send_telegram(
            f"Daily Job Complete!\n"
            f"New MOTUS Leads: {added}\n"
            f"New Cold Emails: {new_sent}\n"
            f"Same-Gmail Followups: {followup_sent}\n"
            f"Warmup Mails Sent: {warmed_count}"
        )
    except Exception:
        err = traceback.format_exc()
        print("Daily Pipeline Error:\n", err)
        send_telegram(f"Daily Pipeline Error:\n{err[:600]}")

# --- INBOX WATCHER (HAR OUTREACH GMAIL CHECK KAREGA AUR USI SE REPLY DEGA) ---
def watch_all_inboxes():
    while True:
        try:
            all_accounts = get_all_email_accounts()
            outreach_accs = [a for a in all_accounts if a["mode"] == "outreach"]

            if outreach_accs:
                settings, _ = get_live_settings()
                rules = settings.get("DISPATCH_RULES", "5% dispatch fee, no forced dispatch.")
                safe_backup = settings.get("SAFE_MAIN_EMAIL", SAFE_MAIN_EMAIL)

                for acc in outreach_accs:
                    acc_email = acc["email"]
                    acc_pass = acc["pass"]
                    try:
                        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
                        mail.login(acc_email, acc_pass)
                        mail.select("inbox")

                        _, messages = mail.search(None, "UNSEEN")
                        email_ids = messages[0].split()

                        if email_ids:
                            sheet = get_leads_sheet()
                            records = sheet.get_all_records()

                            for e_id in email_ids:
                                _, msg_data = mail.fetch(e_id, "(RFC822)")
                                msg = email.message_from_bytes(msg_data[0][1])
                                sender_email = email.utils.parseaddr(msg.get("From", ""))[1].lower().strip()
                                subject = msg.get("Subject", "Re: Dispatch Services")

                                body = ""
                                if msg.is_multipart():
                                    for part in msg.walk():
                                        if part.get_content_type() == "text/plain":
                                            body = part.get_payload(decode=True).decode(errors="ignore")
                                            break
                                else:
                                    body = msg.get_payload(decode=True).decode(errors="ignore")

                                for idx, row in enumerate(records, start=2):
                                    if str(row.get("Email", "")).strip().lower() == sender_email:
                                        company = row.get("Company Name", "Carrier")
                                        ai_reply = generate_ai_reply(company, body[:1000], rules)
                                        re_sub = subject if subject.lower().startswith("re:") else f"Re: {subject}"

                                        # EXACT USI GMAIL SE REPLY BHEJEGA JISPE REPLY AAYA HAI
                                        send_email_from_account(acc_email, acc_pass, sender_email, re_sub, ai_reply, safe_backup)

                                        sheet.update_cell(idx, 7, "Replied - AI Active")
                                        sheet.update_cell(idx, 8, acc_email)
                                        sheet.update_cell(idx, 11, body[:500])
                                        sheet.update_cell(idx, 12, ai_reply[:500])

                                        send_telegram(
                                            f"NEW CARRIER REPLY!\n"
                                            f"Via Gmail: {acc_email}\n"
                                            f"Company: {company}\n"
                                            f"Driver Email: {sender_email}\n\n"
                                            f"Driver Said:\n{body[:300]}\n\n"
                                            f"AI Replied:\n{ai_reply[:300]}"
                                        )
                                        break
                        mail.logout()
                    except Exception as acc_err:
                        print(f"Inbox check error for {acc_email}:", acc_err)
        except Exception:
            print("Watcher Loop Error:\n", traceback.format_exc())

        time.sleep(30)

# --- TELEGRAM GITHUB SELF-UPDATER ---
def push_code_to_github(user_instruction):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return "Render mein GITHUB_TOKEN aur GITHUB_REPO set nahi hain."

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/main.py"
    gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    r = requests.get(api_url, headers=gh_headers, timeout=15)
    if r.status_code != 200:
        return f"GitHub fetch error: {r.text[:200]}"

    file_data = r.json()
    sha = file_data["sha"]
    current_code = base64.b64decode(file_data["content"]).decode("utf-8")

    new_code = run_crew_with_rotation(
        role="Senior Python & CrewAI Software Engineer",
        goal="Modify existing Flask + CrewAI Python code safely without breaking features.",
        backstory="You write clean, bug-free Python code for 512MB RAM servers.",
        task_desc=(
            f"Current `main.py`:\n```python\n{current_code}\n```\n\n"
            f"USER REQUESTED CHANGE:\n\"{user_instruction}\"\n\n"
            "Apply this exact change while keeping multi-Gmail sticky routing, warmup, follow-ups, "
            "Google Sheets, and Telegram bot commands intact. Return ONLY raw Python code without ``` fences."
        ),
        expected_out="Complete raw Python code."
    )

    new_code = re.sub(r"^```python\s*", "", new_code.strip())
    new_code = re.sub(r"\s*```$", "", new_code.strip())

    try:
        compile(new_code, "main.py", "exec")
    except SyntaxError as syn_err:
        return f"Syntax error detected, push aborted: {syn_err}"

    payload = {
        "message": f"Telegram AI update: {user_instruction[:50]}",
        "content": base64.b64encode(new_code.encode("utf-8")).decode("utf-8"),
        "sha": sha
    }
    put_res = requests.put(api_url, headers=gh_headers, json=payload, timeout=20)
    if put_res.status_code in (200, 201):
        return "Done! Naya code GitHub par push ho gaya hai. Render 2 minute mein auto-deploy kar dega."
    return f"GitHub push failed: {put_res.text[:200]}"

# --- TELEGRAM COMMAND CONTROLLER ---
def watch_telegram_commands():
    last_update_id = 0
    while True:
        try:
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/getUpdates"
                resp = requests.get(url, params={"offset": last_update_id + 1, "timeout": 20}, timeout=25)
                data = resp.json()

                for item in data.get("result", []):
                    last_update_id = item["update_id"]
                    msg = item.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", "")).strip()
                    text = msg.get("text", "").strip()

                    if chat_id != TELEGRAM_CHAT_ID or not text:
                        continue

                    if text in ("/start", "/help"):
                        send_telegram(
                            "USA Dispatch AI Control Panel:\n\n"
                            "/status - System aur Gmail accounts dekhein\n"
                            "/run - Abhi daily job chalayein\n"
                            "/add_outreach <email> <app_pass> - Naya outreach Gmail jodein\n"
                            "/add_warmup <email> <app_pass> - Naya warmup Gmail jodein\n"
                            "/promote <email> - Warmup Gmail ko outreach mein shift karein\n"
                            "/set_safe <email> - Backup Safe Main Email set karein\n"
                            "/set_prompt <rule> - AI offer/fee rule badlein\n"
                            "/set_limit <num> - Per-Gmail daily limit set karein\n"
                            "/pause | /resume - Outreach rokein ya chalu karein\n"
                            "/code <prompt> - CrewAI se poora code edit karwayein"
                        )
                    elif text == "/status":
                        settings, _ = get_live_settings()
                        accs = get_all_email_accounts()
                        out_list = [a["email"] for a in accs if a["mode"] == "outreach"]
                        warm_list = [a["email"] for a in accs if a["mode"] == "warmup"]
                        send_telegram(
                            f"Status: {settings.get('STATUS')}\n"
                            f"Gemini Keys: {len(GEMINI_KEY_LIST)}\n"
                            f"Safe Backup Email: {settings.get('SAFE_MAIN_EMAIL')}\n"
                            f"Outreach Gmails ({len(out_list)}): {', '.join(out_list)}\n"
                            f"Warmup Gmails ({len(warm_list)}): {', '.join(warm_list)}\n"
                            f"Limit Per Gmail: {settings.get('DAILY_LIMIT_PER_EMAIL')}\n"
                            f"Rule: {settings.get('DISPATCH_RULES')}"
                        )
                    elif text == "/run":
                        send_telegram("Daily job shuru kar di gayi hai...")
                        threading.Thread(target=run_daily_pipeline, daemon=True).start()
                    elif text == "/pause":
                        update_live_setting("STATUS", "PAUSED")
                        send_telegram("Outreach PAUSED.")
                    elif text == "/resume":
                        update_live_setting("STATUS", "ACTIVE")
                        send_telegram("Outreach ACTIVE.")
                    elif text.startswith("/add_outreach "):
                        parts = text.split()
                        if len(parts) >= 3:
                            acc_sheet = get_accounts_sheet()
                            acc_sheet.append_row([parts[1].lower(), parts[2], "outreach", "active"])
                            send_telegram(f"Outreach Gmail added: {parts[1].lower()}")
                        else:
                            send_telegram("Format: /add_outreach email@gmail.com 16digitpassword")
                    elif text.startswith("/add_warmup "):
                        parts = text.split()
                        if len(parts) >= 3:
                            acc_sheet = get_accounts_sheet()
                            acc_sheet.append_row([parts[1].lower(), parts[2], "warmup", "active"])
                            send_telegram(f"Warmup Gmail added: {parts[1].lower()}")
                        else:
                            send_telegram("Format: /add_warmup email@gmail.com 16digitpassword")
                    elif text.startswith("/promote "):
                        target_mail = text.replace("/promote ", "").strip().lower()
                        acc_sheet = get_accounts_sheet()
                        cell = acc_sheet.find(target_mail)
                        if cell:
                            acc_sheet.update_cell(cell.row, 3, "outreach")
                            send_telegram(f"{target_mail} ko warmup se OUTREACH mode mein shift kar diya gaya!")
                        else:
                            send_telegram("Ye email Email_Accounts sheet mein nahi mila.")
                    elif text.startswith("/set_safe "):
                        safe_mail = text.replace("/set_safe ", "").strip().lower()
                        update_live_setting("SAFE_MAIN_EMAIL", safe_mail)
                        send_telegram(f"Safe Backup Email update ho gaya: {safe_mail}")
                    elif text.startswith("/set_limit "):
                        val = text.replace("/set_limit ", "").strip()
                        if val.isdigit():
                            update_live_setting("DAILY_LIMIT_PER_EMAIL", val)
                            send_telegram(f"Per-Gmail daily limit ab {val} hai.")
                    elif text.startswith("/set_prompt "):
                        new_rule = text.replace("/set_prompt ", "").strip()
                        update_live_setting("DISPATCH_RULES", new_rule)
                        send_telegram(f"Naya AI Rule save ho gaya:\n{new_rule}")
                    elif text.startswith("/code "):
                        instruction = text.replace("/code ", "").strip()
                        send_telegram("CrewAI code likh kar GitHub update kar raha hai, 20 sec rukiye...")
                        send_telegram(push_code_to_github(instruction))
        except Exception:
            print("Telegram Poll Error:\n", traceback.format_exc())
            time.sleep(5)

threading.Thread(target=watch_all_inboxes, daemon=True).start()
threading.Thread(target=watch_telegram_commands, daemon=True).start()

@app.route("/")
def home():
    return jsonify({"status": "running", "gemini_keys": len(GEMINI_KEY_LIST)})

@app.route("/daily-job")
def trigger_daily_job():
    threading.Thread(target=run_daily_pipeline, daemon=True).start()
    return jsonify({"status": "started"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
