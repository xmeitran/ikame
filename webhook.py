"""Minimal HTTPS webhook receiver for Lark recording-ready events.

Run behind a public HTTPS tunnel (for example localtunnel) and configure the
public URL + `/lark/events` in Developer Console.
"""
import json
import logging
import os
import re
import secrets
import subprocess
import datetime
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lark-webhook")
APP_ID = os.getenv("LARK_APP_ID")
APP_SECRET = os.getenv("LARK_APP_SECRET")
BASE_TOKEN = os.getenv("BITABLE_APP_TOKEN")
TABLE_ID = os.getenv("MEETINGS_TABLE_ID")
PROJECTS_TABLE_ID = os.getenv("PROJECTS_TABLE_ID")
TASKS_TABLE_ID = os.getenv("TASKS_TABLE_ID")
CODEX_BIN = os.getenv("CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex")
DOMAIN = os.getenv("LARK_DOMAIN", "https://open.larksuite.com")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "")
OAUTH_STATE = secrets.token_urlsafe(24)
OAUTH_TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".oauth_token.json")

def tenant_token():
    r = requests.post(f"{DOMAIN}/open-apis/auth/v3/tenant_access_token/internal", json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=20)
    r.raise_for_status()
    return r.json()["tenant_access_token"]

def user_token():
    try:
        with open(OAUTH_TOKEN_FILE, encoding="utf-8") as fh:
            return json.load(fh)["access_token"]
    except (OSError, KeyError):
        return ""

def find_value(obj, names):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in names and isinstance(value, (str, int)) and value:
                return str(value)
            found = find_value(value, names)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_value(value, names)
            if found:
                return found
    return ""

def fetch_transcript(minute_token):
    token = user_token()
    if not token:
        raise RuntimeError("No OAuth user token; open /oauth/start first")
    url = f"{DOMAIN}/open-apis/minutes/v1/minutes/{urllib.parse.quote(minute_token, safe='')}/transcript"
    r = requests.get(url, params={"need_speaker": "true", "need_timestamp": "true", "file_format": "txt"}, headers={"Authorization": f"Bearer {token}"}, timeout=45)
    r.raise_for_status()
    if "application/json" in r.headers.get("content-type", ""):
        data = r.json().get("data", r.json())
        return data.get("content", "") if isinstance(data, dict) else str(data)
    return r.text

def create_minute_record(event, minute_token, transcript):
    event_id = event.get("_event_id", "unknown")
    fields = {
        "Meeting Title": "Lark meeting minutes",
        "Transcript / Raw Recap": transcript,
        "Minutes URL": f"{DOMAIN}/minutes/{minute_token}",
        "Source Event ID": event_id,
        "Ingestion Status": "Imported — awaiting AI",
        "AI Processing Status": "Pending",
    }
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records"
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base create failed {r.status_code}: {r.text[:500]}")
    log.info("Base record created from generated Minutes %s", minute_token)
    create_project_and_tasks(event, fields["Meeting Title"], transcript)

def bitable_create(table_id, fields):
    if not table_id:
        return ""
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base create failed {r.status_code}: {r.text[:500]}")
    return (r.json().get("data") or {}).get("record", {}).get("record_id", "")

def participant_ids(event):
    """Collect user open_ids from an event, excluding duplicates."""
    if event.get("_event_type") == "im.message.receive_v1":
        sender = ((event.get("sender") or {}).get("sender_id") or {}).get("open_id")
        if sender:
            return [sender]
    found = []
    def walk(value):
        if isinstance(value, dict):
            oid = value.get("open_id")
            if isinstance(oid, str) and oid and oid not in found:
                found.append(oid)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(event)
    return found

