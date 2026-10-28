#!/usr/bin/env python3
"""Customer Reference Finder.

Finds customers who already posted about your product, checks their company's
industry and size, ranks them against the prospect that needs a reference, and
drafts the ask for your customer success manager.
Data layer: Crustdata POST /social_post/search and POST /company/enrich (https://docs.crustdata.com).

  search_posts()      -> one documented call: public posts that mention the product
  qualify()           -> deterministic rules that re-check every post (unit tested)
  enrich_companies()  -> one documented call: industry and size per company
  score()             -> rank against the prospect's industry and size
  summary()           -> aggregate numbers (safe to show in a README)
  slack_text          -> the alert; send_slack() respects --dry-run
  only_new / save_state -> alert each post once
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from crustdata_api import Crustdata, day, load_env
from notify import ask_claude, claude_available, send_slack

POST_FIELDS = [
    "share_url", "date_posted", "post_type", "text",
    "actor.actor_type", "actor.name", "actor.title", "actor.company_name",
    "engagement.total_reactions",
]
COMPANY_FIELDS = ["basic_info", "taxonomy.professional_network_industry"]

# Happy-customer cues and complaint cues, matched on whole words in the post text.
POSITIVE = re.compile(
    r"\b(love|loved|loving|great|recommend|recommended|saved us|saves us|helped us|helps us|"
    r"favou?rite|game.?changer|can't live without|cannot live without|switched to|"
    r"works well|worth it|huge win|big win)\b", re.I)
NEGATIVE = re.compile(
    r"\b(hate|hated|terrible|worst|outage|broken|awful|frustrat\w*|disappointed|"
    r"cancel(?:led|ed)?|leaving|stopped using|too expensive|overpriced|bug|bugs)\b", re.I)
# Roles that can say yes to a reference call.
SENIOR = re.compile(
    r"\b(chief|ceo|cto|coo|cfo|cio|cmo|cro|founder|co-?founder|owner|president|"
    r"vp|vice president|head|director|general manager|partner|principal|manager|lead)\b", re.I)
TOP = re.compile(r"\b(chief|ceo|cto|coo|cfo|cio|cmo|cro|founder|co-?founder|owner|president|vp|vice president|head)\b", re.I)

SIZE_BANDS = ["1", "2-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5001-10000", "10001+"]


def norm(text: Optional[str]) -> str:
    """'  TeamOut (YC W22) ' -> 'teamout (yc w22)'."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def build_filters(product: str, since: str) -> Dict[str, Any]:
    # `[.]` guarantees the literal word is in the text; post_type=original drops reposts,
    # which otherwise dominate the results and are not the author's own words.
    return {"op": "and", "conditions": [
        {"field": "text", "type": "[.]", "value": product},
        {"field": "actor.actor_type", "type": "=", "value": "person"},
        {"field": "post_type", "type": "=", "value": "original"},
        {"field": "date_posted", "type": "=>", "value": since},
    ]}


def qualify(post: Dict[str, Any], product: str, own_company: str, since: str) -> Optional[Dict[str, Any]]:
    """Return a flat record, or None. Every rule is re-checked here, whatever the filters did."""
    actor = post.get("actor") or {}
    text = post.get("text") or ""
    title = (actor.get("title") or "").strip()
    company = (actor.get("company_name") or "").strip()
    if post.get("post_type") != "original":
        return None
    if actor.get("actor_type", "person") != "person":
        return None
    if not (post.get("share_url") and title and company):
        return None
    if day(post.get("date_posted")) < since:
        return None
    if own_company and norm(own_company) in norm(company):
        return None  # your own employees are not references
    if not re.search(r"\b" + re.escape(product) + r"\b", text, re.I):
        return None
    if NEGATIVE.search(text) or not POSITIVE.search(text):
        return None
    if not SENIOR.search(title):
        return None
    excerpt = re.sub(r"\s+", " ", text).strip()
    return {
        "id": post["share_url"],
        "url": post["share_url"],
        "posted": day(post.get("date_posted")),
        "title": title,
        "company": company,
        "reactions": (post.get("engagement") or {}).get("total_reactions") or 0,
        "excerpt": excerpt[:240],
        "industry": "",
        "size": "",
        "score": 0,
    }


