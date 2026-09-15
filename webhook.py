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
import shutil
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
MILESTONES_TABLE_ID = os.getenv("MILESTONES_TABLE_ID")
WORKFLOW_TABLE_ID = os.getenv("WORKFLOW_TABLE_ID")
DELIVERY_TABLE_ID = os.getenv("DELIVERY_TABLE_ID")
MEMBERS_TABLE_ID = os.getenv("MEMBERS_TABLE_ID", "tblsxyYYcbp9dpUS")
PROGRESS_CHAT_ID = os.getenv("PROGRESS_CHAT_ID", "").strip()
PROGRESS_NOTIFY_ENABLED = os.getenv("PROGRESS_NOTIFY_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
DAILY_PROGRESS_CHAT_ID = os.getenv("DAILY_PROGRESS_CHAT_ID", "").strip()
DAILY_PROGRESS_HOUR = int(os.getenv("DAILY_PROGRESS_HOUR", "9"))
DAILY_PROGRESS_ENABLED = os.getenv("DAILY_PROGRESS_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
CODEX_BIN = os.getenv("CODEX_BIN") or shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex"
# Local development uses the authenticated Codex CLI by default.  Hosted
# environments can set AI_PROVIDER explicitly (or fall back to API providers).
AI_PROVIDER = os.getenv("AI_PROVIDER", "codex" if shutil.which("codex") else "auto").strip().lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DOMAIN = os.getenv("LARK_DOMAIN", "https://open.larksuite.com")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "")
OAUTH_STATE = secrets.token_urlsafe(24)
OAUTH_TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".oauth_token.json")
PROCESSED_EVENTS = set()
PROCESSED_EVENTS_LOCK = threading.Lock()
PROCESSED_MINUTES = set()
PROCESSING_MINUTES = set()

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

def refresh_user_token():
    """Refresh the stored Lark OAuth user token when its short TTL expires."""
    try:
        with open(OAUTH_TOKEN_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        refresh_token = saved.get("refresh_token", "")
        if not refresh_token:
            return ""
        r = requests.post(
            f"{DOMAIN}/open-apis/authen/v1/refresh_access_token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "app_id": APP_ID,
                "app_secret": APP_SECRET,
            },
            timeout=20,
        )
        if r.status_code >= 300:
            log.warning("Lark OAuth refresh failed: %s %s", r.status_code, r.text[:300])
            return ""
        body = r.json()
        if body.get("code", 0) not in (0, None):
            log.warning("Lark OAuth refresh rejected: %s", body.get("msg", body.get("code")))
            return ""
        data = body.get("data", body)
        if not data.get("access_token"):
            return ""
        # Keep the newest refresh token and any identity metadata returned by
        # the original authorization response.
        merged = dict(saved)
        merged.update(data)
        with open(OAUTH_TOKEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(merged, fh)
        log.info("Lark OAuth user token refreshed")
        return data["access_token"]
    except Exception as exc:
        log.warning("Lark OAuth refresh unavailable: %s", exc)
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
    params = {"need_speaker": "true", "need_timestamp": "true", "file_format": "txt"}
    r = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=45)
    if r.status_code == 401:
        refreshed = refresh_user_token()
        if refreshed:
            r = requests.get(url, params=params, headers={"Authorization": f"Bearer {refreshed}"}, timeout=45)
    r.raise_for_status()
    if "application/json" in r.headers.get("content-type", ""):
        data = r.json().get("data", r.json())
        return data.get("content", "") if isinstance(data, dict) else str(data)
    # Lark may omit charset on transcript responses.  Decode the bytes as
    # UTF-8 explicitly; requests otherwise guesses latin-1 and corrupts
    # Vietnamese (e.g. "cái" becomes "cÃ¡i").
    return r.content.decode("utf-8", errors="replace")

def fetch_minutes_metadata(minute_token):
    """Return best-effort title/participant metadata for a Minutes item."""
    token = user_token()
    if not token or not minute_token:
        return {}
    url = f"{DOMAIN}/open-apis/minutes/v1/minutes/{urllib.parse.quote(minute_token, safe='')}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code >= 300:
            return {}
        payload = r.json()
        data = payload.get("data", payload)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.info("Minutes metadata unavailable for %s: %s", minute_token, exc)
        return {}

def minutes_title_and_participants(minute_token):
    metadata = fetch_minutes_metadata(minute_token)
    title = find_value(metadata, {"title", "topic", "meeting_title", "name"})
    participants = []
    def walk(value):
        if isinstance(value, dict):
            oid = value.get("open_id")
            if isinstance(oid, str) and oid and oid not in participants:
                participants.append(oid)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(metadata)
    return title, participants

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
    body = r.json()
    if body.get("code", 0) not in (0, None):
        raise RuntimeError(f"Base create failed {body.get('code')}: {body.get('msg', '')}")
    record_id = (body.get("data") or {}).get("record", {}).get("record_id", "")
    log.info("Base record created from generated Minutes %s", minute_token)
    plan = create_project_and_tasks(event, fields["Meeting Title"], transcript)
    if record_id and plan:
        bitable_update(TABLE_ID, record_id, plan_to_meeting_fields(plan))
    send_progress_card(event, plan, fields["Meeting Title"], transcript)

