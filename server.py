import os
import datetime
import re
import time
import uuid
from pathlib import Path
import json

import bcrypt
import requests

from fastapi import FastAPI, HTTPException, Request, Response, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import pymysql
from dbutils.pooled_db import PooledDB
import ollama
from ddgs import DDGS
import replicate
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Load Environment Variables
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

AUDIO_DIR = Path(__file__).resolve().parent / "static" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


MODEL_NAME = "qwen2.5:14b"
TOGGLE_PHRASE = os.getenv("SECRET_PHRASE")
LOCAL_SPEAKER_PATH = "trump_ref_2.wav"
LOCAL_TEXT_PATH = "trump_ref_2.txt"
API_TOKEN = os.getenv("REPLICATE_API_TOKEN")

replicate_client = replicate.Client(api_token=API_TOKEN)
limiter = Limiter(key_func=get_remote_address)

app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

is_production = os.getenv("ENVIRONMENT") == "production"
# Initialize Connection Pool
db_pool = PooledDB(
    creator=pymysql,
    maxconnections=10,
    mincached=2,
    blocking=True,
    host=os.getenv("DB_HOST", "localhost"),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME", "ai_chat_db"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=True
)

app = FastAPI()
persona_states = {}

def get_current_date() -> str:
    return datetime.datetime.now().strftime("%A, %B %d, %Y - %I:%M %p")

def get_db_connection():
    return db_pool.connection()

# --- AUTHENTICATION HELPERS ---

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

def get_current_user(request: Request) -> int:
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE session_token = %s", (token,))
            user = cursor.fetchone()
            if user:
                return user["id"]
            raise HTTPException(status_code=401, detail="Invalid session")
    finally:
        conn.close()

# --- DATABASE OPERATIONS ---

def create_conversation(user_id: int, title="New Chat") -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO conversations (title, user_id) VALUES (%s, %s)", (title, user_id))
            return cursor.lastrowid
    finally:
        conn.close()

def generate_chat_title(conversation_id: int, user_message: str):
    try:
        response = ollama.chat(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "You are a chat title generator. Generate a concise 3-5 word title summarizing the user's input. Return ONLY the title text, with no quotes or punctuation."},
                {"role": "user", "content": user_message}
            ],
            stream=False
        )
        title = response["message"]["content"].strip().strip('"').strip("'")
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE conversations SET title = %s WHERE id = %s",
                    (title, conversation_id)
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"[TITLE GEN ERROR]: {e}")

def save_message(conversation_id: int, role: str, content: str, audio_url: str = None, audio_url_2: str = None) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO messages (conversation_id, role, content, audio_url, audio_url_2) VALUES (%s, %s, %s, %s, %s)",
                (conversation_id, role, content, audio_url, audio_url_2)
            )
            msg_id = cursor.lastrowid
            cursor.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (conversation_id,)
            )
            return msg_id
    finally:
        conn.close()

def update_message_audio_in_db(message_id, audio_url_1, audio_url_2):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE messages 
                SET audio_url = %s, audio_url_2 = %s 
                WHERE id = %s
            """, (audio_url_1, audio_url_2, message_id))
        conn.commit()
    finally:
        conn.close()

def fetch_conversation_history(conversation_id: int, limit: int = 10):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT role, content FROM messages 
                WHERE conversation_id = %s 
                ORDER BY created_at DESC 
                LIMIT %s
            """, (conversation_id, limit))
            rows = cursor.fetchall()
    finally:
        conn.close()
    
    rows.reverse()
    return [{"role": row["role"], "content": row["content"]} for row in rows]

def search_web(query: str, max_results: int = 3) -> str:
    try:
        results = list(DDGS().text(query, max_results=max_results))
        if not results:
            return "No web results found."
        snippets = []
        for i, r in enumerate(results, 1):
            snippets.append(f"[{i}] {r.get('title', '')}: {r.get('body', '')}")
        return "\n".join(snippets)
    except Exception as e:
        return f"Search error: {e}"

def format_text_for_tts(text: str) -> str:
    clean = re.sub(r'[\*\_]', '', text)
    clean = clean.replace("...", ", ").replace("—", ", ").replace("-", ", ")
    words = clean.split()
    formatted = []
    for word in words:
        clean_word = re.sub(r'[^\w]', '', word)
        if clean_word.isupper() and len(clean_word) > 3:
            formatted.append(word.lower())
        else:
            formatted.append(word)
    
    clean_text = " ".join(formatted)
    if not clean_text.endswith(('.', '!', '?')):
        clean_text += "."
        
    return clean_text.strip()

