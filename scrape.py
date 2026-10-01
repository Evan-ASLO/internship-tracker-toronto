"""
v3 - REAL DATA ONLY.

Rules:
  1. Every posting comes from a live ATS API response. Nothing is invented.
  2. Posted dates come from the API, never estimated.
  3. Every URL is re-fetched and must return HTTP 200 before it ships.
     Closed reqs (404/410) are dropped.
  4. Scope narrowed to 4 business tracks: Marketing, Business Operations,
     Finance, Consulting.
"""

import json, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import requests

TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# ---------------- categories (extensible: add a key + its regex) -------------
# Word-boundary patterns, not substrings. Substring matching produced false
# positives like "Senior Engineering Manager V&V Media" -> Marketing.
CATEGORIES = {
    "Finance": r"\b(financ\w*|account(ing|ant)|audit\w*|tax|actuar\w*|investment"
               r"|investing|banking|treasury|capital\s+markets|fp&a|controller"
               r"|equity\s+research|asset\s+management|wealth|portfolio"
               r"|valuation|credit\s+risk|underwrit\w*)\b",
    "Marketing": r"\b(marketing|brand\s+(management|strategy|marketing)?|social\s+media"
                 r"|content\s+(marketing|strategy)|communications|growth\s+marketing"
                 r"|seo|sem|campaign\w*|public\s+relations|advertising|crm"
                 r"|media\s+planning|lifecycle\s+marketing)\b",
    "Business Operations": r"\b(business\s+operations|bizops|operations"
                           r"|supply\s+chain|logistics|procurement|program\s+manage\w*"
                           r"|project\s+manage\w*|business\s+analyst|process\s+improvement"
                           r"|merchandis\w*|category\s+management)\b",
    "Consulting": r"\b(consult\w*|advisory|strategy|strategic\s+plan\w*"
                  r"|transformation)\b",
}
CATEGORIES = {k: re.compile(v, re.I) for k, v in CATEGORIES.items()}

# Technical roles must never be classified into a business track just because a
# keyword like "brand" or "media" appears in the title.
EXCLUDE_RE = re.compile(
    r"\b(engineer\w*|developer|software|swe|backend|front[\s-]?end|fullstack|full[\s-]?stack"
    r"|scientist|machine\s+learning|ml|data\s+engineer|devops|sre|infrastructure"
    r"|designer|ux|ui|security\s+engineer|qa|test|firmware|hardware|silicon)\b", re.I)

INTERN_RE = re.compile(
    r"\b(intern|interns|internship|internships|co-?op|co-?ops|new\s?grad|"
    r"new\s?graduate|students?)\b", re.I)

# "Campus Talent Acquisition Senior Associate" matched on 'campus' - it is a
# recruiter job, not an internship. Reject seniority markers unless the title
# also explicitly says intern/co-op.
SENIOR_RE = re.compile(r"\b(senior|sr\.?|manager|director|principal|lead|vp"
                       r"|head\s+of|associate\s+partner|executive)\b", re.I)
EXPLICIT_RE = re.compile(r"\b(intern|internship|co-?op)\b", re.I)


def is_internship(title):
    t = title or ""
    if not INTERN_RE.search(t):
        return False
    if SENIOR_RE.search(t) and not EXPLICIT_RE.search(t):
        return False
    return True

TORONTO = ("toronto",)
GTA = ("mississauga", "brampton", "markham", "vaughan", "richmond hill",
       "scarborough", "etobicoke", "north york", "oakville", "burlington",
       "ajax", "pickering", "whitby", "oshawa", "milton", "gta", "concord")
ONTARIO = ("waterloo", "kitchener", "ottawa", "hamilton", "guelph", "ontario",
           ", on", "(on)", "windsor", "barrie")


def geo(loc):
    l = (loc or "").lower()
    if any(k in l for k in TORONTO): return "Toronto"
    if any(k in l for k in GTA): return "GTA"
    if any(k in l for k in ONTARIO): return "Ontario"
    return None


def categorize(title):
    t = title or ""
    if EXCLUDE_RE.search(t):
        return []
    return [c for c, rx in CATEGORIES.items() if rx.search(t)]


def g(url, **kw):
    return requests.get(url, timeout=TIMEOUT, headers=UA, **kw)


def source_id(record, *keys):
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return None


# ---------------- sources -----------------------------------------------
GREENHOUSE = """
shopify faire ada league ritual flipp geotab stripe datadog coinbase roblox
pinterest airbnb instacart twilio asana figma anthropic gusto affirm robinhood
lyft reddit discord squarespace dropbox cloudflare gitlab elastic mongodb
vercel klaviyo toast flexport checkr carta chime sofi betterment marqeta
mercury samsara verkada brex riotgames d2l databricks
""".split()

ASHBY = "cohere ramp deel mercor openai harvey sierra elevenlabs".split()