def codex_plan(transcript):
    """Normalize Vietnamese transcript and extract a strict project/task plan."""
    if not transcript:
        return {"project": "Lark meeting project", "tasks": []}
    prompt = f'''Đọc transcript cuộc họp dưới đây. Trả về DUY NHẤT JSON hợp lệ, không markdown:
{{"project":"Tên dự án ngắn","milestone":"Tên milestone hoặc giai đoạn, nếu có","summary_vi":"Tóm tắt tiếng Việt chuẩn","tasks":[{{"title":"việc cần làm","description":"mô tả rõ","owner":"tên người nếu transcript nói rõ, nếu không để rỗng","deadline":"YYYY-MM-DD nếu có, nếu không để rỗng","priority":"High|Medium|Low","milestone":"milestone của task nếu có"}}]}}
Không bịa tên người hoặc deadline. Sửa lỗi chính tả và dịch sang tiếng Việt tự nhiên.
TRANSCRIPT:
{transcript[:12000]}'''
    try:
        result = subprocess.run([CODEX_BIN, "exec", "--ephemeral", "--skip-git-repo-check", prompt], capture_output=True, text=True, timeout=60)
        output = (result.stdout or "").strip()
        match = re.search(r"\{.*\}", output, re.S)
        if match:
            data = json.loads(match.group(0))
            if isinstance(data.get("tasks"), list):
                return data
    except Exception as exc:
        log.warning("Codex extraction unavailable; using fallback: %s", exc)
    return {"project": "Lark meeting project", "summary_vi": transcript[:5000], "tasks": []}

