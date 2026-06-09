import os
import re
import json
from datetime import datetime, timezone
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv

# Import Google GenAI dependencies
from google import genai
from google.genai import types

load_dotenv()

# Load .env from config directory
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SRC_DIR)
ENV_FILE = os.path.join(BASE_DIR, "config", ".env")
load_dotenv(ENV_FILE)

# 🔒 BYPASS CORPORATE FIREWALL: Disable strict global SSL verification
os.environ["PYTHONHTTPSVERIFY"] = "0"
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# Tokens & Paths
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Validate tokens
if not SLACK_BOT_TOKEN:
    raise ValueError("⚠️ SLACK_BOT_TOKEN not found in .env file")
if not SLACK_APP_TOKEN:
    raise ValueError("⚠️ SLACK_APP_TOKEN not found in .env file")
if not GEMINI_API_KEY:
    raise ValueError("⚠️ GEMINI_API_KEY not found in .env file")

DATA_DIR = os.path.join(BASE_DIR, "data")
# Each concern is a plain JSON file keyed by Slack channel ID.
DB_FILE = os.path.join(DATA_DIR, "db.json")          # standup updates
TEAM_FILE = os.path.join(DATA_DIR, "team.json")      # roster
ABSENT_FILE = os.path.join(DATA_DIR, "absent.json")  # absences

os.makedirs(DATA_DIR, exist_ok=True)


# ------------------------------------------------------------------
# Per-channel JSON state helpers (one file per concern)
# ------------------------------------------------------------------
# Every file is a dict keyed by Slack channel ID, so each channel (team) is
# independent. Each write rewrites the whole file, so removing a member from a
# channel actually deletes them from the file on disk.
def _load_json(path):
    """Read a channel-keyed dict from disk, tolerating a missing/corrupt file."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _load_channel(path, channel_id):
    """Return the list stored for this channel (empty list if none)."""
    return _load_json(path).get(channel_id, [])


def _save_channel(path, channel_id, items):
    """Replace this channel's list, leaving other channels untouched."""
    data = _load_json(path)
    if items:
        data[channel_id] = items
    else:
        data.pop(channel_id, None)   # empty list -> drop the key entirely
    _save_json(path, data)


def load_roster(channel_id):
    """Slack user IDs allowed to submit updates in this channel."""
    return _load_channel(TEAM_FILE, channel_id)


def save_roster(channel_id, roster):
    _save_channel(TEAM_FILE, channel_id, roster)


def load_absent(channel_id):
    """Slack user IDs marked absent/on leave for this channel's current cycle."""
    return _load_channel(ABSENT_FILE, channel_id)


def save_absent(channel_id, absent):
    _save_channel(ABSENT_FILE, channel_id, absent)


def load_updates(channel_id):
    """Standup updates submitted in this channel for the current cycle."""
    updates = _load_channel(DB_FILE, channel_id)
    # Be defensive: guarantee each update carries its channel id.
    for u in updates:
        u.setdefault("slack_channel_id", channel_id)
    return updates


def save_updates(channel_id, updates):
    """Replace this channel's updates (an empty list resets the cycle)."""
    _save_channel(DB_FILE, channel_id, updates)


