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

def summarize_thread(topic_title: str, text: str) -> tuple:
    """Returns a tuple of (summary_string, model_used_string)"""
    if not GEMINI_API_KEY:
        return "Summary unavailable: missing API key.", "none"

    prompt = f"Topic Title: {topic_title}\n\nDiscussion Excerpt:\n{text}\n"
    models = ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        
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
                            "description": "2-3 plain-language sentences summarizing the thread. No greetings, no filler."
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
                        return json.loads(raw_json).get("summary", "").strip(), model
                elif resp.status_code == 404:
                    break 
                elif resp.status_code in (429, 500, 503) and attempt < 2:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                else:
                    break 
            except Exception:
                if attempt < 2:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                break

    return "Summary unavailable at this time.", "none"

def fetch_and_process():
    headers = {"User-Agent": "KasSmithsDigestBot/1.0"}
    
    resp = requests.get(f"{FORUM_BASE}/latest.json", headers=headers, timeout=15)
    resp.raise_for_status()
    topics_meta = resp.json().get("topic_list", {}).get("topics", [])
    
    data_store = load_data()
    existing_ids = {t["id"] for t in data_store.get("topics", [])}

    new_summaries = []
    processed = 0

    for t in topics_meta:
        if processed >= MAX_NEW_PER_RUN:
            break
            
        topic_id = t.get("id")
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

        summary, model_used = summarize_thread(t.get("title", ""), full_thread_sample)
        processed += 1
        time.sleep(MIN_SECONDS_BETWEEN_CALLS)

        now_utc = datetime.now(timezone.utc)
        new_summaries.append({
            "id": topic_id,
            "title": t.get("title", ""),
            "url": f"{FORUM_BASE}/t/{t.get('slug', '')}/{topic_id}",
            "summary": summary,
            "model": model_used,
            "posts_count": t.get("posts_count", 1),
            "views": t.get("views", 0),
            "is_hot": (t.get("posts_count", 1) >= 8 or t.get("like_count", 0) >= 10 or t.get("views", 0) >= 300),
            "iso_date": now_utc.isoformat(),
            "updated_at": now_utc.strftime("%b %d, %Y %H:%M UTC")
        })

    if new_summaries:
        data_store["topics"] = new_summaries + data_store["topics"]
        save_data(data_store)

    generate_html(data_store["topics"])
    generate_rss(data_store["topics"])

    # Output GitHub Step Summary
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary and new_summaries:
        summary_lines = [
            f"- **{t['title']}**: {'⚠️ fallback' if 'unavailable' in t['summary'].lower() else f'✅ ({t.get("model", "unknown")})'}"
            for t in new_summaries
        ]
        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write("## Digest Run Summary\n" + "\n".join(summary_lines) + "\n")

def generate_rss(topics):
    items = ""
    for t in topics:
        safe_title = html_lib.escape(t['title'])
        safe_url = html_lib.escape(t['url'])
        safe_summary = html_lib.escape(t['summary'])
        
        # Format date for RSS (RFC 822)
        try:
            dt = datetime.fromisoformat(t['iso_date'])
            rfc_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except:
            rfc_date = ""

        items += f"""
        <item>
            <title>{safe_title}</title>
            <link>{safe_url}</link>
            <description>{safe_summary}</description>
            <guid isPermaLink="true">{safe_url}</guid>
            <pubDate>{rfc_date}</pubDate>
        </item>"""

    rss_content = f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
<channel>
    <title>Kas-Smiths Forum Digest</title>
    <link>https://weirdtualguy.github.io/katch-up/</link>
    <description>Daily digest of hot discussions</description>
    {items}
</channel>
</rss>"""

    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss_content)

def generate_html(topics):
    cards_html = ""
    for t in topics:
        hot_badge = '<span class="badge">🔥 Hot Discussion</span>' if t.get("is_hot") else ''
        iso_date = t.get('iso_date', '')
        
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
    <meta property="og:title" content="Kas-Smiths Forum Digest">
    <meta property="og:description" content="What happened while you were away, explained in simple terms.">
    <meta property="og:type" content="website">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚡</text></svg>">
    <link rel="alternate" type="application/rss+xml" title="Kas-Smiths Digest RSS" href="feed.xml">
    <title>Kas-Smiths Daily Digest</title>
    <style>
        :root {{
            --bg: #0f172a; --surface: #1e293b; --text-main: #f8fafc; --text-muted: #cbd5e1;
            --primary: #38bdf8; --accent: #f97316; --border: #334155;
        }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text-main); margin: 0; padding: 16px; display: flex; justify-content: center; }}
        .container {{ max-width: 680px; width: 100%; }}
        header {{ margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: baseline; }}
        h1 {{ margin: 0 0 6px 0; font-size: 1.5rem; }}
        .tagline {{ color: var(--text-muted); font-size: 0.9rem; margin: 0; }}
        .rss-link {{ color: var(--text-muted); text-decoration: none; font-size: 0.85rem; }}
        .rss-link:hover {{ color: var(--accent); }}
        .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2); }}
        .card-link {{ display: block; padding: 16px; color: inherit; text-decoration: none; border-radius: 12px; }}
        .card-link:focus-visible {{ outline: 2px solid var(--primary); outline-offset: 3px; }}
        .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 0.75rem; }}
        .badge {{ background: rgba(249, 115, 22, 0.2); color: var(--accent); padding: 2px 8px; border-radius: 99px; font-weight: bold; }}
        .date {{ color: var(--text-muted); margin-left: auto; font-weight: 500; }}
        h2 {{ margin: 0 0 10px 0; font-size: 1.15rem; line-height: 1.35; color: var(--primary); }}
        .card-link:hover h2 {{ text-decoration: underline; }}
        .summary {{ margin: 0 0 12px 0; color: #e2e8f0; font-size: 0.95rem; line-height: 1.5; }}
        .card-footer {{ display: flex; align-items: center; color: var(--text-muted); font-size: 0.85rem; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 8px; }}
    </style>
</head>
<body>
    <main class="container">
        <header>
            <div>
                <h1>⚡ Kas-Smiths Forum Digest</h1>
                <p class="tagline">What happened while you were away, explained in simple terms.</p>
            </div>
            <a href="feed.xml" class="rss-link" aria-label="RSS Feed">📶 RSS</a>
        </header>
        {cards_html}
    </main>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)

if __name__ == "__main__":
    fetch_and_process()
