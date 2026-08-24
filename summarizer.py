import os
import re
import math
import json
import time
import random
import requests
import html as html_lib
from html.parser import HTMLParser
from collections import Counter
from datetime import datetime, timezone

FORUM_BASE = "https://kas-smiths.org"
DATA_FILE = "data.json"
MAX_ITEMS = 60
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", 10))
MIN_SECONDS_BETWEEN_CALLS = 4.5
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# ─────────────────────────────────────────────────────────────────────────
# Phase 1 category taxonomy
# ─────────────────────────────────────────────────────────────────────────
# Lightweight, adjustable. "Community" doubles as the catch-all fallback
# per the Phase 1 spec ("don't force every story into a category").
CATEGORIES = [
    "Development", "Infrastructure", "DeFi", "Tokens", "AI",
    "Privacy", "NFTs", "Ecosystem", "Governance", "Community",
]
DEFAULT_CATEGORY = "Community"

# Ordered most-specific-first: a topic gets the first category whose
# keywords it matches. Checked against the TITLE ALONE first; only if
# nothing matches there do we fall back to title+summary combined (see
# classify_category). That two-pass order matters -- e.g. a thread titled
# "Using Covenants for User verifications" should classify on "covenant"
# (Development) from its own title, not on an incidental "the consensus
# is..." turn of phrase buried in its AI-generated summary.
CATEGORY_KEYWORDS = [
    ("AI", (r"\bai agent", r"\bagentic", r"artificial intelligence", r"\bai-agent",
            r"language model", r"\bllm\b", r"\bai tool", r"autonomous agent")),
    ("Privacy", (r"\bprivacy", r"private transaction", r"anonymit", r"confidential",
                 r"zero-knowledge", r"\bzk\b", r"zk-")),
    ("NFTs", (r"\bnft", r"kcc721", r"krc721", r"collectible")),
    ("DeFi", (r"\bdefi\b", r"stablecoin", r"\bbridge", r"prediction market", r"liquidity",
              r"\bamm\b", r"lending", r"yield farm", r"\bdex\b", r"\bswap")),
    ("Tokens", (r"\btoken", r"kcc20", r"krc20", r"kcc-20", r"krc-20", r"\bminting", r"supply cap")),
    ("Governance", (r"governance", r"\bproposal", r"\bvote\b", r"\bvoting\b", r"\bjury\b",
                     r"\baudit\b", r"\bpolicy\b", r"kcc-0", r"kip-")),
    ("Infrastructure", (r"rest api", r"\bapi\b", r"\bnode\b", r"testnet", r"mainnet", r"\brpc\b",
                         r"infrastructure", r"hard fork", r"\bconsensus\b", r"protocol upgrade")),
    # Checked before the broad "Development" bucket so generic intro/brand
    # posts that happen to mention "developers" in passing aren't misfiled.
    ("Community", (r"\bwelcome\b", r"\bhello\b", r"\bhi everyone\b", r"introduce myself",
                    r"\bbrand\b", r"\blogo\b", r"rebrand", r"\bthank you\b")),
    ("Development", (r"\bsdk\b", r"covenant", r"\bopcode", r"specification", r"\bspec\b",
                      r"\babi\b", r"developer", r"framework", r"implementation", r"\brepo\b", r"\butxo")),
    ("Ecosystem", (r"ecosystem", r"adoption", r"use case", r"immutable seal", r"participate", r"\barchive\b")),
]
_COMPILED_CATEGORY_PATTERNS = [(cat, [re.compile(p) for p in pats]) for cat, pats in CATEGORY_KEYWORDS]


_REDUNDANT_LABEL_PATTERN = re.compile(
    r"^\s*\**\s*(what happened|why it matters|the takeaway)\s*:\**\s*", re.IGNORECASE
)


def _strip_redundant_label(text: str) -> str:
    """Strips a leading label like '**Why it matters:**' or 'What happened:'
    that some pre-Phase-1 summaries embedded directly in the prose. Our own
    card UI already renders that label as a heading, so leaving it in the
    text would show it twice. Only strips an exact 'Label:' lead-in --
    ordinary sentences that happen to start with similar words (e.g. "Why
    it matters -- ...") are left untouched."""
    if not text:
        return text
    return _REDUNDANT_LABEL_PATTERN.sub("", text).strip()


def classify_category(title: str, summary: str = "") -> str:
    """Deterministic keyword-based category classifier.

    Used both as a fallback for legacy topics that predate AI-generated
    categories (see migrate_legacy_topic) and as a safety net if the AI
    ever returns a category outside CATEGORIES. Checks the title alone
    first (the strongest, least noisy signal); only falls back to the
    combined title+summary text if the title doesn't clearly indicate a
    category. Returns DEFAULT_CATEGORY if nothing matches either pass.
    """
    title_text = f" {title.lower()} "
    for category, patterns in _COMPILED_CATEGORY_PATTERNS:
        if any(p.search(title_text) for p in patterns):
            return category

    combined_text = f" {title.lower()} {summary.lower()} "
    for category, patterns in _COMPILED_CATEGORY_PATTERNS:
        if any(p.search(combined_text) for p in patterns):
            return category

    return DEFAULT_CATEGORY


# ─────────────────────────────────────────────────────────────────────────
# Timestamps & reading time
# ─────────────────────────────────────────────────────────────────────────
UPDATED_AT_FORMAT = "%b %d, %Y %H:%M UTC"
WORDS_PER_MINUTE = 200


def parse_topic_datetime(topic: dict):
    """Best-effort timezone-aware datetime for a topic.

    Prefers the machine-readable `iso_date` field; falls back to parsing
    the human-readable `updated_at` string that every topic (old or new
    schema) is guaranteed to have. Returns None only if both are missing
    or unparseable, so callers must handle that case explicitly rather
    than assuming a timestamp always exists.
    """
    iso_date = topic.get("iso_date")
    if iso_date:
        try:
            dt = datetime.fromisoformat(iso_date)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    updated_at = topic.get("updated_at")
    if updated_at:
        try:
            return datetime.strptime(updated_at, UPDATED_AT_FORMAT).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    return None


