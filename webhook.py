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
MILESTONES_TABLE_ID = os.getenv("MILESTONES_TABLE_ID")
WORKFLOW_TABLE_ID = os.getenv("WORKFLOW_TABLE_ID")
DELIVERY_TABLE_ID = os.getenv("DELIVERY_TABLE_ID")
CODEX_BIN = os.getenv("CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
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
    record_id = (r.json().get("data") or {}).get("record", {}).get("record_id", "")
    log.info("Base record created from generated Minutes %s", minute_token)
    plan = create_project_and_tasks(event, fields["Meeting Title"], transcript)
    if record_id and plan:
        bitable_update(TABLE_ID, record_id, plan_to_meeting_fields(plan))

def bitable_create(table_id, fields):
    if not table_id:
        return ""
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    r = requests.post(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base create failed {r.status_code}: {r.text[:500]}")
    return (r.json().get("data") or {}).get("record", {}).get("record_id", "")

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

def clone_workflow_template(project_id, project_name, owner=""):
    """Instantiate the 21-day template for a newly created project."""
    if not WORKFLOW_TABLE_ID or not project_id:
        return
    rows = bitable_list(WORKFLOW_TABLE_ID)
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
                    "PIC": owner,
                    "Sequence": f.get("Sequence") or 0,
                    "Template Source": "21-day master",
                    "Progress %": 0,
                }
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
                    "PIC": owner,
                    "Sequence": f.get("Sequence") or 0,
                    "Priority": field_text(f.get("Default Priority")) or "Medium",
                    "Template Source": "21-day master",
                    "Progress %": 0,
                }
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

def bitable_update(table_id, record_id, fields):
    if not table_id or not record_id or not fields:
        return
    url = f"{DOMAIN}/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    r = requests.put(url, headers={"Authorization": f"Bearer {tenant_token()}", "Content-Type": "application/json"}, json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Base update failed {r.status_code}: {r.text[:500]}")

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
                    "PIC": task.get("owner") or "",
                    "Priority": task.get("priority") or "Medium",
                    "Next Action": task.get("next_step") or "",
                    "Source Meeting": meeting_title,
                    "AI Confidence": "High",
                    "Needs Review": "No",
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

def extraction_prompt(transcript):
    """Prompt shared by the hosted API and the optional local Codex fallback."""
    return f'''Đọc transcript cuộc họp dưới đây. Trả về DUY NHẤT JSON hợp lệ, không markdown:
{{"meeting_summary_vi":"Tóm tắt toàn bộ cuộc họp bằng tiếng Việt chuẩn","decisions":["quyết định quan trọng"],"projects":[{{"project":"Tên project đúng như transcript","status":"Current status","health":"On track|At risk|Blocked|Unknown","progress_percent":0,"summary_vi":"Tình hình project","next_step":"Bước tiếp theo của project","milestones":[{{"milestone":"Tên milestone","status":"Current milestone status","progress_percent":0,"risk":"Rủi ro hoặc blocker, nếu có","next_step":"Bước tiếp theo của milestone","tasks":[{{"title":"việc cần làm","description":"mô tả rõ","owner":"tên người nếu nói rõ, nếu không để rỗng","deadline":"YYYY-MM-DD nếu có, nếu không để rỗng","status":"Open|In Progress|Done|Blocked","progress_percent":0,"next_step":"Bước tiếp theo","risk":"Rủi ro nếu có","priority":"High|Medium|Low"}}]}}]}}]}}
Không bịa tên người hoặc deadline. Sửa lỗi chính tả và dịch sang tiếng Việt tự nhiên.
TRANSCRIPT:
{transcript[:50000]}'''

def openai_plan(transcript):
    """Use OpenAI Responses API for production transcript extraction."""
    if not OPENAI_API_KEY or not transcript:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=extraction_prompt(transcript),
            store=False,
        )
        output = (response.output_text or "").strip()
        match = re.search(r"\{.*\}", output, re.S)
        if match:
            data = json.loads(match.group(0))
            if isinstance(data.get("tasks"), list) or isinstance(data.get("projects"), list):
                log.info("Transcript extracted with OpenAI model %s", OPENAI_MODEL)
                return data
    except Exception as exc:
        log.warning("OpenAI extraction unavailable; trying Codex fallback: %s", exc)
    return None