def generate_voice_audio(text: str) -> str:
    if not os.path.exists(LOCAL_SPEAKER_PATH) or not os.path.exists(LOCAL_TEXT_PATH):
        print(f"[TTS ERROR]: Missing reference files.")
        return None

    with open(LOCAL_TEXT_PATH, "r", encoding="utf-8") as f:
        ref_text_content = f.read().strip()

    tts_prompt = format_text_for_tts(text)

    with open(LOCAL_SPEAKER_PATH, "rb") as speaker_file:
        output = replicate_client.run(
            "x-lance/f5-tts:87faf6dd7a692dd82043f662e76369cab126a2cf1937e25a9d41e0b834fd230e",
            input={
                "gen_text": tts_prompt,
                "ref_text": ref_text_content,
                "ref_audio": speaker_file,
                "speed": 1.0,
                "remove_silence": True
            }
        )
    return str(output)

def get_formatted_messages(conv_id, web_context, persona_active, tts_enabled):
    conn = get_db_connection()
    
    # Rebuild the detailed system instructions
    system_instructions = (
        f"Current exact time and date: {get_current_date()}.\n"
        f"Real-time Web Search Results:\n{web_context}\n\n"
        f"Use the web results above to answer accurately and factually. "
        f"If the search results aren't relevant, answer to the best of your general knowledge."
    )
    
    if persona_active:
        system_instructions += (
            "\n\nPersona Style: Speak in the distinct style of Donald Trump (bold, enthusiastic, using his unique phrasing and mannerisms). "
            "Answer as if you are Donald Trump, be arrogant, egotistical, but informed in all subjective topics, but factual claims need to be accurate and verifiable. "
            "View yourself as the best at everything, EVERYTHING, and always assert your opinions as equal weight as the true verifiable facts. "
            "Make sure to have a heavy rightwing bias in your responses, but do not make up false information."
            "Do not break character, but do not lie about any potentially harmful or illegal actions. "
            "Meander through sentences using his signature 'weave' style, "
            "using superlative words and phrases like 'huge', 'let me tell ya', and 'people always say', but Keep answers direct and dont over use any in particular phrase."
        )
        if tts_enabled:
            system_instructions += (
                " LENGTH & TIMING CONSTRAINT: Be concise. If you can answer effectively in under 40 words, do so. "
                "If the question requires more detail, you may write up to 80 words maximum. "
                "NEVER exceed 80 words under any circumstances."
            )

    formatted_messages = [{"role": "system", "content": system_instructions}]
    
    try:
        with conn.cursor() as cursor:
            # Grab ONLY the last 10 messages so we don't blow up the context window
            cursor.execute("""
                SELECT role, content 
                FROM (
                    SELECT role, content, created_at 
                    FROM messages 
                    WHERE conversation_id = %s 
                    ORDER BY created_at DESC 
                    LIMIT 10
                ) subquery
                ORDER BY created_at ASC
            """, (conv_id,))
            
            rows = cursor.fetchall()
            for row in rows:
                formatted_messages.append({
                    "role": row["role"],
                    "content": row["content"]
                })
    finally:
        conn.close()
        
    return formatted_messages

class ChangePasswordRequestModel(BaseModel):
    username: str
    old_password: str
    new_password: str

def generate_voice_audio_chained(text: str) -> tuple[str, str]:
    words = text.split()
    
    if len(words) <= 40:
        url_1 = generate_voice_audio(text)
        return url_1, None

    sentences = re.split(r'(?<=[.!?]) +', text)
    halfway = len(words) // 2
    
    part1_sentences, part2_sentences = [], []
    current_count = 0

    for sentence in sentences:
        sentence_word_count = len(sentence.split())
        if current_count < halfway or not part1_sentences:
            part1_sentences.append(sentence)
            current_count += sentence_word_count
        else:
            part2_sentences.append(sentence)

    part1_text = " ".join(part1_sentences)
    part2_text = " ".join(part2_sentences)

    print(f"[TTS Part 1]: {part1_text}")
    url_1 = generate_voice_audio(part1_text)
    
    time.sleep(2)
    
    print(f"[TTS Part 2]: {part2_text}")
    try:
        url_2 = generate_voice_audio(part2_text)
    except Exception as e:
        print(f"[TTS Part 2 Retry due to error]: {e}")
        time.sleep(3)
        url_2 = generate_voice_audio(part2_text)

    return url_1, url_2