# ------------------------------------------------------------------
# Slack mention parsing
# ------------------------------------------------------------------
def parse_user_ids(text):
    """Extract Slack user IDs from command text.

    Slack sends mentions as <@U12345|name> or <@U12345>, so we pull the IDs out.
    """
    import re
    return re.findall(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", text or "")


def resolve_user_ids(text, client):
    """Resolve user IDs from command text.

    Works whether or not Slack escapes the mention:
      • Escaped form  <@U12345|name>   -> ID parsed directly
      • Plain form    @swethabharathim -> looked up via the Slack API by handle
    """
    import re

    ids = set(parse_user_ids(text))

    # Plain @handles that Slack did NOT escape (the @ isn't preceded by '<').
    plain_handles = {h.lower() for h in re.findall(r"(?<!<)@([A-Za-z0-9._-]+)", text or "")}
    if plain_handles:
        try:
            cursor = None
            while True:
                resp = client.users_list(cursor=cursor, limit=200)
                for m in resp["members"]:
                    if m.get("deleted") or m.get("is_bot"):
                        continue
                    profile = m.get("profile", {}) or {}
                    candidates = {
                        (m.get("name") or "").lower(),
                        (profile.get("display_name") or "").lower(),
                        (profile.get("display_name_normalized") or "").lower(),
                    }
                    if candidates & plain_handles:
                        ids.add(m["id"])
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            print(f"❌ User lookup error: {e}")

    return list(ids)


# ------------------------------------------------------------------
# Report generation & delivery
# ------------------------------------------------------------------
def roster_complete(channel_id, updates):
    """True when every roster member has either submitted or is marked absent.

    Returns False if the channel has no roster (an open team has no "everyone").
    """
    roster = load_roster(channel_id)
    if not roster:
        return False
    submitted = {u["slack_user_id"] for u in updates}
    absent = set(load_absent(channel_id))
    return set(roster).issubset(submitted | absent)


def markdown_to_slack(text):
    """Convert Gemini's Markdown into Slack mrkdwn so it renders cleanly.

    Slack uses single-asterisk bold and renders `**...**` / leading `* ` as
    literal asterisks, so we translate standard Markdown into Slack's dialect.
    """
    # Headings (### H) -> bold line
    text = re.sub(r'^\s*#{1,6}\s*(.+?)\s*$', r'*\1*', text, flags=re.MULTILINE)
    # Bullets at line start -> • (do before bold so leading "* " isn't seen as bold)
    text = re.sub(r'^(\s*)[*\-+]\s+', r'\1• ', text, flags=re.MULTILINE)
    # Bold **x** / __x__ -> *x*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'__(.+?)__', r'*\1*', text)
    return text


def generate_pulse_report(updates):
    """Format the updates and run them through Gemini. Returns report text or None."""
    formatted_updates = ""
    for idx, item in enumerate(updates, start=1):
        formatted_updates += f"{idx}. User: <@{item['slack_user_id']}> | Update: {item['update_text']}\n"

    system_instruction = (
        "You are Async IQ, an elite project management AI engine.\n"
        "Your task is to synthesize daily engineering standup updates into a professional Team Pulse Report.\n\n"
        "Analyze the updates carefully to identify:\n"
        "1. **Progress Summary**: A consolidated view of what is moving forward.\n"
        "2. **Blockers & Roadblocks**: Explicit issues stopping team members.\n"
        "3. **Cross-Team Dependencies**: Inter-connected tasks (e.g., User A is waiting for User B to finish something).\n\n"
        "Keep the output professional, clean, scannable, and actionable. Use bold text and bullet points."
    )
    user_content = f"Here are the raw team updates for today:\n\n{formatted_updates}"

    # Try a sequence of model names; first one that returns text wins.
    client = genai.Client()
    for model in ('gemini-2.5-flash', 'models/gemini-2.5-flash',
                  'models/gemini-2.5-pro', 'models/gemini-2.5-flash-lite'):
        try:
            print(f"🤖 Trying {model}...")
            response = client.models.generate_content(
                model=model,
                contents=user_content,
                config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3),
            )
            if response.text:
                return response.text
        except Exception as e:
            print(f"   ↳ {model} failed: {e}")
    return None


def deliver_pulse_report(channel_id, say):
    """Generate this channel's report, post it, and reset the channel's cycle.

    Returns True if a report was posted, False if there was nothing to report
    or generation failed.
    """
    updates = load_updates(channel_id)
    if not updates:
        return False

    report_text = generate_pulse_report(updates)
    if not report_text:
        return False

    report_text = markdown_to_slack(report_text)
    header = "📊 *ASYNC IQ DAILY TEAM PULSE REPORT* 📊\n\n"
    say(text=f"{header}{report_text}", channel=channel_id)

    # Reset this channel's updates and absent list for the next session/day.
    save_updates(channel_id, [])
    save_absent(channel_id, [])
    return True