def estimate_reading_time(*texts: str) -> int:
    """Reading time in whole minutes for one story.

    Counts words across ALL visible explanatory text for that story (what
    happened + why it matters + what's next, whichever are present) at a
    conservative 200 words/minute, always rounding up, with a 1-minute
    floor so short items never claim "0 min read".
    """
    word_count = sum(len(t.split()) for t in texts if t)
    if word_count == 0:
        return 1
    return max(1, math.ceil(word_count / WORDS_PER_MINUTE))


# ─────────────────────────────────────────────────────────────────────────
# HTML text extraction (unchanged from pre-Phase-1)
# ─────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────
# Data load/save + legacy migration
# ─────────────────────────────────────────────────────────────────────────
def migrate_legacy_topic(topic: dict) -> dict:
    """Backfills Phase 1 structured fields onto a topic stored under the
    pre-Phase-1 schema (flat `summary`, no category/structured fields).

    Deterministic and idempotent: never calls the network or an LLM, never
    overwrites a field that's already present, and is safe to run on every
    topic on every pipeline execution (new-schema topics pass through
    unchanged). This is what lets old data.json entries render correctly
    in the new UI without a costly/impossible re-summarization pass.
    """
    if not topic.get("category"):
        topic["category"] = classify_category(topic.get("title", ""), topic.get("summary", ""))

    if not topic.get("what_happened"):
        summary = (topic.get("summary") or "").strip()
        if "\n\n" in summary:
            what, _, why = summary.partition("\n\n")
            topic["what_happened"] = _strip_redundant_label(what)
            why = _strip_redundant_label(why)
            if why and not topic.get("why_it_matters"):
                topic["why_it_matters"] = why
        else:
            topic["what_happened"] = _strip_redundant_label(summary)
        # whats_next is deliberately left unset for legacy topics -- we have
        # no reliable signal for it and won't fabricate one (see summarize_thread).

    if not topic.get("iso_date"):
        dt = parse_topic_datetime(topic)
        if dt:
            topic["iso_date"] = dt.isoformat()

    return topic


def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                topics = [
                    t for t in data.get("topics", [])
                    if "unavailable" not in t.get("summary", "").lower()
                ]
                data["topics"] = [migrate_legacy_topic(t) for t in topics]
                return data
        except Exception:
            return {"topics": []}
    return {"topics": []}


def save_data(data):
    data["topics"] = data["topics"][:MAX_ITEMS]
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────
# Importance / trending score (Phase 1, Step 6)
# ─────────────────────────────────────────────────────────────────────────
# Deterministic, explainable, and isolated to this section. The score is
# an internal ranking signal only -- it is never shown to the user, so
# there is no false precision to worry about (see compute_importance_score).
RECENCY_HALF_LIFE_HOURS = 48.0  # recency contribution halves every 2 days
MAX_TOP_STORIES = 5
DUPLICATE_TITLE_THRESHOLD = 0.5  # Jaccard similarity of title tokens

# Weights sum to 1.0. Each raw signal is independently normalized to 0-100
# BEFORE weighting, so a single metric (e.g. raw view count) can't dominate.
# Engagement + velocity (0.55 combined) outweigh recency (0.20) on purpose:
# recency alone reaches ~99/100 within the first hour of a post's life (see
# RECENCY_HALF_LIFE_HOURS), so if it were weighted much higher a brand-new
# thread with a single reply could rival a thread with genuinely substantial
# engagement. Recency still matters -- it's why a fresh, actively-replied-to
# thread can outrank an old one with similar totals -- it just can't win on
# its own against real engagement.
SCORE_WEIGHTS = {
    "recency": 0.20,
    "engagement": 0.30,  # reply count
    "views": 0.15,
    "velocity": 0.25,    # replies gained per hour since posting
    "category": 0.10,    # small, capped nudge -- see CATEGORY_WEIGHT
}

# A modest tie-breaker (max contribution at weight 0.10 is +7 of 100
# points) for categories that tend to carry more ecosystem-wide weight.
# This is the "topic importance" signal from the spec, implemented as a
# capped nudge rather than a subjective per-story judgement call.
CATEGORY_WEIGHT = {
    "Development": 70, "Infrastructure": 70, "Governance": 60, "Privacy": 55,
    "DeFi": 50, "Tokens": 50, "AI": 50, "NFTs": 40, "Ecosystem": 40, "Community": 20,
}

_STOPWORDS = {
    "a", "an", "the", "on", "in", "of", "for", "to", "and", "or", "with",
    "is", "are", "new", "using", "towards", "i", "am", "at", "as", "be",
}


def _normalize(value: float, max_value: float) -> float:
    """Scales a non-negative value to 0-100 against the max in the current
    batch. Returns 0 if max_value is 0 (avoids a divide-by-zero when every
    topic in a tiny batch has the same raw value, e.g. all-zero views)."""
    if max_value <= 0:
        return 0.0
    return 100.0 * (value / max_value)


def _compute_batch_stats(topics: list, now: datetime) -> dict:
    """Precomputes the batch-wide maxima used to normalize each topic's
    signals, so normalization is O(n) overall rather than O(n^2)."""
    log_posts, log_views, velocities = [], [], []
    for t in topics:
        log_posts.append(math.log1p(max(0, t.get("posts_count", 0))))
        log_views.append(math.log1p(max(0, t.get("views", 0))))
        dt = parse_topic_datetime(t)
        if dt:
            age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
            velocities.append(max(0, t.get("posts_count", 0)) / max(age_hours, 1.0))
    return {
        "max_log_posts": max(log_posts) if log_posts else 0.0,
        "max_log_views": max(log_views) if log_views else 0.0,
        "max_velocity": max(velocities) if velocities else 0.0,
    }


