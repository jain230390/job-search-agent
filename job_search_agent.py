#!/usr/bin/env python3
"""
Job Search Agent
-----------------
For each company in companies.json:
  1. Uses Claude (with its native web_search tool) to find current open
     roles matching the target level/function for that company, INCLUDING an
     estimate of how many days ago each was posted. Roles posted more than
     POSTING_MAX_AGE_DAYS ago are dropped - not worth applying to.
  2. Skips roles already scored in a previous run (seen_roles.json cache) -
     reuses the cached score instead of re-paying for a fresh one. Only
     genuinely new roles get fetched + scored.
  3. Fetches the FULL job description text for new roles, then scores ALL new
     roles for that company in a single batched call against resume.txt
     (0-100 fit score + reasoning) - same as pasting a JD directly and asking
     for a fitment percentage. If a JD can't be fetched (JS-rendered page,
     blocked, etc.), that role is scored on title/level/location only and
     flagged "title_only".
  4. Writes a ranked CSV (cached + newly-scored roles together) + prints a
     summary table.

Cost controls:
  - Scoring uses Claude Haiku (cheap); search uses Sonnet (needs to be good
    at web search).
  - Resume sent via a cached system block - not re-billed at full price
    across calls.
  - All of a company's NEW roles are scored in ONE batched call.
  - MAX_ROLES_PER_COMPANY caps a single company from blowing up cost.
  - seen_roles.json: a role already scored is never re-scored on a later
    run - only its last_seen date is refreshed. This is what makes daily/
    weekly automated runs cheap. Entries not seen again for
    CACHE_STALE_AFTER_DAYS are dropped (the role's presumably closed).

Setup:
  pip install anthropic requests beautifulsoup4
  export ANTHROPIC_API_KEY=sk-ant-...

Run manually:
  python job_search_agent.py

Run automatically: see README.md for the cron setup using run_job_search.sh.

Edit companies.json to change the target company list / level per company.
Edit resume.txt if your resume changes.
Edit LEVEL_TIERS / BASE_PROFILE_TEMPLATE below to change what roles it looks for.
"""

import json
import os
import re
import sys
import csv
from datetime import datetime, timedelta

import anthropic
import requests
from bs4 import BeautifulSoup

SEARCH_MODEL = "claude-sonnet-5"
SCORE_MODEL = "claude-haiku-4-5-20251001"

MAX_ROLES_PER_COMPANY = 15
JD_MAX_CHARS = 3000
POSTING_MAX_AGE_DAYS = 14      # drop roles posted longer ago than this
CACHE_STALE_AFTER_DAYS = 21    # drop a cached role if not re-seen for this long (probably closed)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "seen_roles.json")

LEVEL_TIERS = {
    "manager_plus": (
        "Manager, Senior Manager, Associate Director, Director, Senior Director, GM, "
        "Head of, VP, or COO-level positions"
    ),
    "default": (
        "Associate Director, Director, Senior Director, GM, Head of, VP, or COO-level positions"
    ),
    "director_plus": (
        "Director, Senior Director, GM, Head of, VP, or COO-level positions "
        "(explicitly EXCLUDE Associate Director and anything below it - the candidate is "
        "already at that level here, so only strictly more senior roles are relevant)"
    ),
}

BASE_PROFILE_TEMPLATE = (
    "Senior leadership roles suited to a 13+ year commerce/ops operator, specifically: "
    "{levels}, "
    "in Strategy & Operations, Business Operations, GTM, Category Management, "
    "Product Operations, Supply Chain/Logistics, or Creator/Content Commerce. "
    "Location: India (Bengaluru preferred, but include remote/hybrid India roles)."
)


def target_profile_for(level_key):
    levels = LEVEL_TIERS.get(level_key, LEVEL_TIERS["default"])
    return BASE_PROFILE_TEMPLATE.format(levels=levels)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def load_text(path):
    with open(path, "r") as f:
        return f.read()


def load_companies(path):
    """Supports both a flat list of name strings and a list of
    {"name": ..., "level": ...} objects. Returns (name, level_key) tuples."""
    raw = load_json(path, [])
    companies = []
    for entry in raw:
        if isinstance(entry, str):
            companies.append((entry, "default"))
        elif isinstance(entry, dict) and "name" in entry:
            companies.append((entry["name"], entry.get("level", "default")))
    return companies


def load_cache():
    return load_json(CACHE_PATH, {})


