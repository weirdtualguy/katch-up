import os
import re
import math
import json
import time
import random
import requests
import html as html_lib
import urllib.parse
from html.parser import HTMLParser
from collections import Counter
from datetime import datetime, timezone, timedelta

FORUM_BASE = "https://kas-smiths.org"
SITE_BASE = "https://weirdtualguy.github.io/katch-up/"
DATA_FILE = "data.json"
# Phase 1 treated this as a rolling-window cap that silently deleted older
# topics. Phase 2 needs permanent, shareable story URLs and historical
# navigation, so the archive is no longer pruned in normal operation --
# this is now just a runaway-growth safety valve at a scale far beyond
# anything this project will realistically reach.
MAX_ITEMS = 5000
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", 10))
# How many ALREADY-KNOWN topics we're willing to re-check and (if changed)
# re-summarize per run, on top of MAX_NEW_PER_RUN brand-new ones. This is
# what powers "What Changed" -- see check_for_topic_updates().
MAX_UPDATED_PER_RUN = int(os.environ.get("MAX_UPDATED_PER_RUN", 8))
MIN_SECONDS_BETWEEN_CALLS = 4.5
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Output layout (Phase 2). Everything is still plain static files -- no
# server, no routing library. GitHub Pages serves any directory containing
# an index.html at that directory's path, which is what gives us clean
# URLs like /story/74-slug/ and /day/2026-08-23/ for free.
ASSETS_DIR = "assets"
STORY_DIR = "story"
DAY_DIR = "day"
SEARCH_DIR = "search"
CSS_PATH = f"{ASSETS_DIR}/katch-up.css"
JS_PATH = f"{ASSETS_DIR}/katch-up.js"


def slugify(title: str, max_len: int = 60) -> str:
    """Deterministic, URL-safe slug. Assigned ONCE per topic (see
    migrate_legacy_topic) and never regenerated, so a story's URL stays
    stable even if its title is later edited on the forum."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0]
    return s or "story"


def story_slug_dir(topic: dict) -> str:
    """The '<id>-<slug>' directory name for a topic's permanent story page.
    The numeric id prefix is what actually guarantees stability/uniqueness;
    the slug is just for readability."""
    return f"{topic['id']}-{topic.get('slug') or slugify(topic.get('title', ''))}"


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
    """Backfills Phase 1 + Phase 2 structured fields onto a topic stored
    under an older schema (flat `summary`, no category/structured fields,
    no slug/history).

    Deterministic and idempotent: never calls the network or an LLM, never
    overwrites a field that's already present, and is safe to run on every
    topic on every pipeline execution (already-migrated topics pass through
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

    # Phase 2 additions --------------------------------------------------
    if not topic.get("slug"):
        topic["slug"] = slugify(topic.get("title", ""))

    if not topic.get("last_updated_date"):
        # A topic that predates re-visit tracking has only ever had one
        # snapshot: its first one. last_updated_date is the UTC date of
        # the CURRENT (top-level) what_happened/posts_count/etc.
        dt = parse_topic_datetime(topic)
        topic["last_updated_date"] = dt.date().isoformat() if dt else None

    if "history" not in topic:
        # Prior snapshots, oldest first. Empty for every topic that hasn't
        # been re-summarized since Phase 2 shipped -- see
        # check_for_topic_updates(). Never fabricated.
        topic["history"] = []

    return topic


def get_topic_days(topic: dict) -> set:
    """The set of UTC calendar-day strings (YYYY-MM-DD) a topic should
    appear under in the daily archive: the day it was first seen, plus any
    day it was meaningfully updated (see check_for_topic_updates). A topic
    that resurfaces with real news on a later day is supposed to resurface
    in that day's digest too -- that's the whole premise of "What Changed"."""
    days = set()
    first_seen = parse_topic_datetime(topic)
    if first_seen:
        days.add(first_seen.date().isoformat())
    if topic.get("last_updated_date"):
        days.add(topic["last_updated_date"])
    for entry in topic.get("history", []):
        if entry.get("date"):
            days.add(entry["date"])
    return days


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

# Phase 2 story labels. Deliberately just four, each with a concrete,
# data-grounded rule (see compute_story_labels) -- the brief's other
# suggested labels ("Breaking", "Important") don't have a signal in this
# data that would justify them without either faking urgency a 6-hourly
# batch job can't really detect, or inventing a vague "importance" gauge
# on top of the one we already have. A story gets AT MOST one label
# (priority order below), never a stack of badges.
NEW_THRESHOLD_HOURS = 6.0  # matches the digest's own ~6h run cadence
DEEP_DIVE_MIN_READING_MINUTES = 3
HIGHLY_DISCUSSED_MIN_POSTS = 5  # absolute floor, see compute_story_labels
HIGHLY_DISCUSSED_PERCENTILE = 0.75

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


def enrich_and_rank(topics: list, reference_time: datetime = None):
    """Single entry point for all derived ranking + labeling data.

    Returns (scores, top_stories, trend_labels):
      - scores: {topic_id: importance score}, internal use only
      - top_stories: ranked subset of `topics` (see select_top_stories)
      - trend_labels: {topic_id: "Trending" | "Highly Discussed" |
        "Deep Dive" | "New" | None} -- see compute_story_labels

    `reference_time` defaults to now, but historical day pages pass the
    end of that UTC day instead, so a five-day-old story's recency/"New"
    status is computed relative to the day being viewed, not to whenever
    the site happens to be rebuilt. Nothing here is written back to
    data.json -- it's recomputed on demand because it's inherently
    RELATIVE (to a point in time, and to the other topics in the batch),
    not a stable, intrinsic property of a topic the way its category is.
    """
    if not topics:
        return {}, [], {}

    now = reference_time or datetime.now(timezone.utc)
    batch_stats = _compute_batch_stats(topics, now)
    scores = {t["id"]: compute_importance_score(t, now, batch_stats) for t in topics}

    top_stories = select_top_stories(topics, scores)
    trend_labels = compute_story_labels(topics, scores, top_stories, now)

    return scores, top_stories, trend_labels


def compute_story_labels(topics: list, scores: dict, top_stories: list, now: datetime) -> dict:
    """One restrained, data-grounded label per story (or none). Priority
    when a story would qualify for more than one: Trending beats Highly
    Discussed beats Deep Dive beats New -- each check only runs for
    stories that didn't already earn a higher-priority label, so a story
    never shows more than one badge.

      - Trending: it's a genuine Top Story (see select_top_stories) --
        already reflects recency + engagement + velocity together.
      - Highly Discussed: reply count is both a meaningful absolute number
        (>= HIGHLY_DISCUSSED_MIN_POSTS, so a quiet batch can't call a
        2-reply thread "highly discussed" just because everything else
        has zero) AND in the top quartile of the current batch.
      - Deep Dive: long-form -- its reading time meets
        DEEP_DIVE_MIN_READING_MINUTES. An absolute, not batch-relative,
        threshold, since "long" doesn't depend on what else is around it.
      - New: first seen within NEW_THRESHOLD_HOURS of `now`.
    """
    top_story_ids = {t["id"] for t in top_stories}

    posts_counts = sorted((t.get("posts_count", 0) for t in topics), reverse=True)
    idx = min(len(posts_counts) - 1, max(0, int(len(posts_counts) * (1 - HIGHLY_DISCUSSED_PERCENTILE))))
    percentile_floor = posts_counts[idx] if posts_counts else 0
    highly_discussed_floor = max(HIGHLY_DISCUSSED_MIN_POSTS, percentile_floor)

    labels = {}
    for t in topics:
        tid = t["id"]
        if tid in top_story_ids:
            labels[tid] = "Trending"
            continue

        if t.get("posts_count", 0) >= highly_discussed_floor:
            labels[tid] = "Highly Discussed"
            continue

        reading_minutes = estimate_reading_time(t.get("what_happened", ""), t.get("why_it_matters") or "")
        if reading_minutes >= DEEP_DIVE_MIN_READING_MINUTES:
            labels[tid] = "Deep Dive"
            continue

        dt = parse_topic_datetime(t)
        if dt and (now - dt).total_seconds() / 3600.0 <= NEW_THRESHOLD_HOURS:
            labels[tid] = "New"
            continue

        labels[tid] = None

    return labels


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
    existing_by_id = {t["id"]: t for t in data_store.get("topics", [])}
    existing_ids = set(existing_by_id.keys())

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
            "slug": slugify(t.get("title", "")),
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
            "last_updated_date": now_utc.date().isoformat(),
            "history": [],
        })

    if new_summaries:
        data_store["topics"] = new_summaries + data_store["topics"]
        save_data(data_store)

    updated_topics = check_for_topic_updates(topics_meta, data_store, headers)
    if updated_topics:
        save_data(data_store)

    generate_site(data_store["topics"])

    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary and (new_summaries or updated_topics):
        summary_lines = []
        for t in new_summaries:
            status = "⚠️ fallback" if "unavailable" in t["summary"].lower() else f"✅ ({t.get('model', 'unknown')})"
            summary_lines.append(f"- **{t['title']}**: {status}")
        for t in updated_topics:
            summary_lines.append(f"- **{t['title']}**: 🔄 updated ({t.get('model', 'unknown')})")

        with open(gh_summary, "a", encoding="utf-8") as f:
            f.write("## Digest Run Summary\n" + "\n".join(summary_lines) + "\n")