def compute_importance_score(topic: dict, now: datetime, batch_stats: dict) -> float:
    """Deterministic 0-100 importance score for a single topic, combining:

      - recency:    exponential decay from post time (48h half-life) so
                    nothing stays "important" purely from being old
      - engagement: reply count, log-scaled then normalized against the
                    batch max (log-scaling keeps one 46-reply outlier
                    from completely drowning out everything else)
      - views:      same treatment as engagement, for view count
      - velocity:   replies-per-hour since posting -- rewards threads
                    gaining traction FAST over ones that slowly
                    accumulated the same reply count over weeks
      - category:   small capped nudge, see CATEGORY_WEIGHT

    Weights (SCORE_WEIGHTS) sum to 1.0. We deliberately do NOT ask an LLM
    for a numeric "importance" score anywhere in this pipeline -- a small
    model guessing "importance: 87" is exactly the kind of fake precision
    the Phase 1 spec warns against. Every input here is an objective,
    countable signal.
    """
    posts = max(0, topic.get("posts_count", 0))
    views = max(0, topic.get("views", 0))

    dt = parse_topic_datetime(topic)
    if dt:
        age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
        recency = 100.0 * (0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS))
        velocity_raw = posts / max(age_hours, 1.0)
    else:
        # Unknown timestamp: neutral recency rather than boosting or
        # burying a topic we simply can't date.
        recency = 50.0
        velocity_raw = 0.0

    engagement = _normalize(math.log1p(posts), batch_stats["max_log_posts"])
    views_score = _normalize(math.log1p(views), batch_stats["max_log_views"])
    velocity = _normalize(velocity_raw, batch_stats["max_velocity"])
    category_bonus = CATEGORY_WEIGHT.get(topic.get("category", ""), 0.0)

    score = (
        SCORE_WEIGHTS["recency"] * recency
        + SCORE_WEIGHTS["engagement"] * engagement
        + SCORE_WEIGHTS["views"] * views_score
        + SCORE_WEIGHTS["velocity"] * velocity
        + SCORE_WEIGHTS["category"] * category_bonus
    )
    return round(score, 2)


def _title_tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _is_near_duplicate(title_a: str, title_b: str, threshold: float = DUPLICATE_TITLE_THRESHOLD) -> bool:
    """Jaccard similarity of normalized title tokens. Deliberately tuned to
    catch reworded/reordered repost titles ("KCC20 spec proposal" vs
    "Proposal for the KCC20 spec") without flagging merely-related threads
    in the same topic area ("A privacy coin built on Kaspa" vs "A
    privacy-focused stablecoin" -- different proposals, both legitimate)."""
    tokens_a, tokens_b = _title_tokens(title_a), _title_tokens(title_b)
    if not tokens_a or not tokens_b:
        return False
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b) >= threshold


def select_top_stories(topics: list, scores: dict, max_stories: int = MAX_TOP_STORIES) -> list:
    """Greedily selects up to `max_stories` topics in descending score
    order, skipping any topic that's a near-duplicate of one already
    selected (see _is_near_duplicate) -- this is the "uniqueness /
    duplication penalty" signal from the spec, implemented as dedup at
    selection time rather than baked into the score itself, since it's a
    property of the selected SET, not of a topic in isolation.

    Returns however many distinct, high-signal stories are actually
    available. Never padded to a fixed count: with 0 topics this returns
    an empty list, with 2 topics it returns (at most) 2 -- Top Stories is
    never forced to contain items that don't genuinely stand out.
    """
    ranked = sorted(topics, key=lambda t: scores.get(t["id"], 0), reverse=True)
    selected = []
    for topic in ranked:
        if len(selected) >= max_stories:
            break
        if any(_is_near_duplicate(topic["title"], s["title"]) for s in selected):
            continue
        selected.append(topic)
    return selected


def enrich_and_rank(topics: list):
    """Single entry point for all Phase 1 derived ranking data.

    Returns (scores, top_stories, trend_labels):
      - scores: {topic_id: importance score}, internal use only
      - top_stories: ranked subset of `topics` (see select_top_stories)
      - trend_labels: {topic_id: "Trending" | "Active" | None}

    None of this is written back to data.json. It's recomputed fresh on
    every run because it's inherently RELATIVE -- to "now", and to the
    other topics currently in the batch -- not a stable, intrinsic
    property of a topic the way its category or structured summary are.
    """
    if not topics:
        return {}, [], {}

    now = datetime.now(timezone.utc)
    batch_stats = _compute_batch_stats(topics, now)
    scores = {t["id"]: compute_importance_score(t, now, batch_stats) for t in topics}

    top_stories = select_top_stories(topics, scores)
    top_story_ids = {t["id"] for t in top_stories}

    # "Active" is the next tier down: roughly the top 30% of the batch by
    # score, excluding anything already labeled Trending. Deliberately
    # coarse (three buckets, no numbers shown) rather than a percentile
    # displayed to the user -- see compute_importance_score's docstring on
    # why we avoid displaying a fake-precise number.
    sorted_scores = sorted(scores.values(), reverse=True)
    threshold_index = min(len(sorted_scores) - 1, max(0, int(len(sorted_scores) * 0.3)))
    active_threshold = sorted_scores[threshold_index] if sorted_scores else 0

    trend_labels = {}
    for t in topics:
        if t["id"] in top_story_ids:
            trend_labels[t["id"]] = "Trending"
        elif scores[t["id"]] > 0 and scores[t["id"]] >= active_threshold:
            trend_labels[t["id"]] = "Active"
        else:
            trend_labels[t["id"]] = None

    return scores, top_stories, trend_labels