def codex_plan(transcript):
    """Normalize Vietnamese transcript and extract a strict project/task plan."""
    if not transcript:
        return {"project": "Lark meeting project", "tasks": []}
    prompt = extraction_prompt(transcript)
    try:
        result = subprocess.run([CODEX_BIN, "exec", "--ephemeral", "--skip-git-repo-check", prompt], capture_output=True, text=True, timeout=60)
        output = (result.stdout or "").strip()
        match = re.search(r"\{.*\}", output, re.S)
        if match:
            data = json.loads(match.group(0))
            if isinstance(data.get("tasks"), list) or isinstance(data.get("projects"), list):
                return data
    except Exception as exc:
        log.warning("Codex extraction unavailable; using fallback: %s", exc)
    return {"project": "Lark meeting project", "summary_vi": transcript[:5000], "tasks": []}

def create_project_and_tasks(event, title, transcript):
    """Create a project and one Base task per extracted action item.

    The webhook is the single owner of task creation.  AI-provided owners are
    used only when they are already Lark open_ids; otherwise we fall back to
    the meeting owner/participant so a task is never silently dropped.
    """
    if not PROJECTS_TABLE_ID or (not TASKS_TABLE_ID and not DELIVERY_TABLE_ID):
        log.warning("Project/task table IDs are not configured; skipping task creation")
        return {"project": title, "tasks": []}
    plan = openai_plan(transcript) or codex_plan(transcript)
    owners = participant_ids(event)
    meeting_owner = ((event.get("meeting") or {}).get("owner") or {}).get("id", {})
    fallback_owner = meeting_owner.get("open_id") if isinstance(meeting_owner, dict) else ""
    fallback_owner = fallback_owner or (owners[0] if owners else "")
    projects = plan.get("projects") if isinstance(plan.get("projects"), list) else [{"project": plan.get("project") or title, "milestone": plan.get("milestone"), "tasks": plan.get("tasks") or []}]
    total_tasks = 0
    for project in projects:
        project_name = project.get("project") or project.get("name") or title or "Lark meeting project"
        project_id = find_project_id(project_name)
        if not project_id:
            project_id = bitable_create(PROJECTS_TABLE_ID, {"Project Name": project_name})
            clone_workflow_template(project_id, project_name, fallback_owner)
        else:
            log.info("Matched existing project %s (%s); skipping template clone", project_name, project_id)
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
            owner = ai_owner if ai_owner.startswith("ou_") else fallback_owner
            if DELIVERY_TABLE_ID:
                milestone_name = str(task.get("milestone") or project.get("milestone") or "").strip()
                parent_id = ""
                if milestone_name:
                    for r in bitable_list(DELIVERY_TABLE_ID):
                        rf = r.get("fields") or {}
                        if field_text(rf.get("Item Name")).casefold() == milestone_name.casefold() and field_text(rf.get("Project")).casefold() == project_name.casefold():
                            parent_id = r.get("record_id", ""); break
                task_fields = {
                    "Item Name": task_title,
                    "Item Type": "Task",
                    "Status": task.get("status") or "Open",
                    "PIC": owner,
                    "Project": project_name,
                    "Milestone Group": milestone_name or task_title,
                    "Priority": task.get("priority") or "Medium",
                    "Source Meeting": title,
                    "AI Confidence": "High" if ai_owner.startswith("ou_") else "Medium",
                    "Needs Review": "No" if task_title else "Yes",
                }
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
    record_id = (r.json().get("data") or {}).get("record", {}).get("record_id", "")
    log.info("Base record created from forwarded Minutes link for event %s", event_id)
    plan = create_project_and_tasks(event, fields["Meeting Title"], transcript)
    if record_id and plan:
        bitable_update(TABLE_ID, record_id, plan_to_meeting_fields(plan))

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
            if parsed_path == "/lark/project-created" or event_type in {"bitable.record.created_v1", "project.created_v1"}:
                handle_project_created(event)
                body = json.dumps({"ok": True, "status": "template_cloned"}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
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