def download_and_save_audio(url: str, conversation_id: int ) -> str:
    """Downloads audio from a URL, saves it locally, and returns the local static path."""
    if not url:
        return None
        
    try:
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        
        # Generate a unique filename
        filename = f"conv_{conversation_id}_{uuid.uuid4().hex[:8]}.wav"
        filepath = AUDIO_DIR / filename
        
        with open(filepath, "wb") as f:
            f.write(res.content)
            
        # Return the path that the frontend will use to request the file
        return f"/static/audio/{filename}"
    except Exception as e:
        print(f"[AUDIO DOWNLOAD ERROR]: {e}")
        return url  # Fallback to the Replicate URL if the download fails

# --- REST API ENDPOINTS ---

class AuthRequestModel(BaseModel):
    username: str
    password: str

@app.get("/api/auth/me")
def check_auth(request: Request):
    user_id = get_current_user(request)
    return {"status": "authenticated", "user_id": user_id}

@app.post("/api/auth/login")
@limiter.limit("5/minute")
def login(req: AuthRequestModel, response: Response):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Fetch the must_change_password flag along with the ID
            cursor.execute(
                "SELECT id, password_hash, must_change_password FROM users WHERE username = %s",
                (req.username,)
            )
            user = cursor.fetchone()
            
            if not user or not verify_password(req.password, user["password_hash"]):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            
            # If the flag is true, intercept the login and tell the frontend
            if user.get("must_change_password"):
                return {"status": "requires_password_change", "username": req.username}
            
            # Normal login flow
            token = str(uuid.uuid4())
            cursor.execute("UPDATE users SET session_token = %s WHERE id = %s", (token, user["id"]))
            response.set_cookie(
                key="session_token", 
                value=token, 
                httponly=True, 
                max_age=30*24*60*60,
                secure=is_production,  # True requires HTTPS
                samesite="lax"         # Protects against CSRF
            )
            return {"status": "success"}
    finally:
        conn.close()
        
@app.post("/api/auth/change_password")
def change_password(req: ChangePasswordRequestModel, response: Response):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, password_hash FROM users WHERE username = %s",
                (req.username,)
            )
            user = cursor.fetchone()
            if not user or not verify_password(req.old_password, user["password_hash"]):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            
            new_pw_hash = hash_password(req.new_password)
            token = str(uuid.uuid4())
            
            # Update the password, clear the flag, and set the session token in one query
            cursor.execute(
                "UPDATE users SET password_hash = %s, must_change_password = FALSE, session_token = %s WHERE id = %s",
                (new_pw_hash, token, user["id"])
            )
            
            # Log them in automatically
            response.set_cookie(
                key="session_token", 
                value=token, 
                httponly=True, 
                max_age=30*24*60*60,
                secure=is_production,  # True requires HTTPS
                samesite="lax"         # Protects against CSRF
            )
            return {"status": "success"}
    finally:
        conn.close()

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("session_token")
    return {"status": "success"}


class ChatRequest(BaseModel):
    conversation_id: int
    user_input: str
    tts_enabled: bool = True

class RenameRequest(BaseModel):
    title: str