def bitable_create(table_id, fields):
    if not table_id:
        return ""
    # Project template cloning must never send a User-type PIC value.  This
    # guard protects older branches/templates from producing UserFieldConvFail;
    # meeting task creation assigns PIC explicitly after transcript parsing.
    if table_id == DELIVERY_TABLE_ID and isinstance(fields, dict) and fields.get("Template Source") == "21-day master":
        fields = dict(fields)
        fields.pop("PIC", None)
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base create failed {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("code", 0) not in (0, None):
        raise RuntimeError(f"Base create failed {body.get('code')}: {body.get('msg', '')}")
    return (body.get("data") or {}).get("record", {}).get("record_id", "")

def bitable_list(table_id, page_size=500):
    """Read Base records for matching existing projects and workflow templates."""
    if not table_id:
        return []
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    r = requests.get(url, params={"page_size": page_size}, headers={"Authorization": f"Bearer {tenant_token()}"}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base list failed {r.status_code}: {r.text[:500]}")
    return (r.json().get("data") or {}).get("items", [])

def field_text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(field_text(item) for item in value).strip(", ")
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("value") or "").strip()
    return str(value or "").strip()

def pic_field(open_id):
    """Encode an open_id for the Delivery Plan User-type PIC field."""
    return [{"id": open_id}] if isinstance(open_id, str) and open_id.startswith("ou_") else []

def resolve_pic(task_text="", milestone="", explicit_owner="", fallback_owner=""):
    """Resolve a PIC from Members & PIC using name/alias/keyword context."""
    context = normalize_name(f"{task_text} {milestone}")
    try:
        members = []
        for row in bitable_list(MEMBERS_TABLE_ID):
            f = row.get("fields") or {}
            active = f.get("Active")
            if active is False or str(active).casefold() in {"false", "0", "no"}:
                continue
            users = f.get("Lark User") or []
            uid = ""
            if isinstance(users, list) and users and isinstance(users[0], dict):
                uid = users[0].get("id") or users[0].get("open_id") or ""
            uid = uid or (f.get("Lark User") if isinstance(f.get("Lark User"), str) else "")
            if not uid:
                continue
            name = field_text(f.get("Member Name")); aliases = field_text(f.get("Aliases"))
            if explicit_owner and normalize_name(explicit_owner) in normalize_name(f"{name} {aliases}"):
                return uid
            keywords = normalize_name(f.get("Assignment Keywords"))
            score = sum(1 for token in set(context.split()) if token and token in keywords.split())
            role = normalize_name(f.get("Role / Function"))
            # Delivery PIC is the preferred operational fallback; PM is the
            # second fallback when no more specific keyword is present.
            role_score = 2 if "project pic" in role else 1 if "project manager" in role else 0
            members.append((score, role_score, uid))
        if members:
            members.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return members[0][2]
    except Exception as exc:
        log.warning("Members & PIC lookup failed: %s", exc)
    return explicit_owner if explicit_owner.startswith("ou_") else fallback_owner

def stage_for_milestone(milestone):
    """Canonical iKame 21-day stage labels used by Delivery Plan."""
    key = normalize_name(milestone)
    return {
        "art style": "Art & Concept",
        "complete prototype": "Prototype",
        "core game": "Core Game",
        "prototype handover + testing": "QA & Go-live",
        "xây dựng tính năng mới": "Core Game",
    }.get(key, milestone or "")

def clone_workflow_template(project_id, project_name, owner=""):
    """Instantiate the 21-day template for a newly created project."""
    if not WORKFLOW_TABLE_ID or not project_id:
        return
    rows = bitable_list(WORKFLOW_TABLE_ID)
    # The Base UI may return records in insertion order after a migration.
    # Always execute the master template chronologically so onboarding and
    # day-based work are cloned in the intended 21-day sequence.
    def _template_order(row):
        f = row.get("fields") or {}
        try: offset = float(f.get("Offset Day") or 0)
        except (TypeError, ValueError): offset = 0
        try: sequence = float(f.get("Sequence") or 0)
        except (TypeError, ValueError): sequence = 0
        return (offset, sequence, normalize_name(f.get("Milestone")), normalize_name(f.get("Task Template")))
    rows.sort(key=_template_order)
    # New CRM design: one operational table, linked to the project, with a
    # self-referencing Parent Item hierarchy.  This keeps every milestone and
    # task visible in one grouped view while retaining the old tables as a
    # compatibility fallback during migration.
    if DELIVERY_TABLE_ID:
        # Automation retries must be idempotent.  Do not clone the same
        # template into a project that already has delivery items.
        existing_rows = bitable_list(DELIVERY_TABLE_ID)
        project_key = normalize_name(project_name)
        if any(normalize_name((r.get("fields") or {}).get("Project")) == project_key for r in existing_rows):
            log.info("Delivery template already exists for %s; skipping clone", project_name)
            return
        created = {}
        for row in rows:
            f = row.get("fields") or {}
            milestone = field_text(f.get("Milestone")) or field_text(f.get("Phase"))
            task_title = field_text(f.get("Task Template"))
            if not milestone and not task_title:
                continue
            milestone_id = created.get(milestone)
            if milestone and not milestone_id:
                mf = {
                    "Item Name": milestone,
                    "Item Type": "Milestone",
                    "Project": project_name,
                    "Milestone Group": milestone,
                    "Status": "Upcoming",
                    "Sequence": f.get("Sequence") or 0,
                    "Template Source": "21-day master",
                    "Progress %": 0,
                    "Stage": field_text(f.get("Stage")) or stage_for_milestone(milestone),
                }
                # Do not populate PIC while cloning a new project's template.
                # Template rows must be created without any User field; PIC is
                # assigned later when a meeting task is extracted.
                if project_id:
                    mf["Project Link"] = [project_id]
                offset = f.get("Offset Day")
                duration = f.get("Duration Days")
                try:
                    if offset is not None:
                        start_ms = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=float(offset))).timestamp() * 1000)
                        mf["Start Date"] = start_ms
                        if duration is not None:
                            mf["Due Date"] = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=float(offset) + float(duration))).timestamp() * 1000)
                except (TypeError, ValueError):
                    log.warning("Ignoring invalid template dates for milestone %s", milestone)
                milestone_id = bitable_create(DELIVERY_TABLE_ID, mf)
                created[milestone] = milestone_id
            if task_title:
                tf = {
                    "Item Name": task_title,
                    "Item Type": "Task",
                    "Project": project_name,
                    "Milestone Group": milestone,
                    "Status": "Upcoming",
                    "Sequence": f.get("Sequence") or 0,
                    "Priority": field_text(f.get("Default Priority")) or "Medium",
                    "Template Source": "21-day master",
                    "Progress %": 0,
                    "Stage": stage_for_milestone(milestone),
                }
                # Likewise, leave template task PIC unset; meeting processing
                # resolves the responsible member from Base 04.
                if project_id:
                    tf["Project Link"] = [project_id]
                offset = f.get("Offset Day")
                duration = f.get("Duration Days")
                try:
                    if offset is not None:
                        start_ms = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=float(offset))).timestamp() * 1000)
                        tf["Start Date"] = start_ms
                        if duration is not None:
                            tf["Due Date"] = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=float(offset) + float(duration))).timestamp() * 1000)
                except (TypeError, ValueError):
                    log.warning("Ignoring invalid template dates for task %s", task_title)
                if milestone_id:
                    tf["Parent Item"] = milestone
                try:
                    bitable_create(DELIVERY_TABLE_ID, tf)
                except Exception as exc:
                    log.warning("Delivery task parent link failed, retrying without Parent Item: %s", exc)
                    tf.pop("Parent Item", None); bitable_create(DELIVERY_TABLE_ID, tf)
        log.info("Cloned %d workflow row(s) into Delivery Plan for %s", len(rows), project_name)
        return
    if not MILESTONES_TABLE_ID or not project_id:
        return
    for row in rows:
        f = row.get("fields") or {}
        milestone = field_text(f.get("Milestone")) or field_text(f.get("Phase"))
        if not milestone:
            continue
        milestone_fields = {
            "Milestone / Phase": milestone,
            "Project": [{"record_id": project_id}],
            "Phase Type": field_text(f.get("Phase")) or "Production",
            "Sequence": f.get("Sequence") or 0,
            "Status": "Upcoming",
            "PIC": owner,
            "Go-live Gate": field_text(f.get("Go-live Gate")) or "No",
            "Progress %": 0,
            "Stage": field_text(f.get("Stage")) or stage_for_milestone(milestone),
            "Next Action": field_text(f.get("Task Template")) or "Start milestone",
        }
        try:
            milestone_id = bitable_create(MILESTONES_TABLE_ID, milestone_fields)
        except Exception as exc:
            log.warning("Milestone project link failed, retrying as text: %s", exc)
            milestone_fields["Project"] = project_name
            milestone_id = bitable_create(MILESTONES_TABLE_ID, milestone_fields)
        task_title = field_text(f.get("Task Template"))
        if TASKS_TABLE_ID and task_title:
            task_fields = {
                "Task name": task_title,
                "Owner": ([{"id": owner}] if owner else []),
                "Description": f"Template 21 ngày — {project_name} — {milestone}",
                "Project": [{"record_id": project_id}],
            }
            offset = f.get("Offset Day")
            duration = f.get("Duration Days")
            if isinstance(offset, (int, float)):
                start = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=int(offset))
                task_fields["Start Date"] = int(start.timestamp() * 1000)
                if isinstance(duration, (int, float)):
                    task_fields["Deadline"] = int((start + datetime.timedelta(days=int(duration))).timestamp() * 1000)
            try:
                bitable_create(TASKS_TABLE_ID, task_fields)
            except Exception as exc:
                log.warning("Template task link failed, retrying without Project: %s", exc)
                task_fields.pop("Project", None)
                bitable_create(TASKS_TABLE_ID, task_fields)
    log.info("Cloned %d workflow template row(s) for project %s", len(rows), project_name)

