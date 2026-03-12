import csv
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import anthropic
import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

ALERT_DISCORD_WEBHOOK_URL = os.getenv("ALERT_DISCORD_WEBHOOK_URL", "").strip()
CSV_DISCORD_WEBHOOK_URL = os.getenv("CSV_DISCORD_WEBHOOK_URL", "").strip()
TEST_DISCORD_WEBHOOK_URL = os.getenv("TEST_DISCORD_WEBHOOK_URL", "").strip()
ALL_DISCORD_WEBHOOK_URL = os.getenv("ALL_DISCORD_WEBHOOK_URL", "").strip()
SUBREDDIT = os.getenv("SUBREDDIT", "Watchexchange").strip()
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))
MAX_POST_AGE_MINUTES = int(os.getenv("MAX_POST_AGE_MINUTES", "15"))
CSV_UPLOAD_HOUR = int(os.getenv("CSV_UPLOAD_HOUR", "0"))
CSV_UPLOAD_MINUTE = int(os.getenv("CSV_UPLOAD_MINUTE", "5"))
CSV_DIRNAME = os.getenv("CSV_DIRNAME", "daily_csv").strip()

MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "").strip()
MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
MINIMAX_MODEL = "MiniMax-M2.5"

STATE_FILE = Path("seen_posts.json")
CSV_STATE_FILE = Path("csv_upload_state.json")
CSV_DIR = Path(CSV_DIRNAME)
RSS_URL = f"https://www.reddit.com/r/{SUBREDDIT}/new/.rss"
USER_AGENT = "mac:watchexchange-alert-bot:v7.0"

# Initialize Anthropic client with MiniMax
anthropic_client = anthropic.Anthropic(
    api_key=MINIMAX_API_KEY,
    base_url=MINIMAX_BASE_URL,
)


def load_seen_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("seen_ids", []))
    except Exception:
        return set()


def save_seen_ids(seen_ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps({"seen_ids": sorted(seen_ids)}, indent=2))


def load_csv_upload_state() -> dict:
    if not CSV_STATE_FILE.exists():
        return {}
    try:
        return json.loads(CSV_STATE_FILE.read_text())
    except Exception:
        return {}