# ─────────────────────────────────────────────────────────────────────────
# AI summarization (Gemini) -- structured output
# ─────────────────────────────────────────────────────────────────────────
def summarize_thread(topic_title: str, text: str) -> dict:
    """Calls Gemini for a structured summary of a forum thread.

    Returns a dict: what_happened (str), why_it_matters (str or None),
    whats_next (str or None), category (str, one of CATEGORIES), model
    (str -- which Gemini model produced it, or "none" on total failure).

    We deliberately do NOT ask the model for a numeric importance/confidence
    score -- see compute_importance_score's docstring. whats_next is
    explicitly allowed to be null and the prompt instructs the model not to
    speculate: Phase 1's "don't fake intelligence" principle applies as much
    to the generation pipeline as to the frontend.
    """
    fallback = {
        "what_happened": "Summary unavailable at this time.",
        "why_it_matters": None,
        "whats_next": None,
        "category": DEFAULT_CATEGORY,
        "model": "none",
    }
    if not GEMINI_API_KEY:
        fallback["what_happened"] = "Summary unavailable: missing API key."
        return fallback

    prompt = (
        f"Topic Title: {topic_title}\n\n"
        f"Discussion Excerpt:\n{text}\n\n"
        "Summarize this forum thread for a busy reader who has not seen it. "
        "Use plain, concrete language -- no greetings, no filler. "
        "Only fill in whats_next if the excerpt itself clearly describes a "
        "concrete next step (e.g. a planned change, a vote, a follow-up "
        "post); otherwise leave it null. Never speculate or invent a next "
        "step, a prediction, or a statistic that isn't in the excerpt."
    )
    models = ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "what_happened": {
                "type": "STRING",
                "description": "1-2 plain-language sentences on what actually happened, was proposed, or was discussed."
            },
            "why_it_matters": {
                "type": "STRING",
                "nullable": True,
                "description": "One plain-language sentence on why someone following Kaspa should care. Null if genuinely not evident from the excerpt."
            },
            "whats_next": {
                "type": "STRING",
                "nullable": True,
                "description": "One sentence on a concrete next step, ONLY if explicitly evident in the excerpt. Null otherwise -- never speculate."
            },
            "category": {
                "type": "STRING",
                "enum": CATEGORIES,
                "description": "Best-fit single category for this thread."
            },
        },
        "required": ["what_happened", "category"],
    }

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 350,
                "responseMimeType": "application/json",
                "responseSchema": response_schema,
            },
        }

        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=25)
                if resp.status_code == 200:
                    result = resp.json()
                    candidates = result.get("candidates", [])
                    if candidates:
                        raw_json = candidates[0]["content"]["parts"][0]["text"]
                        parsed = json.loads(raw_json)
                        what_happened = (parsed.get("what_happened") or "").strip() or fallback["what_happened"]
                        category = parsed.get("category")
                        if category not in CATEGORIES:
                            category = classify_category(topic_title, what_happened)
                        return {
                            "what_happened": what_happened,
                            "why_it_matters": (parsed.get("why_it_matters") or "").strip() or None,
                            "whats_next": (parsed.get("whats_next") or "").strip() or None,
                            "category": category,
                            "model": model,
                        }
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

    return fallback


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

        result = summarize_thread(t.get("title", ""), full_thread_sample)
        processed += 1
        time.sleep(MIN_SECONDS_BETWEEN_CALLS)

        now_utc = datetime.now(timezone.utc)
        # `summary` stays a flat, human-readable string for backward
        # compatibility (RSS description, any external consumer) --
        # everything else in Phase 1 reads the structured fields instead.
        summary_text = result["what_happened"]
        if result["why_it_matters"]:
            summary_text += "\n\n" + result["why_it_matters"]

        new_summaries.append({
            "id": topic_id,
            "title": t.get("title", ""),
            "url": f"{FORUM_BASE}/t/{t.get('slug', '')}/{topic_id}",
            "summary": summary_text,
            "what_happened": result["what_happened"],
            "why_it_matters": result["why_it_matters"],
            "whats_next": result["whats_next"],
            "category": result["category"],
            "model": result["model"],
            "posts_count": t.get("posts_count", 1),
            "views": t.get("views", 0),
            "is_hot": (t.get("posts_count", 1) >= 8 or t.get("like_count", 0) >= 10 or t.get("views", 0) >= 300),
            "iso_date": now_utc.isoformat(),
            "updated_at": now_utc.strftime("%b %d, %Y %H:%M UTC"),
        })

    if new_summaries:
        data_store["topics"] = new_summaries + data_store["topics"]
        save_data(data_store)

    generate_html(data_store["topics"])
    generate_rss(data_store["topics"])

    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary and new_summaries:
        summary_lines = []
        for t in new_summaries:
            status = "⚠️ fallback" if "unavailable" in t["summary"].lower() else f"✅ ({t.get('model', 'unknown')})"
            summary_lines.append(f"- **{t['title']}**: {status}")

        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write("## Digest Run Summary\n" + "\n".join(summary_lines) + "\n")


def generate_rss(topics):
    items = ""
    for t in topics:
        safe_title = html_lib.escape(t['title'])
        safe_url = html_lib.escape(t['url'])
        safe_summary = html_lib.escape(t['summary'])

        try:
            dt = datetime.fromisoformat(t['iso_date'])
            rfc_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
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
    <title>Katch-Up — Kaspa, caught up.</title>
    <link>https://weirdtualguy.github.io/katch-up/</link>
    <description>The fastest way to understand what's happening in Kaspa, from the Kas-Smiths forum.</description>
    {items}