def enrich_companies(client: Crustdata, names: List[str]) -> Dict[str, Dict[str, str]]:
    """Map normalised company name -> {industry, size, name}. Exact-name matches only.

    Names can fuzzy-match several companies, so a match is kept only when its
    name equals the name on the post; anything else is left unenriched.
    """
    found: Dict[str, Dict[str, str]] = {}
    for i in range(0, len(names), 10):
        for entry in client.company_enrich(names[i:i + 10], COMPANY_FIELDS):
            wanted = norm(entry.get("matched_on"))
            for match in entry.get("matches") or []:
                data = match.get("company_data") or {}
                info = data.get("basic_info") or {}
                if norm(info.get("name")) == wanted and wanted not in found:
                    found[wanted] = {
                        "name": info.get("name") or "",
                        "size": info.get("employee_count_range") or "",
                        "industry": (data.get("taxonomy") or {}).get("professional_network_industry") or "",
                    }
    return found


def size_distance(a: str, b: str) -> Optional[int]:
    if a in SIZE_BANDS and b in SIZE_BANDS:
        return abs(SIZE_BANDS.index(a) - SIZE_BANDS.index(b))
    return None


def score(rec: Dict[str, Any], prospect_industry: str, prospect_size: str) -> int:
    pts = 0
    if prospect_industry and norm(rec.get("industry")) == norm(prospect_industry):
        pts += 3
    dist = size_distance(rec.get("size", ""), prospect_size)
    if dist == 0:
        pts += 2
    elif dist == 1:
        pts += 1
    if TOP.search(rec.get("title", "")):
        pts += 1
    if rec.get("reactions", 0) >= 10:
        pts += 1
    return pts


def run(client: Crustdata, product: str, own_company: str, since: str, max_posts: int,
        prospect_industry: str = "", prospect_size: str = "", max_companies: int = 10) -> Dict[str, Any]:
    scanned = 0
    rows: List[Dict[str, Any]] = []
    seen = set()
    posts = client.social_post_search(build_filters(product, since), POST_FIELDS, max_results=max_posts)
    for post in posts:
        scanned += 1
        rec = qualify(post, product, own_company, since)
        if rec and rec["id"] not in seen:
            seen.add(rec["id"])
            rows.append(rec)
    # Enrich the most visible authors' companies first; the cap keeps enrichment spend bounded.
    rows.sort(key=lambda r: -r["reactions"])
    names: List[str] = []
    for r in rows:
        if r["company"] not in names:
            names.append(r["company"])
    info = enrich_companies(client, names[:max_companies]) if names else {}
    for r in rows:
        hit = info.get(norm(r["company"]))
        if hit:
            r["industry"], r["size"] = hit["industry"], hit["size"]
        r["score"] = score(r, prospect_industry, prospect_size)
    rows.sort(key=lambda r: (-r["score"], -r["reactions"]))
    return {"scanned": scanned, "rows": rows}


def merge_small(counter: Counter, min_count: int = 2) -> Dict[str, int]:
    """Buckets smaller than `min_count` are merged into 'other' so no single record shows."""
    out: Dict[str, int] = {}
    other = 0
    for key, n in counter.most_common():
        if n >= min_count and key:
            out[key] = n
        else:
            other += n
    if other:
        out["other"] = other
    return out


def summary(scanned: int, rows: List[Dict[str, Any]], prospect_industry: str = "",
            prospect_size: str = "") -> Dict[str, Any]:
    enriched = [r for r in rows if r["industry"] or r["size"]]
    return {
        "posts_scanned": scanned,
        "qualified_references": len(rows),
        "enriched_with_company": len(enriched),
        "same_industry_as_prospect": sum(1 for r in enriched if prospect_industry and norm(r["industry"]) == norm(prospect_industry)),
        "within_one_size_band": sum(1 for r in enriched if size_distance(r["size"], prospect_size) in (0, 1)),
        "by_size": merge_small(Counter(r["size"] for r in enriched)),
    }