def save_cache(cache):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def prune_cache(cache):
    """Drop cache entries not re-confirmed as still open for a long while."""
    cutoff = datetime.now() - timedelta(days=CACHE_STALE_AFTER_DAYS)
    kept = {}
    for url, entry in cache.items():
        try:
            last_seen = datetime.strptime(entry["last_seen"], "%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        if last_seen >= cutoff:
            kept[url] = entry
    return kept


def resume_system_block(resume_text):
    return [{
        "type": "text",
        "text": f"Candidate resume:\n---\n{resume_text}\n---",
        "cache_control": {"type": "ephemeral"},
    }]


def find_roles_for_company(client, company, level_key):
    """Web-search the company's current openings, including a posted-days-ago estimate."""
    target_profile = target_profile_for(level_key)
    prompt = f"""Search the web for CURRENT open job postings at "{company}" that match this profile:

{target_profile}

Only include roles that are actually open right now on {company}'s official careers site or
a listing you can verify is current (prefer official careers.{company.lower()}.com pages,
Greenhouse/Lever/Workday boards, or LinkedIn Jobs listings from the company itself).

For each role, also estimate how many days ago it was posted, based on any "posted X days/weeks
ago" label or listed posting date on the page. If you genuinely cannot determine this, use null.

Return ONLY a JSON array (no prose, no markdown fences) of objects with this exact shape:
[{{"title": "...", "url": "...", "location": "...", "level": "...", "posted_days_ago": <int or null>}}]

If you find no matching roles, return an empty array: []
"""
    resp = client.messages.create(
        model=SEARCH_MODEL,
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return extract_json_array(text)


def extract_json_array(text):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return []


def extract_json_object_or_array(text):
    arr = re.search(r"\[.*\]", text, re.DOTALL)
    if arr:
        try:
            return json.loads(arr.group(0))
        except json.JSONDecodeError:
            pass
    obj = re.search(r"\{.*\}", text, re.DOTALL)
    if obj:
        try:
            return json.loads(obj.group(0))
        except json.JSONDecodeError:
            pass
    return None


def fetch_jd_text(url, max_chars=JD_MAX_CHARS):
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; job-search-agent/1.0)"},
        )
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = re.sub(r"\n\s*\n+", "\n\n", soup.get_text(separator="\n").strip())
        if len(text) < 200:
            return None
        return text[:max_chars]
    except requests.RequestException:
        return None