def find_project_id(project_name):
    target = re.sub(r"\s+", " ", (project_name or "").strip().casefold())
    if not target:
        return ""
    for row in bitable_list(PROJECTS_TABLE_ID):
        current = field_text((row.get("fields") or {}).get("Project Name"))
        if re.sub(r"\s+", " ", current.casefold()) == target:
            return row.get("record_id", "")
    return ""

def latest_project_record():
    rows = bitable_list(PROJECTS_TABLE_ID)
    def timestamp(row):
        for key in ("created_time", "last_modified_time"):
            value = row.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                try:
                    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
        return 0.0
    # The API normally supplies timestamps; when it does not, list order is
    # the only reliable signal and the last record is the newly added one.
    return max(enumerate(rows), key=lambda item: (timestamp(item[1]), item[0]))[1] if rows else {}

def handle_project_created(event):
    """Clone the standard workflow after a Base Project record is created."""
    fields = event.get("fields") or event.get("record") or event.get("data") or event
    project_name = field_text(fields.get("Project Name")) if isinstance(fields, dict) else ""
    project_name = project_name or find_value(event, {"project_name", "name", "Project Name"})
    record_id = ""
    if isinstance(event, dict):
        record_id = str(event.get("record_id") or event.get("recordId") or "")
        record = event.get("record")
        if isinstance(record, dict):
            record_id = record_id or str(record.get("record_id") or record.get("recordId") or "")
    record_id = record_id or find_value(event, {"project_record_id", "record_id"})
    if not record_id and project_name:
        record_id = find_project_id(project_name)
    if not record_id and not project_name:
        latest = latest_project_record()
        record_id = latest.get("record_id", "")
        project_name = field_text((latest.get("fields") or {}).get("Project Name"))
    owner = find_value(event, {"owner_open_id", "open_id"})
    if not project_name or not record_id:
        raise RuntimeError("Project-created event must include Project Name and record_id")
    clone_workflow_template(record_id, project_name, owner)
    log.info("Project-created event processed for %s", project_name)

def sync_project_from_delivery(project_name):
    """Roll up Delivery Plan progress into the matching Projects row."""
    if not project_name or not PROJECTS_TABLE_ID or not DELIVERY_TABLE_ID:
        return
    rows = [r for r in bitable_list(DELIVERY_TABLE_ID) if field_text((r.get("fields") or {}).get("Project")).casefold() == project_name.casefold()]
    if not rows:
        return
    done = {"done", "completed", "complete"}
    progress = []
    statuses = []
    for row in rows:
        f = row.get("fields") or {}
        statuses.append(field_text(f.get("Status")).casefold())
        value = f.get("Progress %")
        try:
            if value not in (None, ""): progress.append(float(value))
        except (TypeError, ValueError):
            pass
    pct = round(sum(progress) / len(progress)) if progress else round(sum(s in done for s in statuses) / len(statuses) * 100)
    status = "Done" if statuses and all(s in done for s in statuses) else "In Progress" if any(s in {"in progress", "doing", "open"} for s in statuses) else "Upcoming"
    active = next((r for r in rows if field_text((r.get("fields") or {}).get("Status")).casefold() not in done), rows[0])
    af = active.get("fields") or {}
    current_milestone = field_text(af.get("Milestone Group")) or field_text(af.get("Parent Item"))
    current_stage = field_text(af.get("Stage")) or stage_for_milestone(current_milestone)
    next_action = field_text(af.get("Next Action"))
    project_id = find_project_id(project_name)
    if not project_id:
        return
    # These fields are optional during migration; retry with only fields that
    # are present in the Projects table if the Base schema is still old.
    try:
        # Progress % is a Base formula (average of Delivery Plan progress,
        # normalized from 0–100 to 0–1); never write into the formula field.
        bitable_update(PROJECTS_TABLE_ID, project_id, {"Status": status, "Current Milestone": current_milestone, "Current Stage": current_stage, "Next Action": next_action, "Last Updated": int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)})
    except Exception as exc:
        log.warning("Project roll-up update skipped for %s (add Progress %%/Last Updated fields if needed): %s", project_name, exc)
    log.info("Synced project %s progress to %s%% (%s)", project_name, pct, status)