def check_for_topic_updates(topics_meta: list, data_store: dict, headers: dict) -> list:
    """Re-checks up to MAX_UPDATED_PER_RUN already-known topics for genuine
    new activity, and re-summarizes the ones that changed. This is what
    makes "What Changed" possible -- Phase 1 only ever looked at a topic
    once, so a thread that kept accumulating replies would show the same
    stale summary forever.

    A topic qualifies for re-summarization only if ALL of:
      - it's already in the archive (brand-new topics are handled elsewhere)
      - the forum now reports more replies than we have on file (i.e.
        something genuinely happened -- we never re-summarize on a whim)
      - we haven't already recorded an update for it today (caps "What
        Changed" at day-granularity, matching its own "Yesterday / Today"
        framing, and stops a single busy thread from consuming the whole
        run's update budget across repeated 6-hourly checks)

    On a qualifying topic, the CURRENT what_happened/why_it_matters/
    posts_count/views are pushed into that topic's `history` (dated with
    its own last_updated_date) before being overwritten -- so history[-1]
    is always "the previous state" and the top-level fields are always
    "now". Returns the list of topics that were actually updated.
    """
    existing_by_id = {t["id"]: t for t in data_store.get("topics", [])}
    meta_by_id = {t.get("id"): t for t in topics_meta}
    today_str = datetime.now(timezone.utc).date().isoformat()

    updated = []
    checked = 0

    for topic_id, meta in meta_by_id.items():
        if checked >= MAX_UPDATED_PER_RUN:
            break

        stored = existing_by_id.get(topic_id)
        if not stored:
            continue  # brand new, not our concern here

        api_posts_count = meta.get("posts_count")
        stored_posts_count = stored.get("posts_count", 0)
        if api_posts_count is None or api_posts_count <= stored_posts_count:
            continue  # nothing new
        if stored.get("last_updated_date") == today_str:
            continue  # already recorded an update today

        thread_resp = requests.get(f"{FORUM_BASE}/t/{topic_id}.json", headers=headers, timeout=15)
        checked += 1
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

        result = summarize_thread(stored.get("title", ""), full_thread_sample)
        time.sleep(MIN_SECONDS_BETWEEN_CALLS)

        if "unavailable" in result["what_happened"].lower():
            continue  # don't archive a failed re-summarization as "new" history

        # Archive the OLD state before overwriting it.
        stored.setdefault("history", []).append({
            "date": stored.get("last_updated_date") or (stored.get("iso_date") or "")[:10],
            "what_happened": stored.get("what_happened", ""),
            "why_it_matters": stored.get("why_it_matters"),
            "posts_count": stored_posts_count,
            "views": stored.get("views", 0),
        })

        summary_text = result["what_happened"]
        if result["why_it_matters"]:
            summary_text += "\n\n" + result["why_it_matters"]

        stored["summary"] = summary_text
        stored["what_happened"] = result["what_happened"]
        stored["why_it_matters"] = result["why_it_matters"]
        stored["whats_next"] = result["whats_next"]
        stored["posts_count"] = meta.get("posts_count", stored_posts_count)
        stored["views"] = meta.get("views", stored.get("views", 0))
        stored["is_hot"] = (
            stored["posts_count"] >= 8 or meta.get("like_count", 0) >= 10 or stored["views"] >= 300
        )
        stored["last_updated_date"] = today_str
        stored["model"] = result["model"]
        updated.append(stored)

    return updated


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


def build_day_index(topics: list) -> dict:
    """Groups topics by every UTC day they belong to (see get_topic_days --
    a topic can appear under more than one day if it was meaningfully
    updated later). Returns {date_str: [topics]}, each list sorted newest
    first by that day's own relevance (first-seen or last-updated,
    whichever is on that day). Built once per run and reused for the home
    page, every day archive page, and figuring out which day counts as
    "today" for the home page (see resolve_home_day)."""
    index = {}
    for t in topics:
        for day in get_topic_days(t):
            index.setdefault(day, []).append(t)
    return index


def resolve_home_day(day_index: dict, now: datetime):
    """Which day the home page ('/') should show: today if today already
    has coverage, otherwise the most recent day that does. This keeps the
    home page from ever going blank in the few hours after UTC midnight
    before the bot's next run, while always being honest (via the
    returned is_today flag) about which day is actually on screen -- see
    render_catchup_bar."""
    today_str = now.date().isoformat()
    if day_index.get(today_str):
        return today_str, True
    if not day_index:
        return today_str, True
    latest = max(day_index.keys())
    return latest, (latest == today_str)