def score_roles_batch(client, company, roles, resume_system):
    """Score a batch of (new, not-yet-cached) roles for one company in a single call."""
    if not roles:
        return []

    job_blocks = []
    used_flags = []
    for i, role in enumerate(roles):
        jd_text = fetch_jd_text(role.get("url", ""))
        used_full_jd = jd_text is not None
        used_flags.append(used_full_jd)
        if used_full_jd:
            job_blocks.append(
                f"[{i}] \"{role.get('title','')}\" at {company} ({role.get('location','')}, {role.get('level','')})\n"
                f"Full JD:\n{jd_text}\n"
            )
        else:
            job_blocks.append(
                f"[{i}] \"{role.get('title','')}\" at {company} ({role.get('location','')}, {role.get('level','')})\n"
                f"(JD text unavailable - score on title/level/location only.)\n"
            )

    prompt = f"""Below are {len(roles)} open roles at {company}, each labeled with an index [i].
For EACH role, score how strong a fit the candidate (see resume in system context) is,
from 0-100, the same way you would if their JD had been pasted directly and you were
asked for a fitment percentage. Weigh actual responsibilities/requirements against the
resume's experience, seniority, and skills - not just title-matching.

{chr(10).join(job_blocks)}

Respond ONLY with a JSON array (no prose, no markdown fences), one object per role in the
SAME index order, this exact shape:
[{{"index": 0, "score": <0-100 int>, "reasoning": "<one or two sentences>"}}, ...]
"""
    resp = client.messages.create(
        model=SCORE_MODEL,
        max_tokens=250 * len(roles) + 200,
        system=resume_system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    parsed = extract_json_object_or_array(text)

    results = [{"score": 0, "reasoning": "Could not parse score", "used_full_jd": used_flags[i]}
               for i in range(len(roles))]
    if isinstance(parsed, list):
        for item in parsed:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(roles):
                results[idx] = {
                    "score": item.get("score", 0),
                    "reasoning": item.get("reasoning", ""),
                    "used_full_jd": used_flags[idx],
                }
    return results


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY in your environment first.")

    companies = load_companies(os.path.join(HERE, "companies.json"))
    resume_text = load_text(os.path.join(HERE, "resume.txt"))
    resume_system = resume_system_block(resume_text)

    cache = prune_cache(load_cache())
    client = anthropic.Anthropic(api_key=api_key)
    today = datetime.now().strftime("%Y-%m-%d")

    all_rows = []
    for company, level_key in companies:
        print(f"Searching {company} (level: {level_key})...", file=sys.stderr)
        try:
            found = find_roles_for_company(client, company, level_key)
        except Exception as e:
            print(f"  ! search failed for {company}: {e}", file=sys.stderr)
            continue

        if not found:
            print("  no matching roles found (search returned empty)", file=sys.stderr)
            continue

        # Drop roles posted longer ago than POSTING_MAX_AGE_DAYS. Keep roles with
        # unknown age (posted_days_ago is null) - benefit of the doubt.
        fresh = []
        dropped_stale = 0
        for role in found:
            age = role.get("posted_days_ago")
            if isinstance(age, (int, float)) and age > POSTING_MAX_AGE_DAYS:
                dropped_stale += 1
                continue
            fresh.append(role)
        if dropped_stale:
            print(f"  dropped {dropped_stale} role(s) posted > {POSTING_MAX_AGE_DAYS} days ago", file=sys.stderr)

        if len(fresh) > MAX_ROLES_PER_COMPANY:
            print(f"  {len(fresh)} roles after filtering, capping to top {MAX_ROLES_PER_COMPANY}", file=sys.stderr)
            fresh = fresh[:MAX_ROLES_PER_COMPANY]

        # Split into already-cached (reuse score) vs genuinely new (needs scoring)
        to_score = []
        for role in fresh:
            url = role.get("url", "")
            cached = cache.get(url) if url else None
            if cached:
                cached["last_seen"] = today
                all_rows.append({
                    "score": cached["score"],
                    "jd_source": cached["jd_source"],
                    "company": company,
                    "title": role.get("title", ""),
                    "level": role.get("level", ""),
                    "location": role.get("location", ""),
                    "posted_days_ago": role.get("posted_days_ago", ""),
                    "first_seen": cached["first_seen"],
                    "status": "cached",
                    "reasoning": cached["reasoning"],
                    "url": url,
                })
                print(f"  {role.get('title','?')} -> {cached['score']}  [cached, first seen {cached['first_seen']}]",
                      file=sys.stderr)
            else:
                to_score.append(role)

        if to_score:
            print(f"  scoring {len(to_score)} new role(s) in one batch call...", file=sys.stderr)
            try:
                scored = score_roles_batch(client, company, to_score, resume_system)
            except Exception as e:
                print(f"  ! scoring failed for {company}: {e}", file=sys.stderr)
                scored = [{"score": 0, "reasoning": f"scoring failed: {e}", "used_full_jd": False} for _ in to_score]

            for role, s in zip(to_score, scored):
                url = role.get("url", "")
                jd_source = "full_jd" if s["used_full_jd"] else "title_only"
                if url:
                    cache[url] = {
                        "score": s["score"],
                        "reasoning": s["reasoning"],
                        "jd_source": jd_source,
                        "first_seen": today,
                        "last_seen": today,
                    }
                all_rows.append({
                    "score": s["score"],
                    "jd_source": jd_source,
                    "company": company,
                    "title": role.get("title", ""),
                    "level": role.get("level", ""),
                    "location": role.get("location", ""),
                    "posted_days_ago": role.get("posted_days_ago", ""),
                    "first_seen": today,
                    "status": "new",
                    "reasoning": s["reasoning"],
                    "url": url,
                })
                tag = "" if s["used_full_jd"] else "  [title-only, JD unreadable]"
                print(f"  {role.get('title','?')} -> {s['score']}{tag}  [new]", file=sys.stderr)

    save_cache(cache)

    all_rows.sort(key=lambda r: r["score"], reverse=True)

    out_path = os.path.join(HERE, f"matches_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
    fieldnames = ["score", "status", "jd_source", "company", "title", "level", "location",
                  "posted_days_ago", "first_seen", "reasoning", "url"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

    new_count = sum(1 for r in all_rows if r["status"] == "new")
    cached_count = len(all_rows) - new_count
    print(f"\nSaved {len(all_rows)} roles to {out_path}  ({new_count} newly scored, {cached_count} reused from cache)\n")
    print(f"{'SCORE':<6}{'COMPANY':<10}{'TITLE'}")
    for row in all_rows[:20]:
        print(f"{row['score']:<6}{row['company']:<10}{row['title']}")


if __name__ == "__main__":
    main()