def save_csv_upload_state(state: dict) -> None:
    CSV_STATE_FILE.write_text(json.dumps(state, indent=2))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_price_number(text: str) -> float | None:
    """
    Tries to extract a numeric USD-ish price from text.
    Examples:
      $950
      $1,280
      USD 850
      900 shipped
    """
    patterns = [
        r"\$\s?(\d[\d,]*)",
        r"\bUSD\s?(\d[\d,]*)\b",
        r"\b(\d[\d,]*)\s?(?:shipped|obo|net)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            raw = match.group(1).replace(",", "")
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def extract_price(text: str) -> str | None:
    patterns = [
        r"\$\s?\d[\d,]*",
        r"\bUSD\s?\d[\d,]*\b",
        r"\b\d[\d,]*\s?(?:shipped|obo|net)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def matches_keywords(title: str, summary: str) -> bool:
    """
    Base match logic:
    - standard keyword match
    - OR any mention of 1993
    - OR Seiko under $1000
    """
    haystack = normalize_text(f"{title} {summary}")

    # Rule 1: explicit 1993 mention
    if "1993" in haystack:
        return True

    # Rule 2: Seiko under $1000
    if "seiko" in haystack:
        price_num = parse_price_number(f"{title} {summary}")
        if price_num is not None and price_num < 1000:
            return True

    # Rule 3: normal keyword list (with whole-word matching using regex)
    if not KEYWORDS:
        return True

    for keyword in KEYWORDS:
        # Use word boundaries to ensure we match whole words only
        # This prevents "ap" from matching "formex" or "keep"
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, haystack):
            return True

    return False


def parse_post_age_minutes(entry) -> float | None:
    published = entry.get("published") or entry.get("updated")
    if not published:
        return None
    try:
        dt = parsedate_to_datetime(published)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return None


def clean_summary(summary: str) -> str:
    text = re.sub(r"<[^>]+>", " ", summary or "")
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    noise_patterns = [
        r"submitted by.*",
        r"\[link\].*",
        r"\[comments\].*",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    return text.strip()


def get_match_reason(title: str, summary: str) -> str:
    haystack = normalize_text(f"{title} {summary}")

    if "1993" in haystack:
        return "Matched special rule: mentions 1993"

    if "seiko" in haystack:
        price_num = parse_price_number(f"{title} {summary}")
        if price_num is not None and price_num < 1000:
            return f"Matched special rule: Seiko under $1000 ({int(price_num) if price_num.is_integer() else price_num})"

    matched_keywords = [kw for kw in KEYWORDS if kw in haystack]
    if matched_keywords:
        return f"Matched keywords: {', '.join(matched_keywords[:3])}"

    return "Matched general filter"


def score_with_claude(title: str, summary: str, url: str, published: str, price: str | None) -> dict:
    prompt = f"""
You are evaluating a watch listing from Reddit r/{SUBREDDIT} for whether it is UNDER MARKET.

Return ONLY valid JSON with this exact schema:
{{
  "score": <integer 1-10>,
  "should_alert": <true or false>,
  "reason": "<one short sentence>",
  "brand": "<brand or unknown>",
  "model_guess": "<model guess or unknown>",
  "listing_price": "<price from listing or unknown>",
  "estimated_market_price": "<single estimated fair market price or range>",
  "is_likely_under_market": <true or false>,
  "under_market_amount": "<amount or range below market, else unknown>"
}}

Scoring guidance:
- 9-10: strong under-market steal
- 7-8: probably a good buy / worth seeing
- 5-6: fair or uncertain
- 1-4: overpriced, weak, or not interesting

Important:
- Focus primarily on whether the watch appears under market.
- Use the title/body/reference/model/condition clues.
- If uncertain, make your best estimate.
- Keep reason short.
- listing_price should reflect the seller's asking price if present.
- estimated_market_price should be your best estimate of typical market value.
- under_market_amount should be like "$300", "$300-$500", or "unknown".
- Be conservative if details are sparse.

Listing:
Title: {title}
Published: {published}
Price: {price or "unknown"}
URL: {url}
Body: {summary}
""".strip()

    try:
        response = anthropic_client.messages.create(
            model=MINIMAX_MODEL,
            max_tokens=1024,
            temperature=0.0,
            system="You are a watch valuation expert. Respond with plain text JSON only, no thinking.",
            messages=[{"role": "user", "content": prompt}],
            extra_headers={"anthropic-beta": "output-1024-2025-02-19"},
            timeout=30.0,
        )

        # Extract text from response
        raw = None
        for block in response.content:
            if hasattr(block, 'text'):
                raw = block.text.strip()
                break

        if not raw:
            raise ValueError("No text content in API response")

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise ValueError(f"Claude returned non-JSON output: {raw[:300]}")

    except Exception as e:
        return {
            "score": 5,
            "should_alert": True,
            "reason": f"Claude scoring unavailable: {str(e)[:180]}",
            "brand": "unknown",
            "model_guess": "unknown",
            "listing_price": price or "unknown",
            "estimated_market_price": "unknown",
            "is_likely_under_market": False,
            "under_market_amount": "unknown",
        }


def ensure_csv_dir_exists() -> None:
    CSV_DIR.mkdir(parents=True, exist_ok=True)


def get_daily_csv_file(target_date: str | None = None) -> Path:
    ensure_csv_dir_exists()
    if target_date is None:
        target_date = datetime.now().strftime("%Y-%m-%d")
    return CSV_DIR / f"under_market_{target_date}.csv"


def ensure_daily_csv_exists(target_date: str | None = None) -> Path:
    csv_file = get_daily_csv_file(target_date)
    if csv_file.exists():
        return csv_file

    with csv_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp_saved_local",
            "subreddit",
            "title",
            "url",
            "published",
            "listing_price",
            "estimated_market_price",
            "is_likely_under_market",
            "under_market_amount",
            "score",
            "brand",
            "model_guess",
            "reason",
            "match_reason",
            "notes",
        ])

    return csv_file


def append_to_daily_csv(
    title: str,
    url: str,
    published: str,
    summary: str,
    claude_result: dict,
    match_reason: str,
) -> None:
    csv_file = ensure_daily_csv_exists()

    with csv_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().isoformat(),
            SUBREDDIT,
            title,
            url,
            published,
            claude_result.get("listing_price", "unknown"),
            claude_result.get("estimated_market_price", "unknown"),
            claude_result.get("is_likely_under_market", False),
            claude_result.get("under_market_amount", "unknown"),
            claude_result.get("score", "unknown"),
            claude_result.get("brand", "unknown"),
            claude_result.get("model_guess", "unknown"),
            claude_result.get("reason", ""),
            match_reason,
            clean_summary(summary),
        ])


