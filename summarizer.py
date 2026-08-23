import os
import json
import time
import random
import requests
import html as html_lib
from html.parser import HTMLParser
from datetime import datetime, timezone

FORUM_BASE = "https://kas-smiths.org"
DATA_FILE = "data.json"
MAX_ITEMS = 60
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", 10))
MIN_SECONDS_BETWEEN_CALLS = 4.5
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# --- 1. Robust HTML Parsing (Audit Issue #2) ---
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip = False
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)

def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    parser = _TextExtractor()
    parser.feed(raw_html)
    text = html_lib.unescape(" ".join(parser.chunks))
    return " ".join(text.split())

# --- Data Management ---
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["topics"] = [
                    t for t in data.get("topics", [])
                    if "unavailable" not in t.get("summary", "").lower()
                ]
                return data
        except Exception:
            return {"topics": []}
    return {"topics": []}

def save_data(data):
    data["topics"] = data["topics"][:MAX_ITEMS]
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# --- AI Summarization with Retries & JSON Schema (Audit Issues #4 & #7) ---
def _is_retryable(status_code: int) -> bool:
    return status_code in (429, 500, 503)

def summarize_thread(topic_title: str, text: str) -> str:
    if not GEMINI_API_KEY:
        return "Summary unavailable: missing API key."

    prompt = (
        f"Topic Title: {topic_title}\n\n"
        f"Discussion Excerpt:\n{text}\n"
    )

    models = ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        
        # Enforcing JSON output to prevent conversational filler
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 250,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "summary": {
                            "type": "STRING",
                            "description": "2-3 plain-language sentences summarizing the thread. No greetings, no filler, no restating the title."
                        }
                    },
                    "required": ["summary"]
                }
            }
        }

        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=25)
                if resp.status_code == 200:
                    result = resp.json()
                    candidates = result.get("candidates", [])
                    if candidates:
                        raw_json = candidates[0]["content"]["parts"][0]["text"]
                        return json.loads(raw_json).get("summary", "").strip()
                elif resp.status_code == 404:
                    break # Model not found, skip to next model
                elif _is_retryable(resp.status_code) and attempt < 2:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                else:
                    break # Other error, break attempt loop
            except Exception as ex:
                if attempt < 2:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                break

    return "Summary unavailable at this time."

# --- Core Pipeline (Audit Issue #8) ---
def fetch_and_process():
    headers = {"User-Agent": "KasSmithsDigestBot/1.0"}
    
    resp = requests.get(f"{FORUM_BASE}/latest.json", headers=headers, timeout=15)
    resp.raise_for_status()
    latest_data = resp.json()
    
    topics_meta = latest_data.get("topic_list", {}).get("topics", [])
    data_store = load_data()
    existing_ids = {t["id"] for t in data_store.get("topics", [])}

    new_summaries = []
    processed = 0

    for t in topics_meta:
        if processed >= MAX_NEW_PER_RUN:
            break
            
        topic_id = t.get("id")
        title = t.get("title", "")
        slug = t.get("slug", "")
        post_count = t.get("posts_count", 1)
        like_count = t.get("like_count", 0)
        views = t.get("views", 0)
        
        is_hot = post_count >= 8 or like_count >= 10 or views >= 300

        if topic_id in existing_ids:
            continue

        thread_resp = requests.get(f"{FORUM_BASE}/t/{topic_id}.json", headers=headers, timeout=15)
        if thread_resp.status_code != 200:
            continue
        
        posts = thread_resp.json().get("post_stream", {}).get("posts", [])
        if not posts:
            continue

        combined_text = [f"Original Post: {clean_html(posts[0].get('cooked', ''))[:1200]}"]
        for reply in posts[1:3]:
            reply_text = clean_html(reply.get("cooked", ""))
            if reply_text:
                combined_text.append(f"Reply: {reply_text[:400]}")

        full_thread_sample = "\n".join(combined_text)[:2000]

        summary = summarize_thread(title, full_thread_sample)
        processed += 1
        time.sleep(MIN_SECONDS_BETWEEN_CALLS)

        # Store ISO timestamp alongside readable date
        now_utc = datetime.now(timezone.utc)
        new_summaries.append({
            "id": topic_id,
            "title": title,
            "url": f"{FORUM_BASE}/t/{slug}/{topic_id}",
            "summary": summary,
            "posts_count": post_count,
            "views": views,
            "is_hot": is_hot,
            "iso_date": now_utc.isoformat(),
            "updated_at": now_utc.strftime("%b %d, %Y %H:%M UTC")
        })

    if new_summaries:
        data_store["topics"] = new_summaries + data_store["topics"]
        save_data(data_store)

    generate_html(data_store["topics"])

