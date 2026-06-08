import os
import json
from datetime import datetime
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv

# Import Google GenAI dependencies
from google import genai
from google.genai import types

load_dotenv()

# 🔒 BYPASS CORPORATE FIREWALL: Disable strict SSL verification
os.environ["PYTHONHTTPSVERIFY"] = "0"
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# Tokens & Paths
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")

# Initialize the local JSON database file
if not os.path.exists(DB_FILE):
    with open(DB_FILE, "w") as f:
        json.dump([], f, indent=2)

# Initialize Bolt App
app = App(token=SLACK_BOT_TOKEN)


# ==========================================
# 📥 COMMAND 1: COLLECT USER UPDATES (/async)
# ==========================================
@app.command("/async")
def handle_async_command(ack, command, respond):
    ack()
    user_id = command.get("user_id")
    channel_id = command.get("channel_id")
    text = command.get("text")
    
    if not text:
        respond("❌ Please include your update! Example: `/async Done with UI, working on Phase 3 today.`")
        return

    try:
        with open(DB_FILE, "r") as f:
            updates_list = json.load(f)
            
        updates_list.append({
            "slack_user_id": user_id,
            "slack_channel_id": channel_id,
            "update_text": text,
            "submitted_at": datetime.now().isoformat() + "Z"
        })
        
        with open(DB_FILE, "w") as f:
            json.dump(updates_list, f, indent=2)
            
        respond(f"✅ Thanks <@{user_id}>! Your update has been logged for the AI report.")
        
    except Exception as e:
        print(f"❌ DB Write Error: {e}")
        respond("⚠️ Couldn't save your update.")


# ==========================================
# 🚀 COMMAND 2: GENERATE & DELIVER PULSE (/pulse)
# ==========================================
@app.command("/pulse")
def handle_pulse_command(ack, command, say, respond):
    # 1. Acknowledge the request immediately
    ack()
    
    current_channel = command.get("channel_id")

    # 2. Read logs from database
    with open(DB_FILE, "r") as f:
        updates = json.load(f)
        
    if not updates:
        respond("📭 No updates found in the logs to compile a report.")
        return

    # Notify the channel that the AI is working
    respond("🤖 Gathering standup logs and running analysis through Async IQ Core...")

    # 3. Format the data for Gemini
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

    # 4. Generate the report with Gemini (including the 1.5-flash fallback loop)
    # 4. Generate the report with Gemini (with bulletproof naming fallbacks)
    try:
        client = genai.Client()
        report_text = None
        
        # Try Option A: Standard modern string format
        try:
            print("🤖 Trying standard gemini-2.5-flash...")
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=user_content,
                config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3)
            )
            report_text = response.text
        except Exception:
            pass # Move to next attempt if it fails
            
        # Try Option B: Prefixed modern string format
        if not report_text:
            try:
                print("🤖 Standard failed. Trying models/gemini-2.5-flash...")
                response = client.models.generate_content(
                    model='models/gemini-2.5-flash',
                    contents=user_content,
                    config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3)
                )
                report_text = response.text
            except Exception:
                pass

        # Try Option C: Standard legacy fallback string format
        if not report_text:
            try:
                print("🤖 2.5 failed. Trying stable gemini-1.5-flash...")
                response = client.models.generate_content(
                    model='gemini-1.5-flash',
                    contents=user_content,
                    config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3)
                )
                report_text = response.text
            except Exception:
                pass

        # Try Option D: Prefixed legacy fallback string format
        if not report_text:
            print("🤖 1.5 standard failed. Trying models/gemini-1.5-flash...")
            response = client.models.generate_content(
                model='models/gemini-1.5-flash',
                contents=user_content,
                config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3)
            )
            report_text = response.text

        # 5. BROADCAST IT! Post the final report into the public Slack channel
        header = "📊 *ASYNC IQ DAILY TEAM PULSE REPORT* 📊\n\n"
        say(text=f"{header}{report_text}", channel=current_channel)

        # 6. Clear out the database file for the next working session/day
        with open(DB_FILE, "w") as f:
            json.dump([], f, indent=2)
            
    except Exception as e:
        print(f"❌ Gemini Error during delivery: {e}")
        respond(f"⚠️ Failed to compile AI report after testing all variations: {e}")

        # 5. BROADCAST IT! Post the final report into the public Slack channel
        header = "📊 *ASYNC IQ DAILY TEAM PULSE REPORT* 📊\n\n"
        say(text=f"{header}{report_text}", channel=current_channel)

        # 6. Clear out the database file for the next working session/day
        with open(DB_FILE, "w") as f:
            json.dump([], f, indent=2)
            
    except Exception as e:
        print(f"❌ Gemini Error during delivery: {e}")
        respond(f"⚠️ Failed to compile AI report: {e}")


if __name__ == "__main__":
    print("🚀 Async IQ Complete System Lifecycle Active!")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()