# Initialize Bolt App
app = App(token=SLACK_BOT_TOKEN)


# ==========================================
# 📥 COMMAND 1: COLLECT USER UPDATES (/async)
# ==========================================
@app.command("/async")
def handle_async_command(ack, command, respond, say):
    ack()
    user_id = command.get("user_id")
    channel_id = command.get("channel_id")
    text = command.get("text")

    if not text:
        respond("❌ Please include your update! Example: `/async Done with UI, working on Phase 3 today.`")
        return

    # Only roster members may submit. An empty roster means "open to everyone"
    # so the team isn't locked out before they've configured it.
    roster = load_roster(channel_id)
    if roster and user_id not in roster:
        respond("🚫 You're not on the standup roster, so your update wasn't logged. Ask a teammate to add you with `/async-team add @you`.")
        return

    try:
        updates_list = load_updates(channel_id)

        # One update per user: a re-submission replaces the person's previous entry
        # instead of stacking up duplicates.
        already_submitted = any(u.get("slack_user_id") == user_id for u in updates_list)
        updates_list = [u for u in updates_list if u.get("slack_user_id") != user_id]

        updates_list.append({
            "slack_user_id": user_id,
            "slack_channel_id": channel_id,
            "update_text": text,
            "submitted_at": datetime.now().isoformat() + "Z"
        })

        save_updates(channel_id, updates_list)

        if already_submitted:
            respond(f"♻️ Updated! <@{user_id}>, your latest update replaced the previous one.")
        else:
            respond(f"✅ Thanks <@{user_id}>! Your update has been logged for the AI report.")

        # Auto-trigger: if everyone on the roster has now submitted (or is marked
        # absent), generate and broadcast the Pulse report automatically.
        if roster_complete(channel_id, updates_list):
            respond("🎉 All updates are in! Compiling the Team Pulse report now...")
            try:
                deliver_pulse_report(channel_id, say)
            except Exception as e:
                print(f"❌ Auto-pulse error: {e}")
                respond("⚠️ Everyone submitted, but the report failed to compile. Try `/pulse` manually.")

    except Exception as e:
        print(f"❌ DB Write Error: {e}")
        respond("⚠️ Couldn't save your update.")


# ==========================================
# 👥 COMMAND: MANAGE THE STANDUP ROSTER (/async-team)
# ==========================================
@app.command("/async-team")
def handle_team_command(ack, command, respond, client):
    ack()
    channel_id = command.get("channel_id")
    text = (command.get("text") or "").strip()
    parts = text.split(maxsplit=1)
    action = parts[0].lower() if parts else "list"

    roster = load_roster(channel_id)

    if action == "list":
        if not roster:
            respond("👥 The roster is empty — `/async` is currently open to everyone in this channel. Add members with `/async-team add @user`.")
        else:
            members = "\n".join(f"• <@{uid}>" for uid in roster)
            respond(f"👥 *Standup roster ({len(roster)}):*\n{members}")
        return

    if action == "add":
        user_ids = resolve_user_ids(text, client)
        if not user_ids:
            respond("❌ Couldn't find that user. Mention them like `/async-team add @teammate` (make sure the username is spelled exactly).")
            return
        added = [uid for uid in user_ids if uid not in roster]
        roster.extend(added)
        save_roster(channel_id, roster)
        if added:
            respond("✅ Added to roster: " + ", ".join(f"<@{uid}>" for uid in added))
        else:
            respond("ℹ️ Those members are already on the roster.")
        return

    if action == "remove":
        user_ids = resolve_user_ids(text, client)
        if not user_ids:
            respond("❌ Couldn't find that user. Mention them like `/async-team remove @teammate` (make sure the username is spelled exactly).")
            return
        removed = [uid for uid in user_ids if uid in roster]
        roster = [uid for uid in roster if uid not in user_ids]
        save_roster(channel_id, roster)
        if removed:
            respond("🗑️ Removed from roster: " + ", ".join(f"<@{uid}>" for uid in removed))
        else:
            respond("ℹ️ Those members weren't on the roster.")
        return

    respond("❓ Unknown action. Use `/async-team add @user`, `/async-team remove @user`, or `/async-team list`.")