def bitable_update(table_id, record_id, fields):
    if not table_id or not record_id or not fields:
        return
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    r = requests.put(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base update failed {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("code", 0) not in (0, None):
        raise RuntimeError(f"Base update failed {body.get('code')}: {body.get('msg', '')}")

def plan_to_meeting_fields(plan):
    tasks = flatten_plan_tasks(plan)
    items = []
    for task in tasks:
        items.append("Task: {0} | PIC: {1} | Due: {2} | Priority: {3} | Milestone: {4}".format(
            task.get("title") or "NEEDS_REVIEW",
            task.get("owner") or "NEEDS_REVIEW",
            task.get("deadline") or "NEEDS_REVIEW",
            task.get("priority") or "Medium",
            task.get("milestone") or "",
        ))
    decisions = plan.get("decisions") or []
    decisions_text = decisions if isinstance(decisions, str) else "\n".join(str(item) for item in decisions)
    return {
        "AI Summary": plan.get("summary_vi") or plan.get("meeting_summary_vi") or "",
        "Key Decisions": decisions_text,
        "Extracted Action Items": "\n".join(items),
        "AI Processing Status": "AI Draft Generated",
        "Ingestion Status": "Imported — AI processed",
    }

def _progress_chat_id(event):
    global DAILY_PROGRESS_CHAT_ID
    message = event.get("message") or {}
    chat_id = message.get("chat_id") or message.get("chatid") or PROGRESS_CHAT_ID
    if chat_id and not DAILY_PROGRESS_CHAT_ID:
        DAILY_PROGRESS_CHAT_ID = chat_id
        log.info("Using originating group %s for daily progress notifications", chat_id)
    return chat_id or DAILY_PROGRESS_CHAT_ID

def send_progress_card(event, plan, source_title="", transcript=""):
    """Post a compact Vietnamese progress card to the originating Lark group."""
    if not PROGRESS_NOTIFY_ENABLED or not isinstance(plan, dict):
        return False
    chat_id = _progress_chat_id(event)
    if not chat_id:
        log.info("Progress notification skipped: no group chat_id configured")
        return False
    projects = plan.get("projects") if isinstance(plan.get("projects"), list) else [plan]
    lines = []
    for project in projects[:8]:
        name = project.get("project") or project.get("name") or source_title or "Dự án"
        status = project.get("status") or "Chưa cập nhật"
        health = project.get("health") or "Unknown"
        progress = project.get("progress_percent")
        progress_text = f"{progress}%" if progress is not None else "—"
        next_step = project.get("next_step") or "Chưa có next action"
        lines.append(f"**{name}** · {progress_text} · {status} ({health})\nNext: {next_step}")
    summary = plan.get("summary_vi") or plan.get("meeting_summary_vi") or "Đã cập nhật từ meeting minutes."
    decisions = plan.get("decisions") or plan.get("key_decisions") or []
    decisions_text = decisions if isinstance(decisions, str) else "\n".join(f"• {x}" for x in decisions[:8])
    action_lines = []
    all_tasks = flatten_plan_tasks(plan)
    if isinstance(plan.get("tasks"), list): all_tasks.extend(plan.get("tasks") or [])
    for task in all_tasks[:12]:
        action_lines.append(f"• **{task.get('title') or task.get('task') or 'Đầu việc'}** · {task.get('owner') or 'Chưa giao'} · {task.get('deadline') or 'Chưa có hạn'} · {task.get('status') or 'Open'} · {task.get('priority') or 'Medium'}")
    transcript_excerpt = (transcript or plan.get("transcript") or "").strip()
    if len(transcript_excerpt) > 1400: transcript_excerpt = transcript_excerpt[:1400].rstrip() + "…"
    card = {"config": {"wide_screen_mode": True}, "header": {"template": "blue", "title": {"tag": "plain_text", "content": "📊 Cập nhật tiến độ dự án"}}, "elements": [
        {"tag": "markdown", "content": f"**Meeting:** {source_title or 'Meeting minutes'}\n{summary[:800]}"},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n\n".join(lines) if lines else "Chưa trích xuất được tiến độ."},
        {"tag": "markdown", "content": "**📝 Transcript / nội dung cuộc họp**\n" + (transcript_excerpt or "Không có transcript")},
        {"tag": "markdown", "content": "**✅ Quyết định chính**\n" + (decisions_text or "Không có quyết định được trích xuất")},
        {"tag": "markdown", "content": "**📋 Action items chi tiết**\n" + ("\n".join(action_lines) if action_lines else "Không có action item")},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": "Tự động từ webhook + Codex CLI"}]},
    ]}
    url = f"{DOMAIN}/open-apis/im/v1/messages?receive_id_type=chat_id"
    payload = {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json=payload, timeout=20)
    if r.status_code >= 300:
        log.warning("Progress card send failed %s: %s", r.status_code, r.text[:300])
        return False
    log.info("Progress card sent to chat %s", chat_id)
    return True

def send_daily_progress_card():
    """Send a detailed, executive-style daily report to the configured group."""
    if not DAILY_PROGRESS_ENABLED or not DAILY_PROGRESS_CHAT_ID or not DELIVERY_TABLE_ID: return False
    rows = bitable_list(DELIVERY_TABLE_ID)
    done_status = {"done", "completed", "complete"}
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).date()
    projects, overdue, due_soon, blockers, next_actions = {}, [], [], [], []
    for row in rows:
        f = row.get("fields") or {}; project = field_text(f.get("Project")) or "Chưa gán dự án"
        item = field_text(f.get("Item Name")) or "Đầu việc"; status = field_text(f.get("Status")) or "Open"
        p = projects.setdefault(project, {"rows": [], "done": 0, "progress": []}); p["rows"].append(f)
        if status.casefold() in done_status: p["done"] += 1
        try:
            if f.get("Progress %") not in (None, ""): p["progress"].append(int(float(f["Progress %"])))
        except (TypeError, ValueError): pass
        due = f.get("Due Date") or f.get("Deadline"); due_date = None
        if isinstance(due, (int, float)): due_date = datetime.datetime.fromtimestamp(due / 1000, datetime.timezone.utc).date()
        elif isinstance(due, str):
            try: due_date = datetime.date.fromisoformat(due[:10])
            except ValueError: pass
        pic = field_text(f.get("PIC")) or "Chưa giao"
        if due_date and status.casefold() not in done_status:
            line = f"{project} · {item} · PIC: {pic} · {due_date.strftime('%d/%m')}"
            (overdue if due_date < today else due_soon if due_date <= today + datetime.timedelta(days=2) else []).append(line)
        risk = field_text(f.get("Risk / Blocker"))
        if risk: blockers.append(f"{project} · {item}: {risk}")
        nxt = field_text(f.get("Next Action"))
        if nxt and status.casefold() not in done_status: next_actions.append(f"{project} · {nxt} · {pic}")
    total = sum(len(p["rows"]) for p in projects.values()); done = sum(p["done"] for p in projects.values())
    elements = [{"tag": "markdown", "content": f"**{today.strftime('%A, %d/%m/%Y')}**\n**{len(projects)}** dự án · **{total}** đầu việc · **{done}/{total}** hoàn tất · **{len(overdue)}** quá hạn"}, {"tag": "hr"}]
    elements.append({"tag": "markdown", "content": "**📌 Tổng quan theo dự án**"})
    for name, p in list(projects.items())[:12]:
        avg = round(sum(p["progress"]) / len(p["progress"])) if p["progress"] else round(p["done"] / len(p["rows"]) * 100) if p["rows"] else 0
        milestones = sum(1 for r in p["rows"] if field_text(r.get("Item Type")).casefold() == "milestone")
        active = sum(1 for r in p["rows"] if field_text(r.get("Status")).casefold() not in done_status)
        risk = sum(1 for r in p["rows"] if field_text(r.get("Risk / Blocker")))
        elements.append({"tag": "markdown", "content": f"**{name}**  ·  **{avg}%** tiến độ\nMilestone: {milestones} · Đang làm: {active} · Hoàn tất: {p['done']} · Risk: {risk}"})
    def section(title, values): return {"tag": "markdown", "content": f"**{title}**\n" + ("\n".join(f"• {v}" for v in values[:10]) if values else "Không có")}
    elements += [{"tag": "hr"}, section("🔴 Việc quá hạn", overdue), section("🟡 Hạn trong 2 ngày", due_soon), section("⚠️ Risk / Blocker", blockers), section("➡️ Next action & PIC", next_actions), {"tag": "note", "elements": [{"tag": "plain_text", "content": "Nguồn: Delivery Plan · Tự động lúc 09:00 ICT mỗi ngày"}]}]
    card = {"config": {"wide_screen_mode": True}, "header": {"template": "blue", "title": {"tag": "plain_text", "content": "☀️ Daily Project Report"}}, "elements": elements}
    url = f"{DOMAIN}/open-apis/im/v1/messages?receive_id_type=chat_id"
    payload = {"receive_id": DAILY_PROGRESS_CHAT_ID, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json=payload, timeout=20)
    if r.status_code >= 300:
        log.warning("Daily progress card send failed %s: %s", r.status_code, r.text[:300]); return False
    log.info("Daily progress card sent to chat %s", DAILY_PROGRESS_CHAT_ID)
    return True

def daily_progress_loop():
    sent_date = None
    tz = datetime.timezone(datetime.timedelta(hours=7))
    while True:
        now = datetime.datetime.now(tz)
        if DAILY_PROGRESS_ENABLED and DAILY_PROGRESS_CHAT_ID and now.hour == DAILY_PROGRESS_HOUR and sent_date != now.date():
            try: send_daily_progress_card()
            except Exception: log.exception("Daily progress notification failed")
            sent_date = now.date()
        threading.Event().wait(45)

def normalize_name(value):
    return re.sub(r"\s+", " ", field_text(value).casefold()).strip()

def update_delivery_progress(plan, meeting_title=""):
    """Update existing Delivery Plan items from portfolio AI output."""
    if not DELIVERY_TABLE_ID or not isinstance(plan, dict):
        return
    rows = bitable_list(DELIVERY_TABLE_ID)
    index = {(normalize_name((r.get("fields") or {}).get("Project")), normalize_name((r.get("fields") or {}).get("Item Name"))): r for r in rows}
    for project in plan.get("projects") or []:
        project_name = project.get("project") or project.get("name") or ""
        pkey = normalize_name(project_name)
        for milestone in project.get("milestones") or []:
            milestone_name = milestone.get("milestone") or milestone.get("name") or ""
            key = (pkey, normalize_name(milestone_name))
            existing = index.get(key)
            if not existing:
                continue
            fields = {
                "Status": milestone.get("status") or "In Progress",
                "Next Action": milestone.get("next_step") or "",
                "Source Meeting": meeting_title,
                "AI Confidence": "High" if milestone_name else "Low",
                "Needs Review": "No",
                "Stage": stage_for_milestone(milestone_name),
            }
            if milestone.get("progress_percent") is not None:
                fields["Progress %"] = milestone.get("progress_percent")
            if milestone.get("risk"):
                fields["Risk / Blocker"] = milestone.get("risk")
            try:
                bitable_update(DELIVERY_TABLE_ID, existing.get("record_id", ""), fields)
            except Exception as exc:
                log.warning("Delivery milestone update failed; applying core fields: %s", exc)
                core = {k: fields[k] for k in ("Status", "Progress %") if k in fields}
                if core:
                    try: bitable_update(DELIVERY_TABLE_ID, existing.get("record_id", ""), core)
                    except Exception as inner: log.warning("Delivery core update failed: %s", inner)
            for task in milestone.get("tasks") or []:
                title = str(task.get("title") or "").strip()
                if not title:
                    continue
                tkey = (pkey, normalize_name(title))
                task_row = index.get(tkey)
                fields = {
                    "Status": task.get("status") or "Open",
                    "PIC": pic_field(resolve_pic(title, milestone_name, task.get("owner") or "", fallback_owner="")),
                    "Priority": task.get("priority") or "Medium",
                    "Next Action": task.get("next_step") or "",
                    "Source Meeting": meeting_title,
                    "AI Confidence": "High",
                    "Needs Review": "No",
                    "Stage": stage_for_milestone(milestone_name),
                }
                if task.get("progress_percent") is not None:
                    fields["Progress %"] = task.get("progress_percent")
                if task.get("risk"):
                    fields["Risk / Blocker"] = task.get("risk")
                if task_row:
                    try: bitable_update(DELIVERY_TABLE_ID, task_row.get("record_id", ""), fields)
                    except Exception as exc:
                        log.warning("Delivery task update failed; applying core fields: %s", exc)
                        core = {k: fields[k] for k in ("Status", "PIC", "Priority", "Progress %") if k in fields}
                        if core:
                            try: bitable_update(DELIVERY_TABLE_ID, task_row.get("record_id", ""), core)
                            except Exception as inner: log.warning("Delivery task core update failed: %s", inner)

def flatten_plan_tasks(plan):
    """Flatten portfolio-style AI output while keeping legacy output compatible."""
    if isinstance(plan.get("projects"), list):
        flattened = []
        for project in plan["projects"]:
            project_name = project.get("project") or project.get("name") or ""
            for milestone in project.get("milestones") or []:
                milestone_name = milestone.get("milestone") or milestone.get("name") or ""
                for task in milestone.get("tasks") or []:
                    item = dict(task)
                    item.setdefault("project", project_name)
                    item.setdefault("milestone", milestone_name)
                    flattened.append(item)
            for task in project.get("tasks") or []:
                item = dict(task)
                item.setdefault("project", project_name)
                flattened.append(item)
        return flattened
    return list(plan.get("tasks") or [])

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

def available_project_context():
    """Return the live Project → Milestone → Stage map for AI classification."""
    try:
        projects = bitable_list(PROJECTS_TABLE_ID)
        delivery = bitable_list(DELIVERY_TABLE_ID) if DELIVERY_TABLE_ID else []
        by_project = {}
        for row in delivery:
            f = row.get("fields") or {}
            project = field_text(f.get("Project"))
            milestone = field_text(f.get("Milestone Group")) or field_text(f.get("Parent Item"))
            stage = field_text(f.get("Stage"))
            if project and milestone:
                by_project.setdefault(normalize_name(project), {}).setdefault(milestone, stage)
        lines = []
        for row in projects:
            f = row.get("fields") or {}
            name = field_text(f.get("Project Name"))
            if not name:
                continue
            milestones = by_project.get(normalize_name(name), {})
            items = "; ".join(f"{m} [Stage: {s or 'chưa xác định'}]" for m, s in milestones.items())
            lines.append(f"- Project: {name} | Milestones: {items or 'chưa có milestone'}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("Unable to load project context for AI classification: %s", exc)
        return ""

def extraction_prompt(transcript, project_context=""):
    """Prompt shared by the hosted API and the optional local Codex fallback."""
    return f'''Đọc transcript cuộc họp dưới đây. Trả về DUY NHẤT JSON hợp lệ, không markdown:
{{"meeting_summary_vi":"Tóm tắt toàn bộ cuộc họp bằng tiếng Việt chuẩn","decisions":["quyết định quan trọng"],"projects":[{{"project":"Tên project đúng như transcript","status":"Current status","health":"On track|At risk|Blocked|Unknown","progress_percent":0,"summary_vi":"Tình hình project","next_step":"Bước tiếp theo của project","milestones":[{{"milestone":"Tên milestone","status":"Current milestone status","progress_percent":0,"risk":"Rủi ro hoặc blocker, nếu có","next_step":"Bước tiếp theo của milestone","tasks":[{{"title":"việc cần làm","description":"mô tả rõ","owner":"tên người nếu nói rõ, nếu không để rỗng","deadline":"YYYY-MM-DD nếu có, nếu không để rỗng","status":"Open|In Progress|Done|Blocked","progress_percent":0,"next_step":"Bước tiếp theo","risk":"Rủi ro nếu có","priority":"High|Medium|Low"}}]}}]}}]}}
QUAN TRỌNG: Tiêu đề meeting không phải tên project. Hãy phân loại nội dung vào đúng Project và Milestone trong danh sách Base bên dưới. Chỉ dùng đúng tên Project/Milestone có trong danh sách; không tạo project mới và không dùng tên meeting như "meeting daily" làm project. Nếu transcript không nói rõ project nhưng danh sách chỉ có một project, chọn project duy nhất đó. Mỗi task phải nằm trong milestone phù hợp; nếu chưa khớp chắc chắn, chọn milestone gần nhất và đánh dấu risk/Needs Review trong nội dung task.
XỬ LÝ TIẾNG VIỆT: Transcript có thể là ASR nên thiếu dấu câu, lặp từ hoặc nghe nhầm. Hãy ghép lại câu hoàn chỉnh, sửa lỗi chính tả/dấu tiếng Việt và diễn đạt tự nhiên theo ngữ cảnh công việc. Giữ nguyên tên người, tên dự án, mã sản phẩm và thuật ngữ kỹ thuật (ví dụ CIM, SDK, Firebase, ASO), không tự dịch tên riêng. Không suy diễn phần âm thanh không rõ; nếu không chắc hãy ghi rõ "Chưa xác định" trong risk/summary.
Trong nghiệp vụ CRM này, nếu ASR/AI ghi "CIM" nhưng ngữ cảnh nói về hệ thống quản lý khách hàng/UpLark thì phải chuẩn hóa thành "CRM".
PROJECT CONTEXT TỪ LARK BASE:
{project_context or '(không đọc được context — không được tự bịa project)'}
Không bịa tên người hoặc deadline. Tất cả summary, decision, milestone, task, next_step và risk phải viết bằng tiếng Việt chuẩn, ngắn gọn nhưng đủ ý.
TRANSCRIPT:
{transcript[:50000]}'''

def parse_plan_output(output):
    """Find the last valid plan object in CLI/API output that may contain logs."""
    if not output:
        return None
    decoder = json.JSONDecoder()
    candidates = []
    for match in re.finditer(r"\{", output):
        try:
            value, _ = decoder.raw_decode(output[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (isinstance(value.get("projects"), list) or isinstance(value.get("tasks"), list)):
            candidates.append(value)
    return candidates[-1] if candidates else None

def normalize_business_terms(value):
    """Correct recurring Vietnamese ASR confusions for this CRM workflow."""
    if isinstance(value, str):
        return re.sub(r"\bCIM\b", "CRM", value, flags=re.IGNORECASE)
    if isinstance(value, list):
        return [normalize_business_terms(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_business_terms(item) for key, item in value.items()}
    return value

def openai_plan(transcript, project_context=""):
    """Use OpenAI Responses API for production transcript extraction."""
    if not OPENAI_API_KEY or not transcript:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=extraction_prompt(transcript, project_context),
            store=False,
        )
        output = (response.output_text or "").strip()
        data = parse_plan_output(output)
        if data:
            log.info("Transcript extracted with OpenAI model %s", OPENAI_MODEL)
            return data
    except Exception as exc:
        log.warning("OpenAI extraction unavailable; trying Codex fallback: %s", exc)
    return None

def gemini_plan(transcript, project_context=""):
    """Use Gemini generateContent when a Gemini key is configured."""
    if not GEMINI_API_KEY or not transcript:
        return None
    try:
        body = {
            "contents": [{"parts": [{"text": extraction_prompt(transcript, project_context)}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        model_path = urllib.parse.quote(GEMINI_MODEL, safe='')
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_path}:generateContent",
            params={"key": GEMINI_API_KEY}, json=body, timeout=60,
        )
        # Some Google API keys expose the model through v1 rather than v1beta.
        if response.status_code == 404:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1/models/{model_path}:generateContent",
                params={"key": GEMINI_API_KEY}, json=body, timeout=60,
            )
        response.raise_for_status()
        data = response.json()
        output = data["candidates"][0]["content"]["parts"][0].get("text", "").strip()
        plan = parse_plan_output(output)
        if plan:
            log.info("Transcript extracted with Gemini model %s", GEMINI_MODEL)
            return plan
    except Exception as exc:
        log.warning("Gemini extraction unavailable; trying OpenAI/Codex fallback: %s", exc)
    return None

def codex_plan(transcript, project_context=""):
    """Normalize Vietnamese transcript and extract a strict project/task plan."""
    if not transcript:
        return {"project": "Lark meeting project", "tasks": []}
    prompt = extraction_prompt(transcript, project_context)
    try:
        result = subprocess.run(
            [CODEX_BIN, "exec", "--ephemeral", "--skip-git-repo-check", "-"],
            input=prompt, capture_output=True, text=True, timeout=180,
        )
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        data = parse_plan_output(output)
        if data:
            log.info("Transcript extracted with local Codex CLI")
            return data
    except Exception as exc:
        log.warning("Codex extraction unavailable; using fallback: %s", exc)
    return {"project": "Lark meeting project", "summary_vi": transcript[:5000], "tasks": []}

def _match_existing_project(project_name, title, transcript):
    """Map AI output to an existing Project; never invent a project name."""
    rows = bitable_list(PROJECTS_TABLE_ID)
    candidates = [(r.get("record_id", ""), field_text((r.get("fields") or {}).get("Project Name"))) for r in rows]
    candidates = [(rid, name) for rid, name in candidates if rid and name]
    if not candidates:
        return "", ""
    target = re.sub(r"[^\w\s]+", " ", (project_name or "").casefold())
    target_tokens = set(target.split())
    # Start below zero so a valid single-project candidate with zero token
    # overlap is still retained for the unambiguous fallback below.
    best = (-1.0, "", "")
    context = f"{title or ''} {transcript[:1200] or ''}".casefold()
    for rid, name in candidates:
        norm = re.sub(r"[^\w\s]+", " ", name.casefold())
        tokens = set(norm.split())
        score = 1.0 if norm == target.strip() else (len(target_tokens & tokens) / max(1, len(target_tokens | tokens)))
        if name.casefold() in context: score += 0.35
        if score > best[0]: best = (score, rid, name)
    # If there is only one live project, it is the safe meeting context.
    if best[0] >= 0.20 or len(candidates) == 1:
        return best[1], best[2]
    return "", ""

def create_project_and_tasks(event, title, transcript, allow_project_create=False):
    """Create a project and one Base task per extracted action item.

    The webhook is the single owner of task creation.  AI-provided owners are
    used only when they are already Lark open_ids; otherwise we fall back to
    the meeting owner/participant so a task is never silently dropped.
    """
    if not PROJECTS_TABLE_ID or (not TASKS_TABLE_ID and not DELIVERY_TABLE_ID):
        log.warning("Project/task table IDs are not configured; skipping task creation")
        return {"project": title, "tasks": []}
    project_context = available_project_context()
    providers = {
        "codex": [codex_plan],
        "gemini": [gemini_plan],
        "openai": [openai_plan],
        "auto": [gemini_plan, openai_plan, codex_plan],
    }.get(AI_PROVIDER, [codex_plan])
    plan = next((candidate for provider in providers if (candidate := provider(transcript, project_context))), None)
    plan = normalize_business_terms(plan or {"project": title, "tasks": []})
    # A meeting transcript may contain only a meeting title and a milestone,
    # with no explicit project name. Resolve that missing project from Base
    # before flattening tasks; the title must never become a project record.
    if not plan.get("projects") and not plan.get("project"):
        only_project = latest_project_record()
        only_name = field_text((only_project.get("fields") or {}).get("Project Name"))
        if only_project.get("record_id") and only_name:
            plan["project"] = only_name
    owners = participant_ids(event)
    meeting_owner = ((event.get("meeting") or {}).get("owner") or {}).get("id", {})
    fallback_owner = meeting_owner.get("open_id") if isinstance(meeting_owner, dict) else ""
    fallback_owner = fallback_owner or (owners[0] if owners else "")
    projects = plan.get("projects") if isinstance(plan.get("projects"), list) else [{"project": plan.get("project") or title, "milestone": plan.get("milestone"), "tasks": plan.get("tasks") or []}]
    total_tasks = 0
    for project in projects:
        project_name = project.get("project") or project.get("name") or title or "Lark meeting project"
        project_id = find_project_id(project_name)
        if project_id:
            canonical_name = project_name
        elif allow_project_create:
            project_id = bitable_create(PROJECTS_TABLE_ID, {"Project Name": project_name})
            canonical_name = project_name
            clone_workflow_template(project_id, project_name, fallback_owner)
        else:
            project_id, canonical_name = _match_existing_project(project_name, title, transcript)
            # Meeting titles are often not project names (for example
            # "meeting daily"). When this Base has a single active project,
            # use it as the unambiguous meeting context.
            if not project_id:
                only_project = latest_project_record()
                only_name = field_text((only_project.get("fields") or {}).get("Project Name"))
                if only_project.get("record_id") and only_name:
                    project_id, canonical_name = only_project["record_id"], only_name
            if not project_id:
                log.warning("Meeting project '%s' did not match an existing Project; no project/task created", project_name)
                continue
            project_name = canonical_name
        log.info("Matched existing project %s (%s); meeting tasks will stay in its Delivery Plan", project_name, project_id)
        project_tasks = []
        if project.get("milestone"):
            project_tasks = project.get("tasks") or []
        else:
            for milestone in project.get("milestones") or []:
                for task in milestone.get("tasks") or []:
                    item = dict(task); item.setdefault("milestone", milestone.get("milestone") or milestone.get("name") or ""); project_tasks.append(item)
            project_tasks.extend(project.get("tasks") or [])
        # First update existing template rows; new action items are created below.
        update_delivery_progress({"projects": [project]}, title)
        for task in project_tasks:
            task_title = str(task.get("title") or "").strip()
            if not task_title:
                continue
            ai_owner = str(task.get("owner") or "").strip()
            owner = resolve_pic(task_title, str(task.get("milestone") or project.get("milestone") or ""), ai_owner, fallback_owner)
            if DELIVERY_TABLE_ID:
                milestone_name = str(task.get("milestone") or project.get("milestone") or "").strip()
                parent_id = ""
                delivery_rows = bitable_list(DELIVERY_TABLE_ID)
                if milestone_name:
                    for r in delivery_rows:
                        rf = r.get("fields") or {}
                        if field_text(rf.get("Item Name")).casefold() == milestone_name.casefold() and field_text(rf.get("Project")).casefold() == project_name.casefold():
                            parent_id = r.get("record_id", ""); break
                task_fields = {
                    "Item Name": task_title,
                    "Item Type": "Task",
                    "Status": task.get("status") or "Open",
                    "PIC": pic_field(owner),
                    "Project": project_name,
                    "Milestone Group": milestone_name or task_title,
                    "Priority": task.get("priority") or "Medium",
                    "Source Meeting": title,
                    "AI Confidence": "High" if ai_owner.startswith("ou_") else "Medium",
                    "Needs Review": "No" if task_title else "Yes",
                    "Stage": stage_for_milestone(milestone_name),
                }
                if project_id:
                    task_fields["Project Link"] = [project_id]
                if milestone_name: task_fields["Parent Item"] = milestone_name
                target_table = DELIVERY_TABLE_ID
            else:
                task_fields = {"Task name": task_title, "Owner": ([{"id": owner}] if owner else []), "Description": task.get("description") or plan.get("summary_vi") or plan.get("meeting_summary_vi") or transcript or ""}
                target_table = TASKS_TABLE_ID
            deadline = task.get("deadline") or ""
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
                due_ms = int(datetime.datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
                task_fields["Due Date" if DELIVERY_TABLE_ID else "Deadline"] = due_ms
            if project_id:
                task_fields.setdefault("Project", project_name)
            # Prefer updating the matching template task.  AI often rewrites
            # the action item in Vietnamese, while the 21-day template uses a
            # canonical title; creating a new row here would break the
            # template hierarchy and produce apparent duplicates.
            existing_task = None
            if DELIVERY_TABLE_ID:
                title_key = normalize_name(task_title)
                for candidate in delivery_rows:
                    cf = candidate.get("fields") or {}
                    if field_text(cf.get("Project")).casefold() != project_name.casefold():
                        continue
                    if field_text(cf.get("Item Type")).casefold() != "task":
                        continue
                    if normalize_name(cf.get("Item Name")) == title_key:
                        existing_task = candidate; break
                if not existing_task and milestone_name:
                    # Current 21-day template has one canonical task per
                    # milestone. Reuse that row when the AI wording differs.
                    candidates = [c for c in delivery_rows if field_text((c.get("fields") or {}).get("Project")).casefold() == project_name.casefold() and field_text((c.get("fields") or {}).get("Item Type")).casefold() == "task" and field_text((c.get("fields") or {}).get("Milestone Group")).casefold() == milestone_name.casefold()]
                    if len(candidates) == 1:
                        existing_task = candidates[0]
                        log.info("Mapped AI action '%s' to template task '%s'", task_title, field_text((existing_task.get("fields") or {}).get("Item Name")))
            if existing_task:
                bitable_update(DELIVERY_TABLE_ID, existing_task.get("record_id", ""), task_fields)
            else:
                try:
                    bitable_create(target_table, task_fields)
                except Exception as exc:
                    log.warning("Task link failed, retrying without Project: %s", exc)
                    task_fields.pop("Project", None)
                    bitable_create(target_table, task_fields)
            total_tasks += 1
    log.info("Created %d project(s) and %d extracted task(s)", len(projects), total_tasks)
    return plan

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
    # Participants is a GroupChat field in this Base; an event owner open_id
    # is not a valid GroupChat value, so leave it for Lark's native sync.
    fields = {k: v for k, v in fields.items() if v not in ("", None)}
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records"
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base create failed {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("code", 0) not in (0, None):
        raise RuntimeError(f"Base create failed {body.get('code')}: {body.get('msg', '')}")
    log.info("Base record created for event %s", event_id)

def extract_minutes_url(value):
    """Find a forwarded Lark Minutes link anywhere in a message payload."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    match = re.search(r"https?://[^\"\s<>]+(?:minutes|minute)[^\"\s<>]*", text, re.I)
    return match.group(0).rstrip(".,)") if match else ""

def fetch_message_payload(message_id):
    """Fetch the parent/root message for reply events (e.g. forwarded cards)."""
    if not message_id:
        return {}
    url = f"{DOMAIN}/open-apis/im/v1/messages/{urllib.parse.quote(str(message_id), safe='')}"
    r = requests.get(url, headers={"Authorization": f"Bearer {tenant_token()}"}, timeout=20)
    if r.status_code >= 300:
        log.warning("Unable to fetch parent message %s: %s", message_id, r.status_code)
        return {}
    return r.json().get("data", {}).get("items", [{}])[0] if isinstance(r.json().get("data", {}).get("items"), list) else r.json().get("data", {})

def create_forwarded_minutes_record(event, minute_url):
    """Create a Base row when a user forwards the Minutes card/link to this bot."""
    event_id = event.get("_event_id", "unknown")
    # Forwarded cards arrive as a URL rather than a minutes event.  The last
    # path component is the minute token used by the transcript API.
    parsed = urllib.parse.urlparse(minute_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    raw_token = urllib.parse.unquote(path_parts[-1]) if path_parts else ""
    # Rich-message forwarding can append the bot mention (e.g. @\_user\_1)
    # or an escaped backslash to the URL.  The Minutes API accepts only the
    # actual opaque token, which is the leading URL-safe segment.
    token_match = re.match(r"([A-Za-z0-9_-]+)", raw_token)
    minute_token = token_match.group(1) if token_match else ""
    # One user action can produce both a message event and a forwarded-card
    # event.  Treat the opaque Minutes token as the idempotency key so only
    # one Base row/project is created.
    if minute_token:
        with PROCESSED_EVENTS_LOCK:
            if minute_token in PROCESSED_MINUTES or minute_token in PROCESSING_MINUTES:
                log.info("Skipping duplicate forwarded Minutes %s", minute_token)
                return
            PROCESSING_MINUTES.add(minute_token)
    transcript = ""
    if minute_token and minute_token.lower() not in {"minutes", "minute", "min"}:
        try:
            transcript = fetch_transcript(minute_token)
            log.info("Transcript fetched for forwarded Minutes %s", minute_token)
        except Exception as exc:
            log.warning("Transcript fetch failed for forwarded Minutes %s: %s", minute_token, exc)
            # Do not mark a link as processed and do not create an empty AI
            # project/card. The user can resend the link after OAuth refresh.
            with PROCESSED_EVENTS_LOCK:
                PROCESSING_MINUTES.discard(minute_token)
            return
    meeting_title, metadata_participants = minutes_title_and_participants(minute_token)
    # Forwarded rich cards often carry the original title in the message
    # payload; use it when the Minutes metadata endpoint is unavailable.
    meeting_title = meeting_title or find_value(event, {"meeting_title", "topic", "title", "name"}) or "Forwarded Lark meeting minutes"
    fields = {
        "Meeting Title": meeting_title,
        "Minutes URL": minute_url,
        "Source Event ID": event_id,
        "Ingestion Status": "Imported — awaiting AI" if transcript else "Minutes link received — transcript fetch pending",
        "AI Processing Status": "Pending",
    }
    # Do not write user open_ids into the GroupChat field. That field only
    # accepts chat identifiers and would make the entire record create fail
    # with WrongRequestBody.
    if transcript:
        fields["Transcript / Raw Recap"] = transcript
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records"
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"Base create failed {r.status_code}: {r.text[:500]}")
        body = r.json()
        if body.get("code", 0) not in (0, None):
            raise RuntimeError(f"Base create failed {body.get('code')}: {body.get('msg', '')}")
        record_id = (body.get("data") or {}).get("record", {}).get("record_id", "")
        log.info("Base record created from forwarded Minutes link for event %s", event_id)
        plan = create_project_and_tasks(event, fields["Meeting Title"], transcript)
        if record_id and plan:
            bitable_update(TABLE_ID, record_id, plan_to_meeting_fields(plan))
        send_progress_card(event, plan, fields["Meeting Title"], transcript)
        if minute_token:
            with PROCESSED_EVENTS_LOCK:
                PROCESSING_MINUTES.discard(minute_token)
                PROCESSED_MINUTES.add(minute_token)
    except Exception:
        if minute_token:
            with PROCESSED_EVENTS_LOCK:
                PROCESSING_MINUTES.discard(minute_token)
        raise

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
        parsed_path = urllib.parse.urlparse(self.path).path
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
            created_table_id = find_value(event, {"table_id", "tableId"})
            change_action = str(find_value(event, {"action", "change_type", "changeType", "operation"}) or "").casefold()
            bitable_change_event = event_type == "drive.file.bitable_record_changed_v1"
            is_project_create = parsed_path == "/lark/project-created" or event_type == "project.created_v1" or (event_type == "bitable.record.created_v1" and created_table_id == PROJECTS_TABLE_ID) or (bitable_change_event and created_table_id == PROJECTS_TABLE_ID and change_action not in {"record_deleted", "deleted", "delete"})
            if is_project_create:
                handle_project_created(event)
                body = json.dumps({"ok": True, "status": "template_cloned"}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if event_type in {"bitable.record.updated_v1", "bitable.record.changed_v1", "drive.file.bitable_record_changed_v1"}:
                fields = event.get("fields") or (event.get("record") or {}).get("fields") or (event.get("data") or {}).get("fields") or {}
                table_id = find_value(event, {"table_id", "tableId"})
                project_name = field_text(fields.get("Project")) if isinstance(fields, dict) else ""
                if DELIVERY_TABLE_ID and (not table_id or table_id == DELIVERY_TABLE_ID) and project_name:
                    sync_project_from_delivery(project_name)
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
            meeting_id = (event.get("meeting") or {}).get("id")
            log.info("Webhook received: event=%s meeting=%s", event_type, meeting_id)

            # The app also receives chat messages.  They are only a health check
            # for the bot unless the user forwards a Lark Minutes link.
            if event_type == "im.message.receive_v1":
                event_id = event.get("_event_id", "unknown")
                with PROCESSED_EVENTS_LOCK:
                    if event_id != "unknown" and event_id in PROCESSED_EVENTS:
                        log.info("Skipping duplicate webhook event %s", event_id)
                        self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
                    if event_id != "unknown":
                        PROCESSED_EVENTS.add(event_id)
                minute_url = extract_minutes_url(event)
                if not minute_url:
                    message = event.get("message") or {}
                    for parent_id in (message.get("parent_id"), message.get("root_id")):
                        parent = fetch_message_payload(parent_id)
                        minute_url = extract_minutes_url(parent)
                        if minute_url:
                            log.info("Found Minutes link in parent message %s", parent_id)
                            break
                if minute_url:
                    try:
                        create_forwarded_minutes_record(event, minute_url)
                    except Exception:
                        # Allow Lark to retry a failed delivery instead of
                        # treating the event as successfully processed.
                        with PROCESSED_EVENTS_LOCK:
                            PROCESSED_EVENTS.discard(event_id)
                        raise
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
    if DAILY_PROGRESS_ENABLED:
        threading.Thread(target=daily_progress_loop, daemon=True, name="daily-progress").start()
        log.info("Daily progress automation enabled for %02d:00 ICT", DAILY_PROGRESS_HOUR)
    log.info("Listening on http://127.0.0.1:%s/lark/events", port)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