def render_catchup_bar(day_topics: list, top_stories: list, day_date, is_today: bool) -> str:
    """Renders the 'What You Missed' summary bar (Phase 1 Step 4 / 13),
    generalized in Phase 2 to work for a historical day archive as much as
    for today (see generate_day_pages) -- `day_topics` is always the
    already-resolved set for whichever single day is being rendered.

    The catch-up time estimate is deliberately based on Top Stories only,
    not the full list -- summing every tracked topic would defeat the
    product's core promise that you don't need to read everything.
    """
    count = len(day_topics)
    if is_today:
        headline = f'You missed <strong>{count}</strong> discussion{"s" if count != 1 else ""} today.' if count \
            else "No new discussions today yet."
        date_label = f"Today &middot; {html_lib.escape(day_date.strftime('%b %d'))}"
    else:
        headline = f'<strong>{count}</strong> discussion{"s" if count != 1 else ""} from this day.' if count \
            else "No Katch-Up coverage for this date."
        date_label = html_lib.escape(day_date.strftime("%A, %b %d"))

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
        time_line = "Nothing stands out enough for Top Stories." if count else ""

    iso_day = day_date.isoformat()

    return f'''
        <section class="catchup-bar" aria-label="Digest summary">
            <time class="catchup-date" datetime="{iso_day}">{date_label}</time>
            <h1 class="catchup-headline">{headline}</h1>
            {f'<p class="catchup-time">{time_line}</p>' if time_line else ''}
        </section>'''


def render_date_nav(day_index: dict, day_date, is_today: bool, prefix: str, is_home_page: bool = False) -> str:
    """The '← Aug 22  Aug 23  Aug 24 →' style strip (Phase 2 Step 6/7).
    Prev/next links are computed from the actual archive at BUILD time and
    baked in as plain <a href> -- no client-side date picker, no manifest
    fetch, works with JS entirely disabled. Never links to a future date,
    and never links to a past date with no coverage. `is_home_page`
    suppresses the "back to today" link on the home page itself, where
    it would just link back to the page you're already on."""
    available = sorted(day_index.keys())
    idx = available.index(day_date.isoformat()) if day_date.isoformat() in available else -1

    prev_day = available[idx - 1] if idx > 0 else None
    next_day = available[idx + 1] if 0 <= idx < len(available) - 1 else None

    def day_link(day_str, label, arrow_class):
        return f'<a class="date-nav-link {arrow_class}" href="{prefix}{DAY_DIR}/{day_str}/">{label}</a>'

    parts = []
    if prev_day:
        parts.append(day_link(prev_day, f"← {_format_short_date(prev_day)}", "date-nav-prev"))
    else:
        parts.append('<span class="date-nav-link date-nav-disabled" aria-hidden="true">←</span>')

    parts.append(f'<span class="date-nav-current">{_format_short_date(day_date.isoformat())}</span>')

    if next_day:
        parts.append(day_link(next_day, f"{_format_short_date(next_day)} →", "date-nav-next"))
    else:
        parts.append('<span class="date-nav-link date-nav-disabled" aria-hidden="true">→</span>')

    today_link = ""
    if not is_today and not is_home_page:
        today_link = f'<a class="date-nav-today" href="{prefix}">Back to today</a>'

    return f'''
        <nav class="date-nav" aria-label="Browse previous days">
            <div class="date-nav-strip">{"".join(parts)}</div>
            {today_link}
        </nav>'''


def _format_short_date(day_str: str) -> str:
    try:
        return datetime.strptime(day_str, "%Y-%m-%d").strftime("%b %d")
    except ValueError:
        return day_str


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


# Each label's icon/text/CSS class in one place, since it's now used by
# both card types plus the story page. Color is never the only signal --
# every pill carries its own text label alongside the color (Phase 2
# accessibility requirement: don't communicate status with color alone).
_LABEL_PILL_META = {
    "Trending": ("🔥", "Trending", "pill-trend"),
    "Highly Discussed": ("💬", "Highly Discussed", "pill-discussed"),
    "Deep Dive": ("🧠", "Deep Dive", "pill-deepdive"),
    "New": ("⚡", "New", "pill-new"),
}


def label_pill(label, inline: bool = False) -> str:
    """Renders a single restrained label pill, or '' for no label. `inline`
    selects the smaller card-footer treatment vs. the top-story-card size."""
    if not label or label not in _LABEL_PILL_META:
        return ""
    icon, text, css_class = _LABEL_PILL_META[label]
    suffix = "-inline" if inline else ""
    return f'<span class="pill {css_class}{suffix}">{icon} {html_lib.escape(text)}</span>'