# ==========================================
# 🌴 COMMAND: MARK TEAMMATES ABSENT / ON LEAVE (/async-absent)
# ==========================================
@app.command("/async-absent")
def handle_absent_command(ack, command, respond, say, client):
    ack()
    channel_id = command.get("channel_id")
    text = (command.get("text") or "").strip()
    parts = text.split(maxsplit=1)
    action = parts[0].lower() if parts else ""

    absent = load_absent(channel_id)

    if action == "list":
        if not absent:
            respond("🟢 Nobody is marked absent for this cycle.")
        else:
            members = "\n".join(f"• <@{uid}>" for uid in absent)
            respond(f"🌴 *Marked absent this cycle ({len(absent)}):*\n{members}")
        return

    if action in ("clear", "remove"):
        user_ids = resolve_user_ids(text, client)
        if not user_ids:
            respond("❌ Mention who to un-mark. Example: `/async-absent clear @teammate`")
            return
        restored = [uid for uid in user_ids if uid in absent]
        absent = [uid for uid in absent if uid not in user_ids]
        save_absent(channel_id, absent)
        if restored:
            respond("🔙 No longer absent: " + ", ".join(f"<@{uid}>" for uid in restored))
        else:
            respond("ℹ️ Those members weren't marked absent.")
        return

    # Default action: mark the mentioned people as absent.
    user_ids = resolve_user_ids(text, client)
    if not user_ids:
        respond("❌ Mention who's absent. Example: `/async-absent @teammate`")
        return

    roster = load_roster(channel_id)
    added, skipped = [], []
    for uid in user_ids:
        if roster and uid not in roster:
            skipped.append(uid)          # not on the roster, so absence is meaningless
            continue
        if uid not in absent:
            absent.append(uid)
            added.append(uid)
    save_absent(channel_id, absent)

    if added:
        respond("🌴 Marked absent (will be skipped today): " + ", ".join(f"<@{uid}>" for uid in added))
    if skipped:
        respond("⚠️ Not on the roster, ignored: " + ", ".join(f"<@{uid}>" for uid in skipped))
    if not added and not skipped:
        respond("ℹ️ Those members were already marked absent.")

    # Marking someone absent may complete the roster — fire the report if so.
    updates = load_updates(channel_id)
    if updates and roster_complete(channel_id, updates):
        respond("🎉 Everyone remaining has submitted! Compiling the Team Pulse report now...")
        try:
            deliver_pulse_report(channel_id, say)
        except Exception as e:
            print(f"❌ Auto-pulse error: {e}")
            respond("⚠️ The report failed to compile. Try `/pulse` manually.")


# ==========================================
# 🚀 COMMAND 2: GENERATE & DELIVER PULSE (/pulse)
# ==========================================
@app.command("/pulse")
def handle_pulse_command(ack, command, say, respond):
    # Manual override — force a report even if not everyone has submitted yet.
    ack()

    current_channel = command.get("channel_id")

    updates = load_updates(current_channel)

    if not updates:
        respond("📭 No updates found in the logs to compile a report.")
        return

    respond("🤖 Gathering standup logs and running analysis through Async IQ Core...")

    try:
        posted = deliver_pulse_report(current_channel, say)
        if not posted:
            respond("⚠️ Failed to compile AI report after testing all model variations.")
    except Exception as e:
        print(f"❌ Gemini Error during delivery: {e}")
        respond(f"⚠️ Failed to compile AI report: {e}")


if __name__ == "__main__":
    print("🚀 Async IQ Complete System Lifecycle Active!")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