def draft_ask(rec: Dict[str, Any], product: str, prospect: str, use_claude: bool = False) -> str:
    """The note the CSM sends. Claude writes it when asked and configured; otherwise a template."""
    if use_claude and claude_available():
        prompt = (
            f"Write a warm, three-sentence note a customer success manager sends to a customer who "
            f"publicly posted about {product}. Ask if they would take a 20 minute reference call with "
            f"{prospect}. Their post: \"{rec['excerpt']}\". Their role: {rec['title']} at {rec['company']}. "
            "Plain text, no flattery, no placeholders other than [CSM name]."
        )
        text = ask_claude(prompt, max_tokens=500)
        if text:
            return text
    return (f"Hi, I saw your post about {product} and it made my day. "
            f"We are talking with {prospect} and a short call with someone in your seat at {rec['company']} "
            f"would help a lot. Would you be open to a 20 minute reference call this month? [CSM name]")


def slack_text(rows: List[Dict[str, Any]], product: str, prospect: str, limit: int = 5,
               use_claude: bool = False) -> str:
    lines = [f":speech_balloon: {len(rows)} customers posted about {product}. Best matches for {prospect}:"]
    for r in rows[:limit]:
        meta = " · ".join(x for x in (r["industry"], r["size"]) if x) or "company not matched"
        lines.append(f"• {r['title']}, {r['company']} ({meta}) · match {r['score']} · {r['url']}")
        lines.append(f"  > {r['excerpt'][:140]}")
    if rows:
        lines.append("")
        lines.append("Draft ask for the top match:")
        lines.append(draft_ask(rows[0], product, prospect, use_claude))
    return "\n".join(lines)


def _load_state(state_path):
    if not os.path.exists(state_path):
        return set()
    with open(state_path, encoding="utf-8") as fh:
        return set(json.load(fh))


def only_new(rows, state_path):
    seen = _load_state(state_path)
    return [r for r in rows if str(r["id"]) not in seen]


def save_state(rows, state_path):
    seen = _load_state(state_path)
    seen.update(str(r["id"]) for r in rows)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(seen), fh, indent=1)


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    env = os.environ.get
    p = argparse.ArgumentParser(description="Find customer references from public posts (Crustdata data layer)")
    p.add_argument("--product", default=env("PRODUCT", ""), help="product name to search for in posts")
    p.add_argument("--own-company", default=env("OWN_COMPANY", ""), help="skip authors who work here")
    p.add_argument("--prospect-industry", default=env("PROSPECT_INDUSTRY", ""))
    p.add_argument("--prospect-size", default=env("PROSPECT_SIZE", ""), help="e.g. 201-500")
    p.add_argument("--prospect", default="the prospect", help="how to name the prospect in the ask")
    p.add_argument("--since-days", type=int, default=int(env("SINCE_DAYS", "30")))
    p.add_argument("--max-posts", type=int, default=50)
    p.add_argument("--max-companies", type=int, default=10)
    p.add_argument("--claude", action="store_true", help="let Claude draft the ask (needs ANTHROPIC_API_KEY)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all", action="store_true", help="include posts already alerted")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)
    if not args.product:
        p.error("set --product or PRODUCT")
    since = (dt.date.today() - dt.timedelta(days=args.since_days)).isoformat()
    result = run(Crustdata(), args.product, args.own_company, since, args.max_posts,
                 args.prospect_industry, args.prospect_size, args.max_companies)
    rows = result["rows"]
    if not args.all:
        rows = only_new(rows, args.state)
    stats = summary(result["scanned"], rows, args.prospect_industry, args.prospect_size)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "references.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": stats, "rows": rows}, fh, indent=1)
    print(json.dumps(stats, indent=1))
    if rows:
        print(send_slack(slack_text(rows, args.product, args.prospect, use_claude=args.claude), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(rows, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