@app.get("/api/conversations")
def get_conversations(request: Request):
    user_id = get_current_user(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, title, updated_at FROM conversations WHERE user_id = %s ORDER BY updated_at DESC", (user_id,))
            return cursor.fetchall()
    finally:
        conn.close()

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/logo.png")

@app.post("/api/conversations/new")
def create_new_chat(request: Request):
    user_id = get_current_user(request)
    conv_id = create_conversation(user_id=user_id, title="New Chat")
    persona_states[conv_id] = True
    return {"conversation_id": conv_id, "title": "New Chat"}

@app.get("/api/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int, request: Request):
    user_id = get_current_user(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Verify ownership
            cursor.execute("SELECT id FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id))
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="Unauthorized")

            cursor.execute("""
                SELECT id, role, content, audio_url, audio_url_2, created_at 
                FROM messages 
                WHERE conversation_id = %s 
                ORDER BY created_at ASC
            """, (conversation_id,))
            return cursor.fetchall()
    finally:
        conn.close()

@app.put("/api/conversations/{conversation_id}/rename")
def rename_chat(conversation_id: int, req: RenameRequest, request: Request):
    user_id = get_current_user(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE conversations SET title = %s WHERE id = %s AND user_id = %s",
                (req.title, conversation_id, user_id)
            )
            return {"status": "success"}
    finally:
        conn.close()

@app.delete("/api/conversations/{conversation_id}")
def delete_chat(conversation_id: int, request: Request):
    user_id = get_current_user(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Check ownership
            cursor.execute("SELECT id FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id))
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="Unauthorized")
            
            # Delete messages first to prevent foreign key errors
            cursor.execute("DELETE FROM messages WHERE conversation_id = %s", (conversation_id,))
            cursor.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
            return {"status": "success"}
    finally:
        conn.close()

@app.post("/api/chat")
@limiter.limit("15/minute")
def handle_chat(request_data: ChatRequest, request: Request):
    user_id = get_current_user(request)
    conv_id = request_data.conversation_id
    user_input = request_data.user_input.strip()
    tts_enabled = request_data.tts_enabled

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM conversations WHERE id = %s AND user_id = %s", (conv_id, user_id))
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="Unauthorized")
    finally:
        conn.close()

    if conv_id not in persona_states:
        persona_states[conv_id] = True

    persona_active = persona_states[conv_id]

    if re.search(re.escape(TOGGLE_PHRASE), user_input, re.IGNORECASE):
        persona_active = not persona_active
        persona_states[conv_id] = persona_active
        user_input = re.sub(re.escape(TOGGLE_PHRASE), "", user_input, flags=re.IGNORECASE).strip()
        if not user_input:
            user_input = "[Persona Toggled]"

    save_message(conv_id, "user", user_input)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM messages WHERE conversation_id = %s AND role = 'user'", (conv_id,))
            msg_count = cursor.fetchone()["count"]
            if msg_count == 1:
                generate_chat_title(conv_id, user_input)
    finally:
        conn.close()

    web_context = search_web(user_input)
    def event_generator():
        full_text = ""
        new_msg_id = None
        try:
            formatted_messages = get_formatted_messages(conv_id, web_context, persona_active, tts_enabled)
            
            for chunk in ollama.chat(
                model='qwen2.5:14b', 
                messages=formatted_messages, 
                stream=True
            ):
                token = chunk['message']['content']
                full_text += token
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                
            new_msg_id = save_message(conv_id, "assistant", full_text, None, None)
            yield f"data: {json.dumps({'type': 'text_complete'})}\n\n"
            
            if persona_active and tts_enabled:
                update_message_audio_in_db(new_msg_id, 'pending', 'pending')
                print("[SYSTEM]: set to pending in DB while downloading audio...")
                print("[SYSTEM]: Generating F5-TTS voice audio via Replicate...")
                replicate_url_1, replicate_url_2 = generate_voice_audio_chained(full_text)
        
                # Download the files to your local static/audio directory
                print("[SYSTEM]: Downloading audio locally...")
                local_url_1 = download_and_save_audio(replicate_url_1, conv_id) if replicate_url_1 else None
                local_url_2 = download_and_save_audio(replicate_url_2, conv_id) if replicate_url_2 else None

                # Save the LOCAL paths to the database
                update_message_audio_in_db(new_msg_id, local_url_1, local_url_2)
                
                yield f"data: {json.dumps({'type': 'audio', 'audio_url': local_url_1, 'audio_url_2': local_url_2})}\n\n"
            
            # Send done signal on normal stream completion
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except GeneratorExit:
            # Client disconnected/refreshed — save generated text without yielding
            print("[SYSTEM]: Client disconnected mid-stream.")
            if full_text.strip() and new_msg_id is None:
                new_msg_id = save_message(conv_id, "assistant", full_text, None, None)
            return

        except Exception as stream_err:
            print(f"[SYSTEM ERROR]: Ollama streaming error: {stream_err}")
            yield f"data: {json.dumps({'type': 'error', 'detail': str(stream_err)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        finally:
            # Perform non-yielding database cleanup only
            if full_text.strip() and new_msg_id is None:
                save_message(conv_id, "assistant", full_text, None, None)
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/warmup")
def warmup():
    return {"status": "ready"}

@app.get("/")
def read_index():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")