</channel>
</rss>"""

    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss_content)


# ─────────────────────────────────────────────────────────────────────────
# HTML rendering (Phase 1 information architecture)
# ─────────────────────────────────────────────────────────────────────────
def _is_same_utc_day(dt, now: datetime) -> bool:
    if dt is None:
        return False
    return dt.astimezone(timezone.utc).date() == now.date()


def render_catchup_bar(topics: list, top_stories: list, now: datetime) -> str:
    """Renders the 'What You Missed' summary bar (Phase 1 Step 4 / 13).

    "You missed N discussions" counts topics first-seen TODAY (UTC, by
    generation time) -- the closest defensible approximation to "since you
    were last here" available without user accounts/read-state, which
    Phase 1 explicitly does not add. If nothing is dated today (e.g. a
    quiet day), this falls back to "N discussions open" over the full
    tracked set rather than showing a false "0".

    The catch-up time estimate is deliberately based on Top Stories only,
    not the full list -- summing every tracked topic would defeat the
    product's core promise that you don't need to read everything.
    """
    today_topics = [t for t in topics if _is_same_utc_day(parse_topic_datetime(t), now)]
    if today_topics:
        count = len(today_topics)
        headline = f'You missed <strong>{count}</strong> discussion{"s" if count != 1 else ""} today.'
    else:
        count = len(topics)
        headline = f'<strong>{count}</strong> discussion{"s" if count != 1 else ""} open right now.'

    total_words = sum(
        len((t.get("what_happened") or "").split())
        + len((t.get("why_it_matters") or "").split())
        + len((t.get("whats_next") or "").split())
        for t in top_stories
    )
    if top_stories:
        catchup_minutes = max(1, math.ceil(total_words / WORDS_PER_MINUTE))
        time_line = f"Catch up on the top stories in ~{catchup_minutes} min."
    else:
        time_line = "Nothing stands out enough for Top Stories yet."

    date_str = html_lib.escape(now.strftime("%b %d"))
    iso_day = now.date().isoformat()

    return f'''
        <section class="catchup-bar" aria-label="Digest summary">
            <time class="catchup-date" datetime="{iso_day}">Today &middot; {date_str}</time>
            <p class="catchup-headline">{headline}</p>
            <p class="catchup-time">{time_line}</p>
        </section>'''


def render_category_chips(topics: list) -> str:
    """Renders the horizontally-scrollable category filter row. Only
    categories with at least one current story get a chip (plus "All"),
    so users don't hit dead-end filters for empty categories on a normal
    visit; the empty-filter-state (search + category combos yielding zero
    results) is still handled client-side regardless -- see the <script>."""
    counts = Counter(t.get("category") or DEFAULT_CATEGORY for t in topics)
    chips = [
        f'<button type="button" class="chip active" data-category="all" aria-pressed="true">'
        f'All <span class="chip-count">{len(topics)}</span></button>'
    ]
    for category in CATEGORIES:
        count = counts.get(category, 0)
        if count == 0:
            continue
        safe_cat = html_lib.escape(category)
        chips.append(
            f'<button type="button" class="chip" data-category="{safe_cat}" aria-pressed="false">'
            f'{safe_cat} <span class="chip-count">{count}</span></button>'
        )
    return "\n                ".join(chips)


def _replies_label(posts_count: int) -> str:
    return "reply" if posts_count == 1 else "replies"


def render_top_story_card(topic: dict, rank: int) -> str:
    safe_url = html_lib.escape(topic["url"])
    safe_title = html_lib.escape(topic["title"])
    category = html_lib.escape(topic.get("category") or DEFAULT_CATEGORY)
    what_happened = html_lib.escape(topic.get("what_happened") or topic.get("summary", ""))
    why_it_matters = topic.get("why_it_matters")
    whats_next = topic.get("whats_next")
    reading_time = estimate_reading_time(
        topic.get("what_happened", ""), why_it_matters or "", whats_next or ""
    )
    has_posts_count = topic.get("posts_count") is not None
    posts_count = int(topic.get("posts_count") or 0)
    iso_date = topic.get("iso_date")
    safe_date = html_lib.escape(topic.get("updated_at", ""))

    why_block = ""
    if why_it_matters:
        why_block = f'''
                <div class="story-why">
                    <span class="story-why-label">Why it matters</span>
                    <p>{html_lib.escape(why_it_matters)}</p>
                </div>'''

    next_block = ""
    if whats_next:
        next_block = f'''
                <div class="story-next">
                    <span class="story-next-label">What&#x27;s next</span>
                    <p>{html_lib.escape(whats_next)}</p>
                </div>'''

    footer_bits = []
    if has_posts_count:
        footer_bits.append(f'<span class="story-meta-item">💬 {posts_count} {_replies_label(posts_count)}</span>')
    footer_bits.append(f'<span class="story-meta-item">📖 {reading_time} min read</span>')
    if iso_date:
        footer_bits.append(f'<time class="story-meta-item" datetime="{html_lib.escape(iso_date)}">{safe_date}</time>')
    elif safe_date:
        footer_bits.append(f'<span class="story-meta-item">{safe_date}</span>')

    return f'''
                <article class="top-story-card">
                    <a class="top-story-link" href="{safe_url}" target="_blank" rel="noopener noreferrer" aria-label="{safe_title} — {category}, top story, opens original discussion">
                        <div class="top-story-meta">
                            <span class="story-rank" aria-hidden="true">{rank}</span>
                            <span class="pill pill-category">{category}</span>
                            <span class="pill pill-trend">🔥 Trending</span>
                        </div>
                        <h3 class="top-story-title">{safe_title}</h3>
                        <p class="story-what">{what_happened}</p>{why_block}{next_block}
                        <div class="story-footer">
                            {" ".join(footer_bits)}
                        </div>
                        <span class="read-link" aria-hidden="true">Read discussion →</span>
                    </a>
                </article>'''


def render_discussion_card(topic: dict, trend_label) -> str:
    safe_url = html_lib.escape(topic["url"])
    safe_title = html_lib.escape(topic["title"])
    category = html_lib.escape(topic.get("category") or DEFAULT_CATEGORY)
    what_happened = html_lib.escape(topic.get("what_happened") or topic.get("summary", ""))
    why_it_matters = topic.get("why_it_matters")
    reading_time = estimate_reading_time(topic.get("what_happened", ""), why_it_matters or "")
    has_posts_count = topic.get("posts_count") is not None
    posts_count = int(topic.get("posts_count") or 0)
    iso_date = topic.get("iso_date")
    safe_date = html_lib.escape(topic.get("updated_at", ""))

    if trend_label == "Trending":
        trend_pill = '<span class="pill pill-trend-inline">🔥 Trending</span>'
    elif trend_label == "Active":
        trend_pill = '<span class="pill pill-active-inline">Active</span>'
    else:
        trend_pill = ""

    why_block = ""
    if why_it_matters:
        why_block = (
            f'<p class="card-why"><span class="card-why-label">Why it matters</span>'
            f'{html_lib.escape(why_it_matters)}</p>'
        )

    footer_bits = []
    if has_posts_count:
        footer_bits.append(f'<span class="card-meta-item">💬 {posts_count} {_replies_label(posts_count)}</span>')
    footer_bits.append(f'<span class="card-meta-item">📖 {reading_time} min</span>')
    if iso_date:
        footer_bits.append(f'<time class="card-meta-item" datetime="{html_lib.escape(iso_date)}">{safe_date}</time>')
    elif safe_date:
        footer_bits.append(f'<span class="card-meta-item">{safe_date}</span>')

    return f'''
                <article class="card" data-category="{category}">
                    <a class="card-link" href="{safe_url}" target="_blank" rel="noopener noreferrer" aria-label="{safe_title}">
                        <div class="card-meta">
                            <span class="pill pill-category-inline">{category}</span>{trend_pill}
                        </div>
                        <h3 class="card-title">{safe_title}</h3>
                        <p class="summary">{what_happened}</p>{why_block}
                        <div class="card-footer">
                            {" ".join(footer_bits)}
                        </div>
                    </a>
                </article>'''


PAGE_STYLE = """
        :root {
            --bg: #0f172a; --surface: #1e293b; --surface-raised: #24324a;
            --text-main: #f8fafc; --text-muted: #cbd5e1; --text-dim: #94a3b8;
            --primary: #38bdf8; --primary-dim: rgba(56, 189, 248, 0.14);
            --accent: #f97316; --accent-dim: rgba(249, 115, 22, 0.16);
            --border: #334155; --border-soft: rgba(148, 163, 184, 0.16);
            --radius-lg: 16px; --radius-md: 12px; --radius-sm: 8px;
        }
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg); color: var(--text-main); margin: 0; line-height: 1.5;
            overflow-x: hidden;
        }
        a { color: inherit; }
        button { font: inherit; color: inherit; }
        .visually-hidden {
            position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
            overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
        }
        .skip-link {
            position: absolute; left: -9999px; top: 8px; background: var(--primary); color: #04202f;
            padding: 10px 16px; border-radius: var(--radius-sm); z-index: 100; font-weight: 700;
            text-decoration: none;
        }
        .skip-link:focus { left: 8px; }
        :focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
        .container { max-width: 720px; margin: 0 auto; padding: 0 16px 48px; }

        .site-header { padding: 18px 16px 14px; border-bottom: 1px solid var(--border); }
        .header-inner { max-width: 720px; margin: 0 auto; }
        .header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
        .brand-block { min-width: 0; }
        .logo { display: flex; align-items: center; gap: 6px; font-size: 1.4rem; font-weight: 800; letter-spacing: -0.02em; text-decoration: none; }
        .tagline { margin: 2px 0 0; color: var(--text-muted); font-size: 0.9rem; }
        .header-actions { display: flex; align-items: center; gap: 8px; flex: 1 1 auto; min-width: 180px; justify-content: flex-end; }
        .search-wrap { position: relative; flex: 1 1 auto; max-width: 220px; }
        .search-input {
            width: 100%; background: var(--surface); border: 1px solid var(--border); color: var(--text-main);
            border-radius: 999px; padding: 9px 14px 9px 30px; font-size: 0.87rem; min-height: 40px;
        }
        .search-input::placeholder { color: var(--text-dim); }
        .search-icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--text-dim); font-size: 0.82rem; pointer-events: none; }
        .rss-link {
            color: var(--text-muted); text-decoration: none; font-size: 0.8rem; font-weight: 700;
            border: 1px solid var(--border); border-radius: 999px; padding: 0 14px; min-height: 40px;
            display: inline-flex; align-items: center; white-space: nowrap; flex-shrink: 0;
        }
        .rss-link:hover, .rss-link:focus-visible { color: var(--primary); border-color: var(--primary); }
        .source-line { margin: 10px 0 0; color: var(--text-dim); font-size: 0.76rem; }
        .source-line a { color: var(--text-dim); }

        .catchup-bar {
            margin: 20px 0 28px; padding: 16px 18px;
            background: linear-gradient(135deg, rgba(56,189,248,0.09), rgba(249,115,22,0.05));
            border: 1px solid var(--border-soft); border-radius: var(--radius-lg);
        }
        .catchup-date { display: inline-block; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--primary); margin-bottom: 6px; }
        .catchup-headline { margin: 0 0 4px; font-size: 1.2rem; font-weight: 700; line-height: 1.3; }
        .catchup-headline strong { color: var(--primary); }
        .catchup-time { margin: 0; color: var(--text-muted); font-size: 0.92rem; }

        .section-heading { font-size: 1.05rem; font-weight: 700; margin: 0; }
        .section-heading-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
        .result-count { color: var(--text-dim); font-size: 0.82rem; }
        .top-stories { margin-bottom: 30px; }
        .top-stories .section-heading { margin-bottom: 14px; }

        .top-stories-grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
        @media (min-width: 720px) { .top-stories-grid { grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); } }

        .top-story-card {
            background: var(--surface-raised); border: 1px solid var(--border); border-left: 3px solid var(--primary);
            border-radius: var(--radius-lg); box-shadow: 0 4px 6px -1px rgba(0,0,0,0.25);
        }
        .top-story-link { display: block; padding: 18px; text-decoration: none; color: inherit; border-radius: var(--radius-lg); }
        .top-story-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
        .story-rank {
            display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px;
            border-radius: 50%; background: var(--primary-dim); color: var(--primary); font-size: 0.72rem; font-weight: 800; flex-shrink: 0;
        }
        .pill { font-size: 0.68rem; font-weight: 700; padding: 3px 9px; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
        .pill-category { background: rgba(148,163,184,0.16); color: var(--text-muted); }
        .pill-trend { background: var(--accent-dim); color: var(--accent); }
        .top-story-title { margin: 0 0 8px; font-size: 1.12rem; line-height: 1.35; font-weight: 700; overflow-wrap: break-word; }
        .top-story-link:hover .top-story-title, .top-story-link:focus-visible .top-story-title { color: var(--primary); }
        .story-what { margin: 0 0 10px; color: #e2e8f0; font-size: 0.93rem; line-height: 1.55; }
        .story-why { background: rgba(56,189,248,0.07); border-left: 2px solid var(--primary); padding: 8px 12px; border-radius: 0 var(--radius-sm) var(--radius-sm) 0; margin-bottom: 10px; }
        .story-next { padding: 8px 12px; margin-bottom: 10px; border-left: 2px solid var(--border); }
        .story-why-label, .story-next-label { display: block; font-size: 0.66rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--primary); margin-bottom: 2px; }
        .story-next-label { color: var(--text-dim); }
        .story-why p, .story-next p { margin: 0; font-size: 0.88rem; color: var(--text-muted); line-height: 1.5; }
        .story-footer { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; color: var(--text-dim); font-size: 0.78rem; padding-top: 10px; border-top: 1px solid var(--border-soft); margin-top: 4px; }
        .read-link { display: inline-block; margin-top: 10px; color: var(--primary); font-size: 0.85rem; font-weight: 700; }

        .filter-section { margin-bottom: 22px; }
        .category-scroll { display: flex; gap: 8px; overflow-x: auto; padding: 4px 2px 8px; -webkit-overflow-scrolling: touch; scrollbar-width: thin; }
        @media (prefers-reduced-motion: no-preference) { .category-scroll { scroll-behavior: smooth; } }
        .chip {
            flex: 0 0 auto; display: inline-flex; align-items: center; gap: 6px; background: var(--surface);
            border: 1px solid var(--border); color: var(--text-muted); padding: 0 14px; min-height: 40px;
            border-radius: 999px; font-size: 0.84rem; font-weight: 600; cursor: pointer; white-space: nowrap;
            transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
        }
        .chip:hover { border-color: var(--primary); color: var(--text-main); }
        .chip.active { background: var(--primary); border-color: var(--primary); color: #04202f; }
        .chip-count { font-size: 0.74rem; opacity: 0.8; font-weight: 700; }

        .discussion-grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
        @media (min-width: 640px) { .discussion-grid { grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); } }
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: 0 2px 4px -1px rgba(0,0,0,0.15); }
        .card-link { display: block; padding: 16px; color: inherit; text-decoration: none; border-radius: var(--radius-md); }
        .card-meta { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
        .pill-category-inline { background: rgba(148,163,184,0.13); color: var(--text-dim); }
        .pill-trend-inline { background: var(--accent-dim); color: var(--accent); }
        .pill-active-inline { background: var(--primary-dim); color: var(--primary); }
        .card-title { margin: 0 0 8px; font-size: 1.01rem; line-height: 1.35; font-weight: 700; overflow-wrap: break-word; }
        .card-link:hover .card-title, .card-link:focus-visible .card-title { color: var(--primary); }
        .summary { margin: 0 0 10px; color: #cbd5e1; font-size: 0.87rem; line-height: 1.5; }
        .card-why { margin: 0 0 10px; font-size: 0.84rem; color: var(--text-muted); line-height: 1.5; padding-left: 10px; border-left: 2px solid var(--border); }
        .card-why-label { font-weight: 700; color: var(--text-dim); text-transform: uppercase; font-size: 0.66rem; letter-spacing: 0.05em; margin-right: 4px; }
        .card-footer { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; color: var(--text-dim); font-size: 0.76rem; border-top: 1px solid var(--border-soft); padding-top: 8px; }

        .empty-filter-state { text-align: center; padding: 36px 16px; color: var(--text-muted); }
        .link-button { background: none; border: none; color: var(--primary); font-weight: 700; cursor: pointer; padding: 8px 12px; text-decoration: underline; font-size: 0.9rem; min-height: 40px; }
        .no-data-state { padding: 36px 16px; text-align: center; color: var(--text-muted); }

        .site-footer { max-width: 720px; margin: 8px auto 0; padding: 20px 16px 32px; border-top: 1px solid var(--border); color: var(--text-dim); font-size: 0.8rem; text-align: center; }
        .site-footer a { color: var(--text-muted); }

        @media (max-width: 380px) {
            .container { padding: 0 12px 40px; }
            .site-header { padding: 16px 12px 12px; }
            .catchup-headline { font-size: 1.1rem; }
            .header-actions { justify-content: flex-start; width: 100%; }
            .search-wrap { max-width: none; }
        }

        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.01ms !important; animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important; scroll-behavior: auto !important;
            }
        }
