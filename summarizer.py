import os
import re
import json
import time
import requests
from datetime import datetime, timezone
from google import genai
from google.genai import types

FORUM_BASE = "https://kas-smiths.org"
DATA_FILE = "data.json"
MAX_ITEMS = 60
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    return " ".join(text.split())

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Filter out previous failed placeholder summaries so they get regenerated
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

def summarize_thread(client: genai.Client, topic_title: str, text: str) -> str:
    prompt = f"""You are summarizing forum discussions for everyday, non-technical readers.
Topic Title: {topic_title}

Discussion Excerpt:
{text}

Instructions:
1. Explain what happened or was discussed in 2 to 3 simple, friendly, jargon-free sentences.
2. Highlight why it matters or what the consensus/solution is.
3. Do not start with conversational filler."""

    # Models to attempt in order of preference
    candidate_models = ["gemini-3.5-flash-lite", "gemini-3.6-flash"]

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=250,
                    ),
                )
                if response.text:
                    return response.text.strip()
            except Exception as e:
                print(f"[Warn] Model {model_name} attempt {attempt+1} failed: {e}")
                if "429" in str(e) or "ResourceExhausted" in str(e):
                    time.sleep(5 * (attempt + 1))
                else:
                    break

    return "Summary unavailable at this time."

def fetch_and_process():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    headers = {"User-Agent": "KasSmithsDigestBot/1.0"}
    
    resp = requests.get(f"{FORUM_BASE}/latest.json", headers=headers, timeout=15)
    resp.raise_for_status()
    latest_data = resp.json()
    
    topics_meta = latest_data.get("topic_list", {}).get("topics", [])
    data_store = load_data()
    existing_ids = {t["id"] for t in data_store.get("topics", [])}

    new_summaries = []

    for t in topics_meta:
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
        
        thread_data = thread_resp.json()
        posts = thread_data.get("post_stream", {}).get("posts", [])
        if not posts:
            continue

        combined_text = []
        op_text = clean_html(posts[0].get("cooked", ""))
        combined_text.append(f"Original Post: {op_text[:1200]}")

        for reply in posts[1:3]:
            reply_text = clean_html(reply.get("cooked", ""))
            if reply_text:
                combined_text.append(f"Reply: {reply_text[:400]}")

        full_thread_sample = "\n".join(combined_text)[:2000]

        summary = summarize_thread(client, title, full_thread_sample)
        time.sleep(2)

        new_summaries.append({
            "id": topic_id,
            "title": title,
            "url": f"{FORUM_BASE}/t/{slug}/{topic_id}",
            "summary": summary,
            "posts_count": post_count,
            "views": views,
            "is_hot": is_hot,
            "updated_at": datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
        })

    if new_summaries:
        data_store["topics"] = new_summaries + data_store["topics"]
        save_data(data_store)

    generate_html(data_store["topics"])

def generate_html(topics):
    cards_html = ""
    for t in topics:
        hot_badge = '<span class="badge">🔥 Hot Discussion</span>' if t.get("is_hot") else ''
        cards_html += f"""
        <article class="card">
            <div class="card-header">
                {hot_badge}
                <span class="date">{t.get('updated_at', '')}</span>
            </div>
            <h2><a href="{t['url']}" target="_blank" rel="noopener">{t['title']}</a></h2>
            <p class="summary">{t['summary']}</p>
            <div class="card-footer">
                <span>💬 {t.get('posts_count', 1)} posts</span>
                <span>👁️ {t.get('views', 0)} views</span>
            </div>
        </article>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kas-Smiths Daily Digest</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
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
        .container {{
            max-width: 680px;
            width: 100%;
        }}
        header {{
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }}
        h1 {{ margin: 0 0 6px 0; font-size: 1.5rem; }}
        .tagline {{ color: var(--text-muted); font-size: 0.9rem; margin: 0; }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
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
        .date {{ color: var(--text-muted); margin-left: auto; }}
        h2 {{ margin: 0 0 10px 0; font-size: 1.15rem; line-height: 1.35; }}
        h2 a {{ color: var(--primary); text-decoration: none; }}
        h2 a:hover {{ text-decoration: underline; }}
        .summary {{
            margin: 0 0 12px 0;
            color: #cbd5e1;
            font-size: 0.95rem;
            line-height: 1.5;
        }}
        .card-footer {{
            display: flex;
            gap: 16px;
            color: var(--text-muted);
            font-size: 0.8rem;
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