def render_top_story_card(topic: dict, rank: int, label, prefix: str = "") -> str:
    story_href = f"{prefix}{STORY_DIR}/{story_slug_dir(topic)}/"
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
                    <a class="top-story-link" href="{html_lib.escape(story_href)}" aria-label="{safe_title} — {category}, top story">
                        <div class="top-story-meta">
                            <span class="story-rank" aria-hidden="true">{rank}</span>
                            <span class="pill pill-category">{category}</span>
                            {label_pill(label)}
                        </div>
                        <h3 class="top-story-title">{safe_title}</h3>
                        <p class="story-what">{what_happened}</p>{why_block}{next_block}
                        <div class="story-footer">
                            {" ".join(footer_bits)}
                        </div>
                        <span class="read-link" aria-hidden="true">Open story →</span>
                    </a>
                </article>'''


def render_discussion_card(topic: dict, trend_label, prefix: str = "") -> str:
    story_href = f"{prefix}{STORY_DIR}/{story_slug_dir(topic)}/"
    safe_title = html_lib.escape(topic["title"])
    category = html_lib.escape(topic.get("category") or DEFAULT_CATEGORY)
    what_happened = html_lib.escape(topic.get("what_happened") or topic.get("summary", ""))
    why_it_matters = topic.get("why_it_matters")
    reading_time = estimate_reading_time(topic.get("what_happened", ""), why_it_matters or "")
    has_posts_count = topic.get("posts_count") is not None
    posts_count = int(topic.get("posts_count") or 0)
    iso_date = topic.get("iso_date")
    safe_date = html_lib.escape(topic.get("updated_at", ""))

    trend_pill = label_pill(trend_label, inline=True)

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
                    <a class="card-link" href="{html_lib.escape(story_href)}" aria-label="{safe_title}">
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


def render_what_changed(topic: dict) -> str:
    """The 'What Changed' section (Phase 2 Step 9): the immediately-prior
    snapshot next to the current one, both in the story's own recorded
    words -- never a generated diff narrative, since asking an LLM to
    characterize "what changed" risks inventing a shift that isn't
    actually evidenced. Renders '' (nothing) whenever a topic has no
    history yet, which is true of every topic until it's been through
    check_for_topic_updates() at least once -- gracefully absent, never
    faked, per the spec's explicit warning against inventing changes.
    """
    history = topic.get("history") or []
    if not history:
        return ""

    previous = history[-1]
    entries = [
        (previous.get("date", ""), previous.get("what_happened", ""), False),
        (topic.get("last_updated_date", ""), topic.get("what_happened", ""), True),
    ]
    rendered = []
    for date_str, text, is_current in entries:
        if not text:
            continue
        label = "Today" if is_current else _format_short_date(date_str)
        css_class = "is-current" if is_current else ""
        rendered.append(f'''
            <div class="what-changed-entry">
                <time class="what-changed-date {css_class}" datetime="{html_lib.escape(date_str)}">{html_lib.escape(label)}</time>
                <p>{html_lib.escape(text)}</p>
            </div>''')

    if len(rendered) < 2:
        return ""

    return f'''
        <section class="what-changed story-section" aria-labelledby="whatChangedHeading">
            <h2 id="whatChangedHeading" class="story-section-heading">What Changed</h2>
            {"".join(rendered)}
        </section>'''


def render_continuity(topic: dict, prefix: str) -> str:
    """'Related coverage' timeline (Phase 2 Step 10) -- only rendered once
    a story has at least two prior snapshots PLUS its current state (three
    or more total points), since with just one prior snapshot this would
    just repeat What Changed in list form. Links each point to the day
    archive for that date rather than duplicating content here."""
    history = topic.get("history") or []
    if len(history) < 2:
        return ""

    points = list(history) + [{
        "date": topic.get("last_updated_date", ""),
        "what_happened": topic.get("what_happened", ""),
    }]

    items = []
    for point in points:
        date_str = point.get("date", "")
        if not date_str:
            continue
        excerpt = (point.get("what_happened") or "")[:80]
        if len(point.get("what_happened") or "") > 80:
            excerpt += "…"
        items.append(f'''
                <li class="continuity-item">
                    <time datetime="{html_lib.escape(date_str)}">{html_lib.escape(_format_short_date(date_str))}</time>
                    <a href="{prefix}{DAY_DIR}/{html_lib.escape(date_str)}/">{html_lib.escape(excerpt)}</a>
                </li>''')

    if not items:
        return ""

    return f'''
        <section class="story-section" aria-labelledby="continuityHeading">
            <h2 id="continuityHeading" class="story-section-heading">Related Coverage</h2>
            <ul class="continuity-list">{"".join(items)}
            </ul>
        </section>'''


def get_topic_label_in_context(topic: dict, day_index: dict):
    """The label a story shows on its own page: computed within the batch
    of whichever day it most recently belonged to, so a story page never
    shows a different label than the day/home page card that links to it."""
    days = sorted(get_topic_days(topic))
    if not days:
        return None
    latest_day_str = days[-1]
    day_topics = day_index.get(latest_day_str) or [topic]
    today_str = datetime.now(timezone.utc).date().isoformat()
    reference_time = (
        datetime.now(timezone.utc) if latest_day_str == today_str
        else _end_of_day_utc(datetime.strptime(latest_day_str, "%Y-%m-%d").date())
    )
    _, _, labels = enrich_and_rank(day_topics, reference_time=reference_time)
    return labels.get(topic["id"])


def render_story_page(topic: dict, day_index: dict, prefix: str) -> str:
    safe_title = html_lib.escape(topic["title"])
    category = html_lib.escape(topic.get("category") or DEFAULT_CATEGORY)
    what_happened = topic.get("what_happened") or topic.get("summary", "")
    why_it_matters = topic.get("why_it_matters")
    whats_next = topic.get("whats_next")
    label = get_topic_label_in_context(topic, day_index)
    reading_time = estimate_reading_time(what_happened, why_it_matters or "", whats_next or "")

    posts_count = topic.get("posts_count")
    views = topic.get("views")
    iso_date = topic.get("iso_date")
    safe_date = html_lib.escape(topic.get("updated_at", ""))

    first_seen_day = (iso_date or "")[:10] or topic.get("last_updated_date", "")
    back_href = f"{prefix}{DAY_DIR}/{first_seen_day}/" if first_seen_day else prefix

    why_block = ""
    if why_it_matters:
        why_block = f'''
        <section class="story-section why-matters" aria-labelledby="whyHeading">
            <h2 id="whyHeading" class="story-section-heading">Why It Matters</h2>
            <p>{html_lib.escape(why_it_matters)}</p>
        </section>'''

    next_block = ""
    if whats_next:
        next_block = f'''
        <section class="story-section whats-next" aria-labelledby="nextHeading">
            <h2 id="nextHeading" class="story-section-heading">What&#x27;s Next</h2>
            <p>{html_lib.escape(whats_next)}</p>
        </section>'''

    what_changed_html = render_what_changed(topic)
    continuity_html = render_continuity(topic, prefix)

    meta_stats = []
    if posts_count is not None:
        meta_stats.append(f'<div class="story-meta-stat"><strong>{int(posts_count)}</strong>💬 {_replies_label(int(posts_count))}</div>')
    if views is not None:
        meta_stats.append(f'<div class="story-meta-stat"><strong>{int(views)}</strong>👁 views</div>')
    meta_stats.append(f'<div class="story-meta-stat"><strong>{reading_time}</strong>📖 min read</div>')

    date_html = (
        f'<time datetime="{html_lib.escape(iso_date)}">{safe_date}</time>' if iso_date
        else f'<span>{safe_date}</span>' if safe_date else ""
    )

    safe_report_subject = html_lib.escape(
        urllib.parse.quote(f"Katch-Up summary report: {topic['title']}"), quote=True
    )
    safe_report_body = html_lib.escape(
        urllib.parse.quote(
            f"Story: {topic['title']}\nURL: {SITE_BASE}{STORY_DIR}/{story_slug_dir(topic)}/\n\nWhat seems inaccurate:\n"
        ),
        quote=True,
    )

    return f'''
        <a class="back-link" href="{back_href}">← Back to Katch-Up</a>
        <article>
            <div class="story-hero-meta">
                <span class="pill pill-category">{category}</span>
                {label_pill(label)}
            </div>
            <h1 class="story-hero-title">{safe_title}</h1>
            <div class="story-hero-sub">
                {date_html}
                <span>{reading_time} min read</span>
            </div>

            <section class="story-section" aria-labelledby="whatHeading">
                <h2 id="whatHeading" class="story-section-heading">What Happened</h2>
                <p>{html_lib.escape(what_happened)}</p>
            </section>
            {why_block}
            {next_block}
            {what_changed_html}
            {continuity_html}

            <div class="story-meta-row">
                {"".join(meta_stats)}
            </div>

            <div class="source-box">
                <div class="source-box-label">Source</div>
                <div class="source-box-name">Kas-Smiths</div>
                <a class="source-cta" href="{html_lib.escape(topic['url'])}" target="_blank" rel="noopener noreferrer">Read Original Discussion →</a>
            </div>

            <p class="ai-disclosure">
                AI-generated summary based on the original discussion. It may not capture every detail —
                <a class="report-link" href="mailto:?subject={safe_report_subject}&amp;body={safe_report_body}">report an issue</a>.
            </p>
        </article>'''


def generate_story_pages(all_topics: list, day_index: dict = None):
    """One PERMANENT page per archived topic at /story/<id>-<slug>/,
    regenerated on every run (all_topics is the whole archive, not just
    new ones) since a topic's label, its position in What Changed, or its
    continuity timeline can all shift as later runs add history. This is
    what gives every story a stable, shareable URL that keeps working
    even once the topic is no longer "recent" -- see Phase 2 Step 1/2.
    """
    day_index = day_index if day_index is not None else build_day_index(all_topics)
    prefix = "../../"

    for topic in all_topics:
        body_html = render_story_page(topic, day_index, prefix)
        desc_source = topic.get("what_happened") or topic.get("summary", "")
        meta_description = (desc_source[:157] + "...") if len(desc_source) > 160 else desc_source
        html_content = page_shell(
            body_html, prefix=prefix,
            title=f"{topic['title']} — Katch-Up",
            meta_description=meta_description or "A Kaspa ecosystem discussion, summarized by Katch-Up.",
            canonical_path=f"{STORY_DIR}/{story_slug_dir(topic)}/",
            og_type="article",
        )
        out_dir = os.path.join(STORY_DIR, story_slug_dir(topic))
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html_content)


def build_search_index(all_topics: list) -> list:
    """Compact search index embedded inline on the search page (see
    generate_search_page) -- inline rather than fetched separately so the
    page works even opened as a bare local file (fetch() of a sibling
    JSON file is blocked by CORS under file://, which inline JSON isn't
    subject to), and so there's no extra round trip on a slow connection.

    Each entry carries just what the client-side ranker in katch-up.js
    needs: t=title, s=body snippet (what happened + why it matters,
    truncated), c=category, u=story URL, d=display line, r=a 0-10 recency
    bucket, imp=the existing importance score -- the last two are only
    ever used as small tie-breakers (see SHARED_JS's scoreItem), never to
    override an actual textual match.
    """
    now = datetime.now(timezone.utc)
    scores, _, _ = enrich_and_rank(all_topics, reference_time=now)

    index = []
    for t in all_topics:
        body = f"{t.get('what_happened', '')} {t.get('why_it_matters') or ''}".strip()
        snippet = (body[:137] + "...") if len(body) > 140 else body

        dt = parse_topic_datetime(t)
        if dt:
            age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
            recency_bucket = round(10 * (0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS)), 2)
            display_date = _format_short_date(dt.date().isoformat())
        else:
            recency_bucket = 0
            display_date = ""

        index.append({
            "t": t.get("title", ""),
            "s": snippet,
            "c": t.get("category") or DEFAULT_CATEGORY,
            "u": f"../{STORY_DIR}/{story_slug_dir(t)}/",
            "d": f"{t.get('category') or DEFAULT_CATEGORY} · {display_date}" if display_date else (t.get("category") or DEFAULT_CATEGORY),
            "r": recency_bucket,
            "imp": scores.get(t["id"], 0),
        })
    return index


def generate_search_page(all_topics: list):
    """Dedicated /search/ page (Phase 2 Step 4). Ranking and rendering
    both happen client-side in katch-up.js against the inline index built
    above -- no server, no separate search library, deterministic and
    inspectable. Scoped to text search only: it does not also expose the
    category chips or date navigation, per the spec's own prioritization
    ("if complex combinations are impractical, prioritize: 1. Search,
    2. Date, 3. Category") -- documented as an intentional limitation
    rather than half-building a three-way filter combination.
    """
    prefix = "../"
    index_json = json.dumps(build_search_index(all_topics), ensure_ascii=False, separators=(",", ":"))

    body_html = f'''
        <section class="search-hero">
            <h1 class="section-heading" style="margin-bottom:12px;">Search Katch-Up</h1>
            <div class="search-page-input-wrap">
                <span class="search-page-icon" aria-hidden="true">🔍</span>
                <label for="searchPageInput" class="visually-hidden">Search all discussions</label>
                <input type="search" id="searchPageInput" class="search-page-input" placeholder="Search titles, summaries, categories…" autocomplete="off">
            </div>
            <p class="search-results-count" id="searchResultsCount"></p>
        </section>
        <div class="discussion-grid" id="searchResults" hidden></div>
        <div class="search-empty" id="searchEmptyState" hidden>
            <p>No discussions found for &quot;<span data-query></span>&quot;.</p>
            <p>Try another search, or <a class="link-button" id="searchBrowseAll" href="{prefix}">browse all discussions</a>.</p>
        </div>
        <script type="application/json" id="searchIndexData">{index_json}</script>'''

    html_content = page_shell(
        body_html, prefix=prefix, title="Search — Katch-Up",
        meta_description="Search summarized Kaspa ecosystem discussions across the full Katch-Up archive.",
        canonical_path=f"{SEARCH_DIR}/",
    )
    os.makedirs(SEARCH_DIR, exist_ok=True)
    with open(os.path.join(SEARCH_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_content)


def generate_assets():
    """Writes the shared CSS/JS as real linked files (assets/katch-up.*)
    instead of inlining them on every page -- see page_shell's docstring
    for why this matters once the site has more than one page."""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    with open(CSS_PATH, "w", encoding="utf-8") as f:
        f.write(SHARED_CSS)
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write(SHARED_JS)


def generate_site(all_topics: list):
    """Single entry point that (re)builds every static page from the
    current archive: shared assets, home, every day archive, every story
    page, and search. Called on every pipeline run regardless of whether
    anything actually changed, since a topic's label/ranking/What-Changed
    section can shift even when ITS OWN data didn't (e.g. a sibling
    topic's update changes the day's Top Stories cutoff)."""
    generate_assets()
    day_index = build_day_index(all_topics)
    generate_html(all_topics, day_index)
    generate_day_pages(all_topics, day_index)
    generate_story_pages(all_topics, day_index)
    generate_search_page(all_topics)
    generate_rss(all_topics)


SHARED_CSS = """

        :root {
            --bg: #0f172a; --surface: #1e293b; --surface-raised: #24324a;
            --text-main: #f8fafc; --text-muted: #cbd5e1; --text-dim: #94a3b8;
            --primary: #38bdf8; --primary-dim: rgba(56, 189, 248, 0.14);
            --accent: #f97316; --accent-dim: rgba(249, 115, 22, 0.16);
            --discussed: #eab308; --discussed-dim: rgba(234, 179, 8, 0.16);
            --deepdive: #a78bfa; --deepdive-dim: rgba(167, 139, 250, 0.16);
            --new: #4ade80; --new-dim: rgba(74, 222, 128, 0.16);
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
        .rss-link, .search-page-link {
            color: var(--text-muted); text-decoration: none; font-size: 0.8rem; font-weight: 700;
            border: 1px solid var(--border); border-radius: 999px; padding: 0 14px; min-height: 40px;
            display: inline-flex; align-items: center; white-space: nowrap; flex-shrink: 0;
        }
        .rss-link:hover, .rss-link:focus-visible, .search-page-link:hover, .search-page-link:focus-visible { color: var(--primary); border-color: var(--primary); }
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

        .date-nav { margin: 0 0 20px; }
        .date-nav-strip {
            display: flex; align-items: center; justify-content: space-between; gap: 8px;
            background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md);
            padding: 4px; font-size: 0.86rem;
        }
        .date-nav-link {
            color: var(--text-muted); text-decoration: none; font-weight: 600; padding: 8px 12px;
            border-radius: var(--radius-sm); min-height: 40px; display: inline-flex; align-items: center;
        }
        .date-nav-link:hover, .date-nav-link:focus-visible { color: var(--primary); background: var(--primary-dim); }
        .date-nav-disabled { color: var(--text-dim); opacity: 0.4; padding: 8px 12px; }
        .date-nav-current { font-weight: 700; color: var(--text-main); padding: 8px 12px; }
        .date-nav-today {
            display: inline-flex; align-items: center; margin-top: 4px; color: var(--primary); font-size: 0.82rem;
            font-weight: 700; text-decoration: none; padding: 10px 4px; min-height: 40px; box-sizing: border-box;
        }
        .date-nav-today:hover { text-decoration: underline; }

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
        .pill-trend, .pill-trend-inline { background: var(--accent-dim); color: var(--accent); }
        .pill-discussed, .pill-discussed-inline { background: var(--discussed-dim); color: var(--discussed); }
        .pill-deepdive, .pill-deepdive-inline { background: var(--deepdive-dim); color: var(--deepdive); }
        .pill-new, .pill-new-inline { background: var(--new-dim); color: var(--new); }
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

        /* Story page ------------------------------------------------- */
        .back-link {
            display: inline-flex; align-items: center; gap: 6px; color: var(--text-muted); text-decoration: none;
            font-size: 0.86rem; font-weight: 600; margin: 18px 0 6px; min-height: 40px;
        }
        .back-link:hover, .back-link:focus-visible { color: var(--primary); }
        .story-hero-meta { display: flex; align-items: center; gap: 8px; margin: 10px 0 12px; flex-wrap: wrap; }
        .story-hero-title { font-size: 1.5rem; line-height: 1.3; font-weight: 800; margin: 0 0 10px; overflow-wrap: break-word; }
        .story-hero-sub { display: flex; align-items: center; gap: 10px; color: var(--text-dim); font-size: 0.84rem; margin-bottom: 22px; flex-wrap: wrap; }
        .story-section { margin-bottom: 22px; }
        .story-section-heading { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--primary); margin: 0 0 8px; }
        .story-section p { margin: 0; font-size: 1rem; line-height: 1.65; color: var(--text-main); }
        .story-section.why-matters { padding: 14px 16px; background: rgba(56,189,248,0.06); border-left: 3px solid var(--primary); border-radius: 0 var(--radius-md) var(--radius-md) 0; }
        .story-section.whats-next { padding: 14px 16px; border-left: 3px solid var(--border); }
        .story-meta-row {
            display: flex; align-items: center; gap: 20px; flex-wrap: wrap; padding: 16px 0;
            border-top: 1px solid var(--border-soft); border-bottom: 1px solid var(--border-soft); margin-bottom: 24px;
        }
        .story-meta-stat { font-size: 0.86rem; color: var(--text-muted); }
        .story-meta-stat strong { display: block; font-size: 1.1rem; color: var(--text-main); }
        .source-box { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 18px; margin-bottom: 24px; }
        .source-box-label { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--text-dim); margin-bottom: 6px; }
        .source-box-name { font-weight: 700; margin-bottom: 12px; }
        .source-cta {
            display: inline-flex; align-items: center; gap: 6px; background: var(--primary); color: #04202f;
            text-decoration: none; font-weight: 700; padding: 0 18px; min-height: 44px; border-radius: 999px; font-size: 0.92rem;
        }
        .source-cta:hover, .source-cta:focus-visible { filter: brightness(1.08); }
        .what-changed { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px 18px; margin-bottom: 22px; }
        .what-changed-entry { padding: 10px 0; }
        .what-changed-entry + .what-changed-entry { border-top: 1px solid var(--border-soft); }
        .what-changed-date { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); display: block; margin-bottom: 4px; }
        .what-changed-date.is-current { color: var(--primary); }
        .what-changed-entry p { margin: 0; font-size: 0.9rem; color: var(--text-muted); line-height: 1.55; }
        .continuity-list { list-style: none; margin: 0; padding: 0; }
        .continuity-item { display: flex; gap: 10px; padding: 6px 0; font-size: 0.86rem; color: var(--text-muted); }
        .continuity-item time { color: var(--text-dim); flex-shrink: 0; width: 62px; }
        .ai-disclosure { color: var(--text-dim); font-size: 0.78rem; margin: 28px 0 6px; padding-top: 16px; border-top: 1px solid var(--border-soft); }
        .report-link {
            color: var(--text-dim); font-size: 0.78rem; text-decoration: underline;
            display: inline-block; padding: 10px 2px; margin: -10px -2px; box-sizing: content-box;
        }
        .report-link:hover, .report-link:focus-visible { color: var(--text-muted); }
        .story-unavailable { padding: 40px 16px; text-align: center; color: var(--text-muted); }

        /* Search page -------------------------------------------------- */
        .search-hero { margin: 20px 0 24px; }
        .search-page-input-wrap { position: relative; }
        .search-page-input {
            width: 100%; background: var(--surface); border: 1px solid var(--border); color: var(--text-main);
            border-radius: var(--radius-md); padding: 14px 16px 14px 42px; font-size: 1.05rem; min-height: 52px;
        }
        .search-page-input::placeholder { color: var(--text-dim); }
        .search-page-icon { position: absolute; left: 14px; top: 50%; transform: translateY(-50%); color: var(--text-dim); font-size: 1rem; pointer-events: none; }
        .search-results-count { color: var(--text-dim); font-size: 0.84rem; margin: 16px 0 10px; }
        .search-empty { padding: 40px 16px; text-align: center; color: var(--text-muted); }
        .search-empty p:first-child { font-weight: 700; color: var(--text-main); margin-bottom: 6px; }

        @media (max-width: 380px) {
            .container { padding: 0 12px 40px; }
            .site-header { padding: 16px 12px 12px; }
            .catchup-headline { font-size: 1.1rem; }
            .header-actions { justify-content: flex-start; width: 100%; }
            .search-wrap { max-width: none; }
            .story-hero-title { font-size: 1.3rem; }
        }

        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.01ms !important; animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important; scroll-behavior: auto !important;
            }
        }
"""


SHARED_JS = """
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

        // Header search: typing still does the instant same-page quick
        // filter above (when a #discussionGrid exists on this page); Enter
        // always jumps to the full archive-wide search page. KATCHUP_PREFIX
        // is set by a tiny inline script on every page (see page_shell) so
        // this one shared file works at any directory depth.
        if (searchInput) {
            searchInput.addEventListener('keydown', function (e) {
                if (e.key !== 'Enter') return;
                var q = searchInput.value.trim();
                var prefix = window.KATCHUP_PREFIX || '';
                window.location.href = prefix + 'search/' + (q ? '?q=' + encodeURIComponent(q) : '');
            });
        }
    })();

    // ---------------------------------------------------------------------
    // Search results page. No-ops entirely if #searchResults isn't on the
    // page, so this is safe to ship in the one shared JS file loaded
    // everywhere rather than a second per-page script.
    // ---------------------------------------------------------------------
    (function () {
        var resultsEl = document.getElementById('searchResults');
        if (!resultsEl) return;

        var indexScript = document.getElementById('searchIndexData');
        var items = [];
        try { items = indexScript ? JSON.parse(indexScript.textContent) : []; } catch (e) { items = []; }

        var input = document.getElementById('searchPageInput');
        var countEl = document.getElementById('searchResultsCount');
        var emptyEl = document.getElementById('searchEmptyState');
        var browseLink = document.getElementById('searchBrowseAll');

        function normalize(s) { return (s || '').toLowerCase(); }

        function escapeHtml(s) {
            return (s || '').replace(/[&<>"']/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
            });
        }

        // Deterministic, explainable ranking: a 7-slot tuple compared most-
        // to-least significant (title exact match, title word hits, body
        // exact match, body word hits, category match, recency, importance
        // score). Title always outranks body; exact phrase always outranks
        // scattered word hits within the same field; recency and the
        // existing importance score only ever break ties among results
        // that are already textually comparable -- popularity alone can
        // never push an unrelated story above a real textual match.
        function scoreItem(item, queryLower, queryWords) {
            var titleLower = normalize(item.t);
            var bodyLower = normalize(item.s);
            var catLower = normalize(item.c);

            var titleExact = titleLower.indexOf(queryLower) !== -1 ? 1 : 0;
            var titleWordHits = queryWords.filter(function (w) { return titleLower.indexOf(w) !== -1; }).length;
            var bodyExact = bodyLower.indexOf(queryLower) !== -1 ? 1 : 0;
            var bodyWordHits = queryWords.filter(function (w) { return bodyLower.indexOf(w) !== -1; }).length;
            var categoryMatch = (catLower === queryLower || catLower.indexOf(queryLower) !== -1) ? 1 : 0;

            return {
                item: item,
                relevant: titleExact || titleWordHits || bodyExact || bodyWordHits || categoryMatch,
                tuple: [titleExact, titleWordHits, bodyExact, bodyWordHits, categoryMatch, item.r || 0, item.imp || 0]
            };
        }

        function compareTuples(a, b) {
            for (var i = 0; i < a.length; i++) {
                if (b[i] !== a[i]) return b[i] - a[i];
            }
            return 0;
        }

        function renderResults(query) {
            var queryLower = normalize(query.trim());
            if (!queryLower) {
                resultsEl.innerHTML = '';
                resultsEl.hidden = true;
                if (countEl) countEl.textContent = '';
                if (emptyEl) emptyEl.hidden = true;
                return;
            }

            var queryWords = queryLower.split(/\\s+/).filter(Boolean);
            var scored = items.map(function (item) { return scoreItem(item, queryLower, queryWords); })
                .filter(function (r) { return r.relevant; })
                .sort(function (a, b) { return compareTuples(a.tuple, b.tuple); });

            resultsEl.hidden = false;
            if (countEl) {
                countEl.textContent = scored.length + (scored.length === 1 ? ' result' : ' results') + ' for "' + query.trim() + '"';
            }

            if (scored.length === 0) {
                resultsEl.innerHTML = '';
                if (emptyEl) {
                    emptyEl.hidden = false;
                    var q = emptyEl.querySelector('[data-query]');
                    if (q) q.textContent = query.trim();
                }
                return;
            }
            if (emptyEl) emptyEl.hidden = true;

            resultsEl.innerHTML = scored.map(function (r) {
                var item = r.item;
                return '<a class="card-link search-result-item" href="' + item.u + '">' +
                    '<article class="card">' +
                    '<div class="card-meta"><span class="pill pill-category-inline">' + escapeHtml(item.c) + '</span></div>' +
                    '<h3 class="card-title">' + escapeHtml(item.t) + '</h3>' +
                    '<p class="summary">' + escapeHtml(item.s) + '</p>' +
                    '<div class="card-footer"><span class="card-meta-item">' + escapeHtml(item.d) + '</span></div>' +
                    '</article></a>';
            }).join('');
        }

        function debounce(fn, wait) {
            var t;
            return function () {
                var args = arguments;
                clearTimeout(t);
                t = setTimeout(function () { fn.apply(null, args); }, wait);
            };
        }

        var debouncedRender = debounce(function () { renderResults(input.value); }, 120);

        if (input) {
            input.addEventListener('input', debouncedRender);
            var params = new URLSearchParams(window.location.search);
            var initialQuery = params.get('q') || '';
            if (initialQuery) {
                input.value = initialQuery;
                renderResults(initialQuery);
            }
            input.focus();
        }
        if (browseLink) {
            browseLink.addEventListener('click', function () {
                if (input) { input.value = ''; }
                renderResults('');
            });
        }
    })();
"""



def _end_of_day_utc(day_date) -> datetime:
    return datetime.combine(day_date, datetime.max.time()).replace(microsecond=0, tzinfo=timezone.utc)


def page_shell(body_html: str, *, prefix: str, title: str, meta_description: str,
                canonical_path: str = "", og_type: str = "website", extra_head: str = "") -> str:
    """General page wrapper for every Phase 2 page type (home, day archive,
    story, search). CSS/JS are now linked shared assets (assets/katch-up.*)
    rather than inlined per page, so the browser caches them once across
    the whole site instead of re-downloading the same ~15KB on every
    navigation -- the main Phase 2 performance-hardening change.

    `prefix` is this page's relative path back to the site root (e.g. ""
    for the home page, "../../" for a story or day page, "../" for
    search) and is used for every cross-page link and asset reference, so
    the site works correctly however deep GitHub Pages nests it.
    """
    canonical_url = f"{SITE_BASE}{canonical_path}" if canonical_path else SITE_BASE
    safe_title = html_lib.escape(title)
    safe_desc = html_lib.escape(meta_description)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="{safe_desc}">
    <meta property="og:title" content="{safe_title}">
    <meta property="og:description" content="{safe_desc}">
    <meta property="og:type" content="{og_type}">
    <meta property="og:url" content="{html_lib.escape(canonical_url)}">
    <link rel="canonical" href="{html_lib.escape(canonical_url)}">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚡</text></svg>">
    <link rel="alternate" type="application/rss+xml" title="Katch-Up RSS" href="{prefix}feed.xml">
    <link rel="stylesheet" href="{prefix}{CSS_PATH}">
    {extra_head}
    <title>{safe_title}</title>
</head>
<body>
    <a href="#main" class="skip-link">Skip to content</a>
    <header class="site-header">
        <div class="header-inner">
            <div class="header-row">
                <div class="brand-block">
                    <a class="logo" href="{prefix}">⚡ Katch-Up</a>
                    <p class="tagline">Kaspa, caught up.</p>
                </div>
                <div class="header-actions">
                    <div class="search-wrap">
                        <label for="searchInput" class="visually-hidden">Search discussions</label>
                        <span class="search-icon" aria-hidden="true">🔍</span>
                        <input type="search" id="searchInput" class="search-input" placeholder="Search…">
                    </div>
                    <a href="{prefix}{SEARCH_DIR}/" class="search-page-link" aria-label="Full search">Search</a>
                    <a href="{prefix}feed.xml" class="rss-link" aria-label="RSS feed">RSS</a>
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
    <script>window.KATCHUP_PREFIX = "{prefix}";</script>
    <script src="{prefix}{JS_PATH}"></script>
</body>
</html>"""


def render_digest_body(day_topics: list, day_index: dict, day_date, is_today: bool,
                        prefix: str, is_home_page: bool) -> str:
    """The shared 'Katch-Up for a given day' body: date nav, catch-up bar,
    Top Stories, category filters, full discussion list. Used identically
    by the home page (today, or the most recent day with coverage) and
    every historical day archive page -- per the Phase 2 spec, the archive
    is explicitly NOT a separate product, it's this same view for a
    different day.
    """
    show_date_nav = len(day_index) > 1 or (not is_today and not is_home_page)
    nav_html = render_date_nav(day_index, day_date, is_today, prefix, is_home_page) if show_date_nav else ""

    if not day_topics:
        catchup_html = render_catchup_bar([], [], day_date, is_today)
        message = "No discussions tracked yet." if is_home_page else "No Katch-Up coverage for this date."
        return f'''{nav_html}
{catchup_html}
        <section class="no-data-state" aria-label="Digest summary">
            <p>{message}</p>
            <p>Try browsing to another day, or check back soon.</p>
        </section>'''

    reference_time = datetime.now(timezone.utc) if is_today else _end_of_day_utc(day_date)
    scores, top_stories, trend_labels = enrich_and_rank(day_topics, reference_time=reference_time)

    catchup_html = render_catchup_bar(day_topics, top_stories, day_date, is_today)

    top_stories_html = ""
    if top_stories:
        story_cards = "".join(
            render_top_story_card(t, i + 1, trend_labels.get(t["id"]), prefix)
            for i, t in enumerate(top_stories)
        )
        top_stories_html = f'''
        <section class="top-stories" aria-labelledby="topStoriesHeading">
            <h2 id="topStoriesHeading" class="section-heading">🔥 Top Stories</h2>
            <div class="top-stories-grid">{story_cards}
            </div>
        </section>'''

    chips_html = render_category_chips(day_topics)
    cards_html = "".join(render_discussion_card(t, trend_labels.get(t["id"]), prefix) for t in day_topics)

    return f'''{nav_html}
{catchup_html}
{top_stories_html}
        <section class="filter-section" aria-label="Filter discussions">
            <div class="category-scroll" role="group" aria-label="Filter by category">
                {chips_html}
            </div>
        </section>
        <section class="all-discussions" aria-labelledby="allDiscussionsHeading">
            <div class="section-heading-row">
                <h2 id="allDiscussionsHeading" class="section-heading">All Discussions</h2>
                <span class="result-count" id="resultCount">{len(day_topics)} discussions</span>
            </div>
            <div class="discussion-grid" id="discussionGrid">{cards_html}
            </div>
            <div class="empty-filter-state" id="emptyState" hidden>
                <p>No discussions match your filters.</p>
                <button type="button" class="link-button" id="clearFilters">Clear filters</button>
            </div>
        </section>'''


def generate_html(all_topics: list, day_index: dict = None):
    day_index = day_index if day_index is not None else build_day_index(all_topics)
    now = datetime.now(timezone.utc)
    home_day_str, is_today = resolve_home_day(day_index, now)
    home_day = datetime.strptime(home_day_str, "%Y-%m-%d").date()
    day_topics = day_index.get(home_day_str, [])

    body_html = render_digest_body(day_topics, day_index, home_day, is_today, prefix="", is_home_page=True)
    html_content = page_shell(
        body_html, prefix="", title="Katch-Up — Kaspa, caught up.",
        meta_description="Katch-Up is the fastest way to understand what's happening in Kaspa: top stories, why they matter, and where to read more.",
        canonical_path="",
    )
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)


def generate_day_pages(all_topics: list, day_index: dict = None):
    """One permanent page per archived day at /day/<YYYY-MM-DD>/, reusing
    render_digest_body so it's pixel-for-pixel the same experience as the
    home page was on that day -- see Phase 2 Step 7 ("Katch-Up for that
    day", not a separate product)."""
    day_index = day_index if day_index is not None else build_day_index(all_topics)
    today_str = datetime.now(timezone.utc).date().isoformat()

    for day_str, day_topics in day_index.items():
        day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
        is_today = day_str == today_str
        prefix = "../../"

        body_html = render_digest_body(day_topics, day_index, day_date, is_today, prefix=prefix, is_home_page=False)
        page_title = f"Katch-Up — {day_date.strftime('%B %-d, %Y')}" if os.name != "nt" else f"Katch-Up — {day_date.strftime('%B %d, %Y')}"
        html_content = page_shell(
            body_html, prefix=prefix, title=page_title,
            meta_description=f"What happened in Kaspa on {day_date.strftime('%B %-d, %Y')}: top stories and discussions from Katch-Up's daily archive.",
            canonical_path=f"{DAY_DIR}/{day_str}/",
        )
        out_dir = os.path.join(DAY_DIR, day_str)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html_content)




if __name__ == "__main__":
    fetch_and_process()