"""

PAGE_SCRIPT = """
    (function () {
        var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
        var cards = Array.prototype.slice.call(document.querySelectorAll('#discussionGrid .card'));
        var searchInput = document.getElementById('searchInput');
        var resultCount = document.getElementById('resultCount');
        var emptyState = document.getElementById('emptyState');
        var clearFiltersBtn = document.getElementById('clearFilters');
        var discussionGrid = document.getElementById('discussionGrid');
        var activeCategory = 'all';

        function normalize(s) { return (s || '').toLowerCase(); }

        function applyFilters() {
            var query = normalize(searchInput ? searchInput.value : '');
            var visibleCount = 0;

            cards.forEach(function (card) {
                var category = card.getAttribute('data-category') || '';
                var matchesCategory = activeCategory === 'all' || category === activeCategory;
                var matchesSearch = query === '' || normalize(card.textContent).indexOf(query) !== -1;
                var visible = matchesCategory && matchesSearch;
                card.hidden = !visible;
                if (visible) visibleCount++;
            });

            if (resultCount) {
                resultCount.textContent = visibleCount + (visibleCount === 1 ? ' discussion' : ' discussions');
            }
            if (emptyState) emptyState.hidden = visibleCount !== 0;
            if (discussionGrid) discussionGrid.hidden = visibleCount === 0;
        }

        chips.forEach(function (chip) {
            chip.addEventListener('click', function () {
                chips.forEach(function (c) {
                    c.classList.remove('active');
                    c.setAttribute('aria-pressed', 'false');
                });
                chip.classList.add('active');
                chip.setAttribute('aria-pressed', 'true');
                activeCategory = chip.getAttribute('data-category');
                applyFilters();
            });
        });

        if (searchInput) searchInput.addEventListener('input', applyFilters);

        if (clearFiltersBtn) {
            clearFiltersBtn.addEventListener('click', function () {
                activeCategory = 'all';
                if (searchInput) searchInput.value = '';
                chips.forEach(function (c) {
                    var isAll = c.getAttribute('data-category') === 'all';
                    c.classList.toggle('active', isAll);
                    c.setAttribute('aria-pressed', isAll ? 'true' : 'false');
                });
                applyFilters();
            });
        }
    })();