def send_discord_alert(
    title: str,
    url: str,
    summary: str,
    published: str,
    price: str | None,
    claude_result: dict | None = None,
    match_reason: str | None = None,
    webhook_url: str | None = None,
) -> None:
    # Use provided webhook_url, or fall back to TEST webhook, then ALERT webhook
    if webhook_url is None:
        if TEST_DISCORD_WEBHOOK_URL:
            webhook_url = TEST_DISCORD_WEBHOOK_URL
        else:
            webhook_url = ALERT_DISCORD_WEBHOOK_URL

    if not webhook_url:
        print("No Discord webhook URL configured; skipping alert")
        return

    summary = clean_summary(summary)
    if len(summary) > 220:
        summary = summary[:217] + "..."

    listing_price = price or "unknown"
    est_market = "unknown"
    under_market = "unknown"
    under_amount = "unknown"
    score = "n/a"
    brand = "unknown"
    model = "unknown"
    reason = "No reason provided"

    if claude_result:
        listing_price = claude_result.get("listing_price") or listing_price
        est_market = claude_result.get("estimated_market_price", "unknown")
        under_market = "YES" if claude_result.get("is_likely_under_market", False) else "NO"
        under_amount = claude_result.get("under_market_amount", "unknown")
        score = claude_result.get("score", "n/a")
        brand = claude_result.get("brand", "unknown")
        model = claude_result.get("model_guess", "unknown")
        reason = claude_result.get("reason", reason)

    lines = [
        f"**New r/{SUBREDDIT} match**",
        f"**{title[:180]}**",
        url,
        f"LISTING_PRICE: {listing_price}",
        f"EST_MARKET: {est_market}",
        f"UNDER_MARKET: {under_market}",
        f"EDGE: {under_amount}",
        f"SCORE: {score}",
        f"BRAND: {brand}",
        f"MODEL: {model}",
        f"WHY: {reason}",
    ]

    if match_reason:
        lines.append(f"MATCH_REASON: {match_reason}")

    if published:
        lines.append(f"POSTED: {published}")

    if summary:
        lines.append(f"NOTES: {summary}")

    content = "\n".join(lines)
    if len(content) > 1800:
        content = content[:1797] + "..."

    response = requests.post(
        webhook_url,
        json={"content": content},
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()


def upload_daily_csv_to_discord(target_date: str) -> None:
    if not CSV_DISCORD_WEBHOOK_URL:
        print("CSV_DISCORD_WEBHOOK_URL missing; skipping CSV upload")
        return

    csv_file = get_daily_csv_file(target_date)

    if not csv_file.exists():
        print(f"No CSV for {target_date}; skipping upload")
        return

    payload = {
        "content": f"Daily elite under-market CSV snapshot for r/{SUBREDDIT} — {target_date}"
    }

    with csv_file.open("rb") as f:
        response = requests.post(
            CSV_DISCORD_WEBHOOK_URL,
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (csv_file.name, f, "text/csv")},
            timeout=60,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()

    print(f"[CSV UPLOADED] {csv_file.name}")


def maybe_upload_daily_csv() -> None:
    now = datetime.now()
    state = load_csv_upload_state()

    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    already_uploaded_today = state.get("last_uploaded_for_day") == today
    at_or_after_upload_time = (now.hour, now.minute) >= (CSV_UPLOAD_HOUR, CSV_UPLOAD_MINUTE)

    if not already_uploaded_today and at_or_after_upload_time:
        upload_daily_csv_to_discord(yesterday)
        save_csv_upload_state({
            "last_uploaded_for_day": today,
            "last_uploaded_csv_date": yesterday,
        })


def fetch_feed():
    response = requests.get(
        RSS_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    response.raise_for_status()
    return feedparser.parse(response.content)


def fetch_op_comment_price(url: str) -> tuple[str | None, str]:
    """
    Fetch Reddit post and look for price in comments from the original poster (OP).
    Returns (price_string, full_content_for_scoring).
    """
    try:
        json_url = url + ".json"
        response = requests.get(
            json_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        if not data or len(data) < 2:
            return None, ""

        # Get post data to find the OP username
        post_data = data[0]["data"]["children"][0]["data"]
        op_author = post_data.get("author", "")
        if not op_author:
            return None, ""

        # Get post selftext for scoring
        selftext = post_data.get("selftext", "")
        title = post_data.get("title", "")

        # Now get comments and filter to only OP comments
        comments_data = data[1]["data"]["children"]
        op_price = None
        op_comments = []

        for comment in comments_data[:15]:  # Top 15 comments
            if comment["kind"] == "t1":
                comment_data = comment["data"]
                # Only look at comments from the OP
                if comment_data.get("author") == op_author:
                    body = comment_data.get("body", "")
                    if body:
                        op_comments.append(body)
                        # Extract price from this OP comment
                        price = extract_price(body)
                        if price and op_price is None:
                            op_price = price

        # Combine content for scoring (selftext + all OP comments)
        full_content = f"{title} {selftext} {' '.join(op_comments)}"

        return op_price, full_content

    except Exception as e:
        print(f"[WARN] Failed to fetch OP comment: {e}")
        return None, ""


def main():
    print(f"Watching r/{SUBREDDIT} via RSS: {RSS_URL}")
    print(f"Keywords: {KEYWORDS if KEYWORDS else '[all posts]'}")
    print("Special rules: Seiko under $1000, or any watch mentioning 1993")
    print(f"Daily CSV upload time: {CSV_UPLOAD_HOUR:02d}:{CSV_UPLOAD_MINUTE:02d}")
    print(f"Daily CSV folder: {CSV_DIR.resolve()}")
    print("Upload mode: upload YESTERDAY'S completed CSV once per day")

    seen_ids = load_seen_ids()
    ensure_daily_csv_exists()

    while True:
        try:
            maybe_upload_daily_csv()

            feed = fetch_feed()
            entries = feed.entries[:25]

            for entry in reversed(entries):
                post_id = entry.get("id") or entry.get("link")
                if not post_id or post_id in seen_ids:
                    continue

                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", "").strip()
                published = entry.get("published", "").strip()

                age_min = parse_post_age_minutes(entry)
                if age_min is not None and age_min > MAX_POST_AGE_MINUTES:
                    seen_ids.add(post_id)
                    continue

                # Check for price first (required for any Discord notification)
                price = extract_price(f"{title} {summary}")
                full_content = summary

                # If no price in title/summary, fetch post and look for price in OP's comment
                if not price:
                    print(f"[INFO] No price in RSS, checking for OP comment...")
                    op_price, full_content = fetch_op_comment_price(link)
                    if op_price:
                        price = op_price
                        print(f"[INFO] Found price in OP comment: {price}")
                    else:
                        # No price from OP yet - skip but DON'T add to seen_ids
                        # so we check again on next scan (2 min later)
                        print(f"[SKIP] No price from OP yet: {title[:50]}...")
                        seen_ids.add(post_id)
                        continue

                # Score the listing with MiniMax
                claude_result = score_with_claude(
                    title=title,
                    summary=clean_summary(full_content),
                    url=link,
                    published=published,
                    price=price,
                )
                print(f"[CLAUDE] {claude_result}")

                score = int(claude_result.get("score", 5))
                is_under_market = bool(claude_result.get("is_likely_under_market", False))
                match_reason = get_match_reason(title, summary)
                is_keyword_match = matches_keywords(title, summary)

                print(f"[MATCH] {title}")
                print(f"[MATCH_REASON] {match_reason}")

                # Send to only ONE channel (priority order)
                if is_keyword_match and is_under_market and score >= 8 and ALERT_DISCORD_WEBHOOK_URL:
                    # Priority 1: Best keyword deals (under market + score >= 8) → ALERT
                    send_discord_alert(
                        title=title,
                        url=link,
                        summary=summary,
                        published=published,
                        price=price,
                        claude_result=claude_result,
                        match_reason=match_reason,
                        webhook_url=ALERT_DISCORD_WEBHOOK_URL,
                    )
                    print(f"[ALERT DISCORD SENT] score={score}, under_market={is_under_market}")
                elif is_keyword_match and TEST_DISCORD_WEBHOOK_URL:
                    # Priority 2: Keyword matches (that didn't go to ALERT) → TEST
                    send_discord_alert(
                        title=title,
                        url=link,
                        summary=summary,
                        published=published,
                        price=price,
                        claude_result=claude_result,
                        match_reason=match_reason,
                        webhook_url=TEST_DISCORD_WEBHOOK_URL,
                    )
                    print(f"[TEST DISCORD SENT] score={score}, under_market={is_under_market}")
                elif ALL_DISCORD_WEBHOOK_URL:
                    # Priority 3: All other posts with price → ALL
                    send_discord_alert(
                        title=title,
                        url=link,
                        summary=summary,
                        published=published,
                        price=price,
                        claude_result=claude_result,
                        match_reason=match_reason,
                        webhook_url=ALL_DISCORD_WEBHOOK_URL,
                    )
                    print(f"[ALL DISCORD SENT] score={score}, under_market={is_under_market}")

                # Rule 4: If keyword match AND score > 8, append to CSV
                if is_keyword_match and score > 8:
                    append_to_daily_csv(
                        title=title,
                        url=link,
                        published=published,
                        summary=summary,
                        claude_result=claude_result,
                        match_reason=match_reason,
                    )
                    print(f"[CSV SAVED] Elite steal logged (score={score})")
                else:
                    print(f"[CSV SKIP] score={score}")

                seen_ids.add(post_id)

            save_seen_ids(seen_ids)

        except KeyboardInterrupt:
            print("Stopped by user.")
            break
        except Exception as e:
            print(f"Error: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