def create_project_and_tasks(event, title, transcript):
    """Create one project and one task per participant for a meeting recap."""
    if not PROJECTS_TABLE_ID or not TASKS_TABLE_ID:
        log.warning("Project/task table IDs are not configured; skipping task creation")
        return
    plan = codex_plan(transcript)
    project_name = plan.get("project") or title or "Lark meeting project"
    milestone = plan.get("milestone") or ""
    project_id = bitable_create(PROJECTS_TABLE_ID, {"Project Name": project_name})
    owners = participant_ids(event)
    if not owners:
        log.warning("No participant open_ids found; project %s created without tasks", project_id)
        return
    tasks = plan.get("tasks") or [{"title": "Xem lại recap cuộc họp", "description": plan.get("summary_vi", transcript or ""), "priority": "Medium"}]
    for index, task in enumerate(tasks, 1):
        owner = owners[(index - 1) % len(owners)]
        task_fields = {
            "Task name": (f"[{task.get('milestone') or milestone}] " if (task.get('milestone') or milestone) else "") + (task.get("title") or f"Follow up meeting recap — {project_name} ({index})"),
            "Owner": [{"id": owner}],
            "Description": (("Milestone: " + (task.get("milestone") or milestone) + "\n") if (task.get("milestone") or milestone) else "") + (task.get("description") or plan.get("summary_vi") or transcript or ""),
        }
        deadline = task.get("deadline") or ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
            task_fields["Deadline"] = int(datetime.datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        if project_id:
            task_fields["Project"] = [{"record_id": project_id}]
        try:
            bitable_create(TASKS_TABLE_ID, task_fields)
        except Exception as exc:
            # A linked-record field may be configured differently across Bases;
            # still create the task with the portable fields.
            log.warning("Task link failed for owner %s, retrying without Project: %s", owner, exc)
            task_fields.pop("Project", None)
            bitable_create(TASKS_TABLE_ID, task_fields)
    log.info("Created project %s and %d participant task(s)", project_id, len(owners))

def oauth_url():
    if not OAUTH_REDIRECT_URI:
        return ""
    params = {
        "app_id": APP_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": "minutes:minutes.basic:read minutes:minutes.transcript:export",
        "state": OAUTH_STATE,
    }
    return f"{DOMAIN}/open-apis/authen/v1/authorize?{urllib.parse.urlencode(params)}"

def exchange_oauth_code(code):
    r = requests.post(
        f"{DOMAIN}/open-apis/authen/v1/access_token",
        json={"grant_type": "authorization_code", "code": code, "app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data", r.json())
    with open(OAUTH_TOKEN_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data

def create_record(event):
    meeting = event.get("meeting", {}) or {}
    owner = (meeting.get("owner") or {}).get("id", {}) or {}
    event_id = event.get("_event_id", "unknown")
    fields = {
        "Meeting Title": meeting.get("topic", "Lark meeting"),
        "Minutes URL": event.get("url", ""),
        "Source Event ID": event_id,
        "Ingestion Status": "Imported — awaiting transcript",
        "AI Processing Status": "Pending",
    }
    if owner.get("open_id"):
        fields["Participants"] = [{"id": owner["open_id"], "type": "text"}]
    fields = {k: v for k, v in fields.items() if v not in ("", None)}
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records"
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base create failed {r.status_code}: {r.text[:500]}")
    log.info("Base record created for event %s", event_id)

def extract_minutes_url(value):
    """Find a forwarded Lark Minutes link anywhere in a message payload."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    match = re.search(r"https?://[^\"\s<>]+(?:minutes|minute)[^\"\s<>]*", text, re.I)
    return match.group(0).rstrip(".,)") if match else ""

def create_forwarded_minutes_record(event, minute_url):
    """Create a Base row when a user forwards the Minutes card/link to this bot."""
    event_id = event.get("_event_id", "unknown")
    # Forwarded cards arrive as a URL rather than a minutes event.  The last
    # path component is the minute token used by the transcript API.
    parsed = urllib.parse.urlparse(minute_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    minute_token = path_parts[-1] if path_parts else ""
    transcript = ""
    if minute_token and minute_token.lower() not in {"minutes", "minute", "min"}:
        try:
            transcript = fetch_transcript(minute_token)
            log.info("Transcript fetched for forwarded Minutes %s", minute_token)
        except Exception as exc:
            log.warning("Transcript fetch failed for forwarded Minutes %s: %s", minute_token, exc)
    fields = {
        "Meeting Title": "Forwarded Lark meeting minutes",
        "Minutes URL": minute_url,
        "Source Event ID": event_id,
        "Ingestion Status": "Imported — awaiting AI" if transcript else "Minutes link received — transcript fetch pending",
        "AI Processing Status": "Pending",
    }
    if transcript:
        fields["Transcript / Raw Recap"] = transcript
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records"
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base create failed {r.status_code}: {r.text[:500]}")
    log.info("Base record created from forwarded Minutes link for event %s", event_id)
    create_project_and_tasks(event, fields["Meeting Title"], transcript)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/oauth/start":
            target = oauth_url()
            if not target:
                self.send_response(500); self.end_headers(); self.wfile.write(b"OAUTH_REDIRECT_URI is not configured"); return
            self.send_response(302); self.send_header("Location", target); self.end_headers(); return
        if parsed.path == "/oauth/callback":
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("state", [""])[0] != OAUTH_STATE:
                self.send_response(400); self.end_headers(); self.wfile.write(b"Invalid OAuth state"); return
            code = query.get("code", [""])[0]
            try:
                exchange_oauth_code(code)
                body = b"Lark OAuth completed. You can close this window."
                self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers(); self.wfile.write(body)
            except Exception as exc:
                log.exception("OAuth exchange failed: %s", exc)
                self.send_response(500); self.end_headers(); self.wfile.write(b"OAuth exchange failed")
            return
        body = b"lark webhook is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
            if "challenge" in payload:
                body = json.dumps({"challenge": payload["challenge"]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body); return
            header = payload.get("header", {})
            event = payload.get("event", {}) or {}
            event["_event_id"] = header.get("event_id", "unknown")
            event_type = header.get("event_type") or header.get("event")
            event["_event_type"] = event_type
            meeting_id = (event.get("meeting") or {}).get("id")
            log.info("Webhook received: event=%s meeting=%s", event_type, meeting_id)

            # The app also receives chat messages.  They are only a health check
            # for the bot unless the user forwards a Lark Minutes link.
            if event_type == "im.message.receive_v1":
                minute_url = extract_minutes_url(event)
                if minute_url:
                    create_forwarded_minutes_record(event, minute_url)
                else:
                    # Keep a bounded sample so rich-card/forwarded-message
                    # payloads can be mapped without logging credentials.
                    log.info("Message received without a Minutes link; payload=%s", json.dumps(event, ensure_ascii=False, default=str)[:3000])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return

            if event_type == "minutes.minute.generated_v1":
                minute_token = find_value(event, {"minute_token", "minutes_token", "token"})
                if not minute_token:
                    log.warning("Minute generated event has no minute token; payload=%s", json.dumps(event, ensure_ascii=False, default=str)[:3000])
                else:
                    transcript = fetch_transcript(minute_token)
                    create_minute_record(event, minute_token, transcript)
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return

            if event_type != "vc.meeting.recording_ready_v1":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return

            log.info("Recording webhook received: meeting=%s", meeting_id)
            create_record(event)
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        except Exception as exc:
            log.exception("Webhook processing failed: %s", exc)
            self.send_response(500); self.end_headers(); self.wfile.write(b"error")
    def log_message(self, fmt, *args):
        return

if __name__ == "__main__":
    port = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))
    log.info("Listening on http://127.0.0.1:%s/lark/events", port)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