# tenant, site, host  - only combos proven to resolve, plus business-heavy tries
WORKDAY = [
    ("cibc", "search", "wd3"), ("sunlife", "Experienced-Jobs", "wd3"),
    ("rbc", "RBCJobs", "wd3"), ("rbc", "External", "wd3"),
    ("td", "TD_Bank_Careers", "wd3"), ("scotiabank", "Scotiabank_Careers", "wd3"),
    ("bmo", "External", "wd3"), ("manulife", "MFCJH", "wd3"),
    ("telus", "TELUS_Careers", "wd3"), ("loblaw", "LCLCareers", "wd3"),
    ("rogers", "rogers-jobs", "wd3"), ("omers", "External", "wd3"),
    ("pwc", "Global_Experienced_Careers", "wd3"),
    # Accenture is on wd103, not wd3 - wd3 returns HTTP 422 and silently
    # contributed nothing. Confirmed from accenture.com/ca-en/careers.
    ("accenture", "AccentureCareers", "wd103"),
    ("deloittecanada", "External", "wd3"), ("kpmg", "External", "wd3"),
    ("cpp", "CPPIB_Careers", "wd3"), ("otpp", "External", "wd3"),
    ("cibc", "search", "wd1"), ("sunlife", "Experienced-Jobs", "wd1"),
]