"""


def _page_shell(body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Katch-Up is the fastest way to understand what's happening in Kaspa: top stories, why they matter, and where to read more.">
    <meta property="og:title" content="Katch-Up">
    <meta property="og:description" content="The fastest way to understand what's happening in Kaspa.">
    <meta property="og:type" content="website">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚡</text></svg>">
    <link rel="alternate" type="application/rss+xml" title="Kas-Smiths Digest RSS" href="feed.xml">
    <title>Katch-Up — Kaspa, caught up.</title>
    <style>{PAGE_STYLE}</style>
</head>
<body>
    <a href="#main" class="skip-link">Skip to content</a>
    <header class="site-header">
        <div class="header-inner">
            <div class="header-row">
                <div class="brand-block">
                    <span class="logo">⚡ Katch-Up</span>
                    <p class="tagline">Kaspa, caught up.</p>
                </div>
                <div class="header-actions">
                    <div class="search-wrap">
                        <label for="searchInput" class="visually-hidden">Search discussions</label>
                        <span class="search-icon" aria-hidden="true">🔍</span>
                        <input type="search" id="searchInput" class="search-input" placeholder="Search discussions">
                    </div>
                    <a href="feed.xml" class="rss-link" aria-label="RSS feed">RSS</a>
                </div>
            </div>
            <p class="source-line">Powered by <a href="{FORUM_BASE}">Kas-Smiths</a></p>
        </div>
    </header>
    <main id="main" class="container">
{body_html}
    </main>
    <footer class="site-footer">
        <p>Katch-Up tracks public discussions on <a href="{FORUM_BASE}">Kas-Smiths</a>. The forum is always the authoritative place to read and reply.</p>
    </footer>
    <script>{PAGE_SCRIPT}</script>
</body>
</html>"""


