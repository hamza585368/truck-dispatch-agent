import os
import re
import time
import json
import imaplib
import smtplib
import email
import threading
import requests
import gspread
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pypdf import PdfReader
from io import BytesIO
from flask import Flask
from google.oauth2.service_account import Credentials
from crewai import Agent, Task, Crew, LLM

app = Flask(__name__)

# --- CONFIGURATION FROM RENDER ENV VARIABLES ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SMTP_HOST = os.environ.get("SMTP_HOST")          # e.g., ://zoho.com or ://hostinger.com
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
IMAP_HOST = os.environ.get("IMAP_HOST")          # e.g., ://zoho.com or ://hostinger.com
EMAIL_USER = os.environ.get("EMAIL_USER")        # Aapka custom domain email (e.g., dispatch@yourdomain.com)
EMAIL_PASS = os.environ.get("EMAIL_PASS")        # Email password / App password
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")

# --- CREWAI SETUP ---
gemini_llm = LLM(
    model="gemini/gemini-2.5-flash",
    api_key=GEMINI_API_KEY
)

dispatcher_agent = Agent(
    role="Senior USA Truck Dispatcher",
    goal="Convert US truck owner-operators into dispatch clients and negotiate deals naturally.",
    backstory="You are an experienced USA truck dispatcher. You write short, human-like, plain-text emails without spam words or links. When a carrier replies, you answer their questions professionally and ask for their MC number, COI, and W9 to get started.",
    llm=gemini_llm,
    verbose=False
)

# --- GOOGLE SHEETS CONNECTION ---
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://googleapis.com"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).sheet1
    if not sheet.row_values(1):
        sheet.append_row(["Date", "USDOT", "Company Name", "Email", "Phone", "Status", "Last Reply", "AI Response"])
    return sheet

# --- TELEGRAM ALERT ---
def send_telegram(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        except Exception:
            pass

# --- STEP 1: EXTRACT LEADS FROM MOTUS / FMCSA PDF ---
def extract_motus_leads():
    sheet = get_sheet()
    existing_dots = set(sheet.col_values(2))
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y%m%d")
    pdf_url = f"https://dot.gov{yesterday}"
    
    try:
        resp = requests.get(pdf_url, timeout=20)
        if b"%PDF" not in resp.content[:10]:
            return "No new PDF found for date."
        
        reader = PdfReader(BytesIO(resp.content))
        text = "\n".join([page.extract_text() or "" for page in reader.pages])
        usdots = list(set(re.findall(r'\b\d{7,8}\b', text)))[:30] # Daily safe limit: 30 leads
        
        added = 0
        for dot in usdots:
            if dot in existing_dots:
                continue
            # Fetch public Socrata / MOTUS carrier details
            api_url = f"https://transportation.gov{dot}"
            r = requests.get(api_url, timeout=10).json()
            if r and isinstance(r, list):
                carrier = r[0]
                email_addr = carrier.get("email_address", "").strip()
                company = carrier.get("legal_name", "Trucking Partner").strip()
                phone = carrier.get("telephone", "")
                if email_addr and "@" in email_addr:
                    sheet.append_row([datetime.now().strftime("%Y-%m-%d"), dot, company, email_addr, phone, "New", "", ""])
                    added += 1
        return f"Extracted {added} new leads to Google Sheet."
    except Exception as e:
        return f"Extraction error: {str(e)}"

# --- STEP 2: SEND COLD EMAILS VIA CUSTOM DOMAIN ---
def send_email(to_email, subject, body):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = to_email
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, to_email, msg.as_string())

def process_outreach():
    sheet = get_sheet()
    records = sheet.get_all_records()
    sent_count = 0
    for idx, row in enumerate(records, start=2):
        if row.get("Status") == "New" and sent_count < 15:  # Safe daily batch
            company = row.get("Company Name")
            to_email = row.get("Email")
            task = Task(
                description=f"Write a short, friendly 3-sentence cold email to '{company}' offering USA truck dispatch services (5% fee, no forced dispatch, high-paying loads). Output ONLY the email body, no subject line.",
                expected_output="Plain text 3-sentence email body.",
                agent=dispatcher_agent
            )
            crew = Crew(agents=[dispatcher_agent], tasks=[task])
            email_body = str(crew.kickoff()).strip()
            subject = f"Loads available for {company}"
            try:
                send_email(to_email, subject, email_body)
                sheet.update_cell(idx, 6, "Sent")
                sent_count += 1
                time.sleep(5)
            except Exception as e:
                sheet.update_cell(idx, 6, f"Error: {str(e)[:20]}")
    return f"Sent {sent_count} outreach emails."

# --- STEP 3: REAL-TIME FAST REPLY WATCHER & AUTO-NEGOTIATOR ---
def reply_watcher_loop():
    while True:
        try:
            if IMAP_HOST and EMAIL_USER and EMAIL_PASS:
                mail = imaplib.IMAP4_SSL(IMAP_HOST)
                mail.login(EMAIL_USER, EMAIL_PASS)
                mail.select("inbox")
                status, messages = mail.search(None, "UNSEEN")
                email_ids = messages[0].split()
                
                if email_ids:
                    sheet = get_sheet()
                    records = sheet.get_all_records()
                    
                    for e_id in email_ids:
                        _, msg_data = mail.fetch(e_id, "(RFC822)")
                        raw_email = msg_data[0][1]
                        msg = email.message_from_bytes(raw_email)
                        sender = email.utils.parseaddr(msg["From"])[1].lower()
                        subj = msg["Subject"] or "Dispatch Inquiry"
                        
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    body = part.get_payload(decode=True).decode(errors="ignore")
                                    break
                        else:
                            body = msg.get_payload(decode=True).decode(errors="ignore")
                        
                        # Match sender with Google Sheet
                        for idx, row in enumerate(records, start=2):
                            if str(row.get("Email", "")).lower() == sender:
                                company = row.get("Company Name")
                                # CrewAI generates direct negotiation reply
                                reply_task = Task(
                                    description=f"Carrier '{company}' replied to our dispatch email with: '{body[:500]}'. Write a helpful, direct reply answering them and asking for their MC number, COI, and W-9 to onboard them.",
                                    expected_output="Short plain-text reply.",
                                    agent=dispatcher_agent
                                )
                                crew = Crew(agents=[dispatcher_agent], tasks=[reply_task])
                                ai_reply = str(crew.kickoff()).strip()
                                
                                # Send direct reply via custom domain
                                send_email(sender, f"Re: {subj}", ai_reply)
                                
                                # Update Google Sheet & Stop Follow-ups
                                sheet.update_cell(idx, 6, "Replied - AI Active")
                                sheet.update_cell(idx, 7, body[:200])
                                sheet.update_cell(idx, 8, ai_reply[:200])
                                
                                # Instant Telegram Alert to You
                                send_telegram(f"🚨 NEW REPLY!\nCompany: {company}\nEmail: {sender}\nMessage: {body[:150]}\n\n✅ CrewAI Reply Sent:\n{ai_reply[:150]}")
                mail.logout()
        except Exception:
            pass
        time.sleep(3)  # Safe fast polling every 3 seconds

# Start background reply listener automatically
threading.Thread(target=reply_watcher_loop, daemon=True).start()

@app.route('/')
def home():
    return "CrewAI Truck Dispatch System + Live Reply Watcher is Running!", 200

@app.route('/daily-job')
def daily_job():
    res1 = extract_motus_leads()
    res2 = process_outreach()
    return f"{res1} | {res2}", 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