def from_greenhouse(tok):
    r = g(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=true")
    if r.status_code != 200: return []
    out = []
    for j in r.json().get("jobs", []):
        out.append(dict(company=tok, src="greenhouse", title=j.get("title", ""),
                        loc=(j.get("location") or {}).get("name", ""),
                        url=j.get("absolute_url", ""),
                        external_id=source_id(j, "id"),
                        posted=j.get("first_published") or j.get("updated_at", ""),
                        content=re.sub("<[^>]+>", " ", j.get("content", "") or "")[:600]))
    return out


def from_ashby(tok):
    r = g(f"https://api.ashbyhq.com/posting-api/job-board/{tok}")
    if r.status_code != 200: return []
    d = r.json()
    if not isinstance(d, dict): return []
    return [dict(company=tok, src="ashby", title=j.get("title", ""),
                 loc=j.get("location", "") or "", url=j.get("jobUrl", ""),
                 external_id=source_id(j, "id"),
                 posted=j.get("publishedAt", "") or "",
                 content=re.sub("<[^>]+>", " ", j.get("descriptionHtml", "") or "")[:600])
            for j in d.get("jobs", [])]


def from_workday(spec):
    tenant, site, host = spec
    url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    out = []
    for term in ("intern", "co-op", "student"):
        for offset in (0, 20, 40):
            try:
                r = requests.post(url, timeout=TIMEOUT,
                                  headers={**UA, "Content-Type": "application/json"},
                                  json={"appliedFacets": {}, "limit": 20,
                                        "offset": offset, "searchText": term})
            except Exception:
                return out
            if r.status_code != 200:
                return out
            try:
                posts = r.json().get("jobPostings")
            except Exception:
                return out
            if not posts:
                break
            for j in posts:
                out.append(dict(company=tenant, src=f"workday",
                                title=j.get("title", ""),
                                loc=j.get("locationsText", "") or "",
                                url=f"https://{tenant}.{host}.myworkdayjobs.com/en-US/{site}"
                                    + (j.get("externalPath", "") or ""),
                                # Workday requisition IDs are tenant-scoped, so
                                # URL identity is safer across all employers.
                                external_id=None,
                                posted=j.get("postedOn", "") or "",
                                content=""))
            if len(posts) < 20:
                break
    return out


def from_amazon(_):
    out = []
    for q in ("intern finance", "intern marketing", "intern operations", "intern business"):
        for off in (0, 100):
            r = g("https://www.amazon.jobs/en/search.json?radius=100km"
                  f"&offset={off}&result_limit=100&sort=recent&base_query={q.replace(' ','%20')}"
                  "&loc_query=Toronto%2C%20Ontario%2C%20Canada&country=CAN")
            if r.status_code != 200: break
            hits = r.json().get("jobs", [])
            out += [dict(company="amazon", src="amazon", title=j.get("title", ""),
                         loc=j.get("location", "") or "",
                         url="https://amazon.jobs" + (j.get("job_path", "") or ""),
                         external_id=source_id(j, "id_icims", "id", "job_id"),
                         posted=j.get("posted_date", "") or "",
                         content=(j.get("description_short") or "")[:600]) for j in hits]
            if len(hits) < 100: break
    return out


# SuccessFactors (SAP) powers most of the big Canadian banks/telecoms. It has
# no public JSON API, but its search page renders server-side, so the job rows
# are parseable. Discovered by fingerprinting each employer's careers page.
#   (label, base, extra query params)
SUCCESSFACTORS = [
    ("scotiabank", "https://jobs.scotiabank.com", {}),
    ("rogers",     "https://jobs.rogers.com",     {}),
    ("deloitte",   "https://careers.deloitte.ca", {}),
]

SF_ROW = re.compile(r'href="(/(?:[A-Za-z0-9_\-]+/)?job/[^"]+)"[^>]*>\s*([^<]{6,140}?)\s*<', re.I)


def _unescape(s):
    for a, b in (("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"'),
                 ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    return " ".join(s.split())


# Employer titles carry invisible junk (zero-width spaces, soft hyphens, BOMs).
# It breaks console output on Windows and renders as stray gaps on the site.
INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\u2060"), None)


def clean_text(s):
    if not s:
        return ""
    s = str(s).translate(INVISIBLE)
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    return " ".join(s.split())


def from_successfactors(spec):
    label, base, extra = spec
    out, seen = [], set()
    for term in ("intern", "co-op", "student"):
        for start in (0, 25, 50):
            params = {"q": term, "locationsearch": "Toronto", "startrow": start}
            params.update(extra)
            try:
                r = g(f"{base}/search/", params=params)
            except Exception:
                return out
            if r.status_code != 200:
                return out
            rows = SF_ROW.findall(r.text)
            if not rows:
                break
            new = 0
            for href, title in rows:
                url = base + href
                if url in seen:
                    continue
                seen.add(url); new += 1
                title = _unescape(title)
                # The listing page does not expose a machine-readable location
                # or date; the slug carries the city, which is enough to place
                # it. Date is left empty rather than invented.
                loc = "Toronto, ON" if "/job/toronto" in href.lower() else ""
                out.append(dict(company=label, src="successfactors", title=title,
                                loc=loc, url=url, posted="", content=""))
            if new == 0:
                break
    return out


TASKS = ([(from_greenhouse, t) for t in GREENHOUSE]
         + [(from_ashby, t) for t in ASHBY]
         + [(from_workday, s) for s in WORKDAY]
         + [(from_successfactors, s) for s in SUCCESSFACTORS]
         + [(from_amazon, "amazon")])


def verify(rec):
    """Re-fetch the posting URL. Only keep it if the req is still open."""
    try:
        r = requests.get(rec["url"], timeout=25, headers=UA, allow_redirects=True)
    except Exception as e:
        return rec, False, f"error {type(e).__name__}"
    if r.status_code != 200:
        return rec, False, f"HTTP {r.status_code}"
    low = r.text.lower()
    for dead in ("no longer accepting", "position has been filled",
                 "job is no longer available", "posting is closed",
                 "no longer available", "req is closed"):
        if dead in low:
            return rec, False, "closed text"
    return rec, True, "200"


def main():
    t0 = time.time()
    raw = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(fn, tok): tok for fn, tok in TASKS}
        for f in as_completed(futs):
            try:
                raw.extend(f.result() or [])
            except Exception:
                pass
    print(f"pulled {len(raw)} raw postings in {time.time()-t0:.0f}s")

    # Normalize before any filtering: invisible characters would otherwise
    # break regex word boundaries and leak into the published JSON.
    for r in raw:
        r["title"] = clean_text(r.get("title"))
        r["loc"] = clean_text(r.get("loc"))

    # filter: intern + Toronto area. Category is TAGGED, not required -
    # dropping uncategorized rows would hide real postings from the user.
    cand = []
    for r in raw:
        if not r["title"] or not r["url"]:
            continue
        if not is_internship(r["title"]):
            continue
        gt = geo(r["loc"])
        if not gt:
            continue
        r["geo"] = gt
        r["cats"] = categorize(r["title"])
        cand.append(r)

    # Dedupe by source identity. Workday returns the same req multiple times
    # across the intern/co-op/student search terms, while two distinct reqs can
    # legitimately share a title and location.
    seen, uniq = set(), []
    for r in cand:
        if r.get("external_id"):
            k = ("external", r["src"], r["external_id"])
        elif r.get("url"):
            k = ("url", r["src"], r["url"])
        else:
            k = ("fallback", r["src"], r["company"],
                 r["title"].strip().lower(), r["loc"].strip().lower())
        if k in seen: continue
        seen.add(k); uniq.append(r)
    print(f"{len(uniq)} unique Toronto-area internships (before liveness check)")

    # verify every URL is still live
    live, dropped = [], []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for rec, ok, why in ex.map(lambda x: verify(x), uniq):
            (live if ok else dropped).append((rec, why))
    print(f"VERIFIED LIVE: {len(live)}   DROPPED (closed/unreachable): {len(dropped)}")
    for rec, why in dropped:
        print(f"   drop [{why}] {rec['company']} - {rec['title'][:50]}")

    out = []
    for rec, _ in live:
        out.append({
            "company": rec["company"], "title": rec["title"], "loc": rec["loc"],
            "geo": rec["geo"], "cats": rec["cats"], "url": rec["url"],
            "posted": rec["posted"], "src": rec["src"],
            "external_id": rec.get("external_id"),
            "content": " ".join((rec.get("content") or "").split())[:400],
        })
    out.sort(key=lambda r: (r["company"], r["title"], r["url"]))

    print("\n=== SHIPPING ===")
    for r in out:
        line = f"[{r['geo']:<7}] {r['company']:<14} | {'/'.join(r['cats']):<30} | {r['title'][:52]}"
        # Never let a console encoding issue abort the run before the JSON
        # snapshot is written - reporting must not outrank the actual output.
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))

    with open("live_jobs.json", "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(),
                   "categories": list(CATEGORIES.keys()),
                   "jobs": out}, fh, indent=2)
    print(f"\nWrote live_jobs.json with {len(out)} VERIFIED postings")


if __name__ == "__main__":
    main()