def generate_html(topics):
    if not topics:
        body_html = '''
        <section class="no-data-state" aria-label="Digest summary">
            <p>No discussions tracked yet.</p>
            <p>Check back soon — Katch-Up refreshes every few hours.</p>
        </section>'''
        html_content = _page_shell(body_html)
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        return

    now = datetime.now(timezone.utc)
    scores, top_stories, trend_labels = enrich_and_rank(topics)

    catchup_html = render_catchup_bar(topics, top_stories, now)

    top_stories_html = ""
    if top_stories:
        story_cards = "".join(render_top_story_card(t, i + 1) for i, t in enumerate(top_stories))
        top_stories_html = f'''
        <section class="top-stories" aria-labelledby="topStoriesHeading">
            <h2 id="topStoriesHeading" class="section-heading">🔥 Top Stories</h2>
            <div class="top-stories-grid">{story_cards}
            </div>
        </section>'''

    chips_html = render_category_chips(topics)
    cards_html = "".join(render_discussion_card(t, trend_labels.get(t["id"])) for t in topics)

    body_html = f'''{catchup_html}
{top_stories_html}
        <section class="filter-section" aria-label="Filter discussions">
            <div class="category-scroll" role="group" aria-label="Filter by category">
                {chips_html}
            </div>
        </section>
        <section class="all-discussions" aria-labelledby="allDiscussionsHeading">
            <div class="section-heading-row">
                <h2 id="allDiscussionsHeading" class="section-heading">All Discussions</h2>
                <span class="result-count" id="resultCount">{len(topics)} discussions</span>
            </div>
            <div class="discussion-grid" id="discussionGrid">{cards_html}
            </div>
            <div class="empty-filter-state" id="emptyState" hidden>
                <p>No discussions match your filters.</p>
                <button type="button" class="link-button" id="clearFilters">Clear filters</button>
            </div>
        </section>'''

    html_content = _page_shell(body_html)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)


if __name__ == "__main__":
    fetch_and_process()