# --- Secure & Accessible HTML Generation (Audit Issues #1, #10, #11) ---
def generate_html(topics):
    cards_html = ""
    for t in topics:
        hot_badge = '<span class="badge">🔥 Hot Discussion</span>' if t.get("is_hot") else ''
        iso_date = t.get('iso_date', '')
        
        # Using html.escape to prevent XSS injection
        safe_url = html_lib.escape(t['url'])
        safe_title = html_lib.escape(t['title'])
        safe_summary = html_lib.escape(t['summary'])
        safe_date = html_lib.escape(t.get('updated_at', ''))
        
        cards_html += f"""
        <article class="card">
            <a class="card-link" href="{safe_url}" target="_blank" rel="noopener noreferrer" aria-label="{safe_title}">
                <div class="card-header">
                    {hot_badge}
                    <time class="date" datetime="{iso_date}">{safe_date}</time>
                </div>
                <h2>{safe_title}</h2>
                <p class="summary">{safe_summary}</p>
                <div class="card-footer">
                    <span aria-hidden="true">💬</span> {int(t.get('posts_count', 1))} posts
                    <span style="margin-left:12px;" aria-hidden="true">👁️</span> {int(t.get('views', 0))} views
                </div>
            </a>
        </article>
        """

    cards_html = cards_html or '<p class="empty" style="text-align:center;">No updates yet — check back soon.</p>'

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="A daily digest of hot discussions from the Kas-Smiths forum.">
    <title>Kas-Smiths Daily Digest</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --text-main: #f8fafc;
            --text-muted: #cbd5e1;
            --primary: #38bdf8;
            --accent: #f97316;
            --border: #334155;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text-main);
            margin: 0;
            padding: 16px;
            display: flex;
            justify-content: center;
        }}
        .container {{ max-width: 680px; width: 100%; }}
        header {{ margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
        h1 {{ margin: 0 0 6px 0; font-size: 1.5rem; }}
        .tagline {{ color: var(--text-muted); font-size: 0.9rem; margin: 0; }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
        }}
        .card-link {{
            display: block;
            padding: 16px;
            color: inherit;
            text-decoration: none;
            border-radius: 12px;
        }}
        .card-link:focus-visible {{
            outline: 2px solid var(--primary);
            outline-offset: 3px;
        }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
            font-size: 0.75rem;
        }}
        .badge {{
            background: rgba(249, 115, 22, 0.2);
            color: var(--accent);
            padding: 2px 8px;
            border-radius: 99px;
            font-weight: bold;
        }}
        .date {{ color: var(--text-muted); margin-left: auto; font-weight: 500; }}
        h2 {{ margin: 0 0 10px 0; font-size: 1.15rem; line-height: 1.35; color: var(--primary); }}
        .card-link:hover h2 {{ text-decoration: underline; }}
        .summary {{ margin: 0 0 12px 0; color: #e2e8f0; font-size: 0.95rem; line-height: 1.5; }}
        .card-footer {{
            display: flex;
            align-items: center;
            color: var(--text-muted);
            font-size: 0.85rem;
            border-top: 1px solid rgba(255,255,255,0.05);
            padding-top: 8px;
        }}
    </style>
</head>
<body>
    <main class="container">
        <header>
            <h1>⚡ Kas-Smiths Forum Digest</h1>
            <p class="tagline">What happened while you were away, explained in simple terms.</p>
        </header>
        {cards_html}
    </main>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)

if __name__ == "__main__":
    fetch_and_process()
