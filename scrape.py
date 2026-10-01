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

import json, os, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Lock, Semaphore
import requests

TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
SEARCH_TERMS = ("intern", "co-op", "student")
WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_PAGES = 100
WORKDAY_RETRIES = 3
WORKDAY_RETRY_BASE_SECONDS = 0.5
SOURCE_RETRIES = 3
SOURCE_RETRY_BASE_SECONDS = 0.5
SOURCE_REQUESTS = Semaphore(8)
SUCCESSFACTORS_PAGE_SIZE = 25
SUCCESSFACTORS_MAX_PAGES = 100
VERIFY_RETRIES = 3
VERIFY_RETRY_BASE_SECONDS = 0.5
ADZUNA_RESULTS_PER_PAGE = 50
ADZUNA_REQUESTS_PER_ACTIVE_RUN = 5
ADZUNA_REFRESH_INTERVAL_HOURS = 2
ADZUNA_GRACE_HOURS = 6
ADZUNA_WHERE = "Toronto"
ADZUNA_DISTANCE_KM = 100
ADZUNA_QUERY_NAMES = ("internship", "intern", "coop", "student", "new-grad")

COVERAGE = {}
COVERAGE_LOCK = Lock()


class SourceRequestBudgetExceeded(RuntimeError):
    pass

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
FACET_LOCATION_TERMS = TORONTO + GTA + tuple(
    term for term in ONTARIO if term not in ("ontario", ", on", "(on)")
)
CANADIAN_LOCATION_RE = re.compile(
    r"(?:\bontario\b|,\s*on(?:\b|,)|\bcanada\b|,\s*can(?:\b|,))", re.I)


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


def g(url, request_budget=None, **kw):
    last_response = None
    last_error = None
    for attempt in range(SOURCE_RETRIES):
        if request_budget is not None:
            if request_budget["remaining"] <= 0:
                raise SourceRequestBudgetExceeded("request budget exhausted")
            request_budget["remaining"] -= 1
        try:
            with SOURCE_REQUESTS:
                response = requests.get(url, timeout=TIMEOUT, headers=UA, **kw)
            if response.status_code != 429 and response.status_code < 500:
                return response
            last_response = response
        except requests.RequestException as exc:
            last_error = exc
        if attempt + 1 < SOURCE_RETRIES:
            time.sleep(SOURCE_RETRY_BASE_SECONDS * (2 ** attempt))
    if last_response is not None:
        return last_response
    if last_error:
        raise last_error
    raise RuntimeError("Source request failed")


def reset_coverage():
    with COVERAGE_LOCK:
        COVERAGE.clear()


def set_coverage(key, **values):
    with COVERAGE_LOCK:
        current = COVERAGE.setdefault(key, {})
        current.update(values)


def coverage_key(source, target):
    return f"{source}:{target}"


def with_coverage(record, key):
    record["_coverage_key"] = key
    return record


def target_location_descriptor(descriptor):
    text = clean_text(descriptor).lower()
    if not text or not any(term in text for term in FACET_LOCATION_TERMS):
        return False
    if CANADIAN_LOCATION_RE.search(text):
        return True
    normalized = re.sub(r"[^a-z]+", " ", text).strip()
    return normalized in {re.sub(r"[^a-z]+", " ", term).strip()
                          for term in FACET_LOCATION_TERMS}


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
    key = coverage_key("greenhouse", tok)
    try:
        r = g(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=true")
        if r.status_code != 200:
            set_coverage(key, source="greenhouse", target=tok, pages=1,
                         returned=0, query_total=0, complete=False,
                         errors=[f"HTTP {r.status_code}"])
            return []
        jobs = r.json().get("jobs", [])
        out = []
        for j in jobs:
            out.append(with_coverage(
                dict(company=tok, src="greenhouse", title=j.get("title", ""),
                     loc=(j.get("location") or {}).get("name", ""),
                     url=j.get("absolute_url", ""),
                     external_id=source_id(j, "id"),
                     posted=j.get("first_published") or j.get("updated_at", ""),
                     content=re.sub("<[^>]+>", " ", j.get("content", "") or "")[:600]),
                key))
        set_coverage(key, source="greenhouse", target=tok, pages=1,
                     returned=len(out), query_total=len(jobs), selected_total=len(jobs),
                     location_mode="provider", fallback_terms=0, capped_terms=0,
                     errors=[], complete=True)
        return out
    except Exception as exc:
        set_coverage(key, source="greenhouse", target=tok, pages=0,
                     returned=0, query_total=0, complete=False,
                     errors=[type(exc).__name__])
        return []


def from_ashby(tok):
    key = coverage_key("ashby", tok)
    try:
        r = g(f"https://api.ashbyhq.com/posting-api/job-board/{tok}")
        if r.status_code != 200:
            set_coverage(key, source="ashby", target=tok, pages=1,
                         returned=0, query_total=0, complete=False,
                         errors=[f"HTTP {r.status_code}"])
            return []
        d = r.json()
        if not isinstance(d, dict):
            raise ValueError("non-object response")
        jobs = d.get("jobs", [])
        out = [
            with_coverage(
                dict(company=tok, src="ashby", title=j.get("title", ""),
                     loc=j.get("location", "") or "", url=j.get("jobUrl", ""),
                     external_id=source_id(j, "id"),
                     posted=j.get("publishedAt", "") or "",
                     content=re.sub("<[^>]+>", " ",
                                    j.get("descriptionHtml", "") or "")[:600]),
                key)
            for j in jobs
        ]
        set_coverage(key, source="ashby", target=tok, pages=1,
                     returned=len(out), query_total=len(jobs), selected_total=len(jobs),
                     location_mode="provider", fallback_terms=0, capped_terms=0,
                     errors=[], complete=True)
        return out
    except Exception as exc:
        set_coverage(key, source="ashby", target=tok, pages=0,
                     returned=0, query_total=0, complete=False,
                     errors=[type(exc).__name__])
        return []


def _iter_workday_facets(facets, inside_location_group=False):
    for facet in facets or []:
        if not isinstance(facet, dict):
            continue
        parameter = str(facet.get("facetParameter") or "")
        inside = inside_location_group or parameter.casefold() == "locationmaingroup"
        yield facet, inside_location_group
        nested = [
            value for value in (facet.get("values") or [])
            if isinstance(value, dict) and value.get("facetParameter")
        ]
        yield from _iter_workday_facets(nested, inside)


def workday_location_filter(facets):
    candidates = []
    for facet, inside_location_group in _iter_workday_facets(facets):
        parameter = str(facet.get("facetParameter") or "")
        parameter_key = parameter.casefold()
        if parameter_key not in {"locations", "location", "city"}:
            continue
        matches = [
            value for value in (facet.get("values") or [])
            if isinstance(value, dict)
            and value.get("id")
            and target_location_descriptor(value.get("descriptor"))
        ]
        if not matches:
            continue
        priority = (
            0 if inside_location_group and parameter_key == "locations"
            else 1 if parameter_key == "locations"
            else 2
        )
        candidates.append((priority, -len(matches), parameter, matches))
    if not candidates:
        return None
    _, _, parameter, matches = min(candidates, key=lambda item: (item[0], item[1]))
    return {
        "parameter": parameter,
        "ids": [str(value["id"]) for value in matches],
        "descriptors": [clean_text(value.get("descriptor")) for value in matches],
    }


def _workday_json(url, term, offset, applied_facets):
    payload = {
        "appliedFacets": applied_facets,
        "limit": WORKDAY_PAGE_SIZE,
        "offset": offset,
        "searchText": term,
    }
    last_error = None
    for attempt in range(WORKDAY_RETRIES):
        try:
            with SOURCE_REQUESTS:
                response = requests.post(
                    url,
                    timeout=TIMEOUT,
                    headers={**UA, "Content-Type": "application/json"},
                    json=payload,
                )
            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("non-object response")
                return data
            if response.status_code != 429 and response.status_code < 500:
                raise RuntimeError(f"HTTP {response.status_code}")
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except requests.RequestException as exc:
            last_error = exc
        if attempt + 1 < WORKDAY_RETRIES:
            time.sleep(WORKDAY_RETRY_BASE_SECONDS * (2 ** attempt))
    if last_error:
        raise last_error
    raise RuntimeError("Workday request failed")


def workday_external_id(tenant, external_path):
    final_segment = str(external_path or "").rstrip("/").rsplit("/", 1)[-1]
    match = re.search(
        r"_((?:JR|REQ|R)[A-Za-z0-9_-]+|\d{6,}(?:-\d+)?)$",
        final_segment,
        re.I,
    )
    return f"{tenant}:{match.group(1)}" if match else None


def from_workday(spec):
    tenant, site, host = spec
    target = f"{tenant}.{host}/{site}"
    key = coverage_key("workday", target)
    url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    out, seen = [], set()
    metrics = {
        "source": "workday", "target": target, "facet_requests": 0,
        "query_total": 0, "selected_total": 0, "pages": 0, "returned": 0,
        "facet_terms": 0, "fallback_terms": 0, "capped_terms": 0,
        "repeated_terms": 0, "facet_values": 0, "facet_parameters": set(),
        "errors": [],
    }

    for term in SEARCH_TERMS:
        try:
            discovery = _workday_json(url, term, 0, {})
            metrics["facet_requests"] += 1
        except Exception as exc:
            metrics["errors"].append(
                f"{term}:{type(exc).__name__}({clean_text(exc)[:80]})")
            continue

        metrics["query_total"] += int(discovery.get("total") or 0)
        location_filter = workday_location_filter(discovery.get("facets"))
        if location_filter:
            applied_facets = {
                location_filter["parameter"]: location_filter["ids"]
            }
            metrics["facet_terms"] += 1
            metrics["facet_values"] += len(location_filter["ids"])
            metrics["facet_parameters"].add(location_filter["parameter"])
            first_page = None
        else:
            applied_facets = {}
            metrics["fallback_terms"] += 1
            first_page = discovery

        offset = 0
        term_pages = 0
        selected_total_recorded = False
        page_signatures = set()
        exhausted = False
        while term_pages < WORKDAY_MAX_PAGES:
            try:
                if first_page is not None and offset == 0:
                    data = first_page
                else:
                    data = _workday_json(url, term, offset, applied_facets)
            except Exception as exc:
                metrics["errors"].append(
                    f"{term}@{offset}:{type(exc).__name__}"
                    f"({clean_text(exc)[:80]})")
                break

            term_pages += 1
            metrics["pages"] += 1
            total = int(data.get("total") or 0)
            if not selected_total_recorded:
                metrics["selected_total"] += total
                selected_total_recorded = True
            posts = data.get("jobPostings") or []
            metrics["returned"] += len(posts)
            if not posts:
                exhausted = True
                break

            signature = tuple(
                (job.get("externalPath"), job.get("title"), job.get("locationsText"))
                for job in posts
            )
            if signature in page_signatures:
                metrics["repeated_terms"] += 1
                break
            page_signatures.add(signature)

            for job in posts:
                path = job.get("externalPath", "") or ""
                if not path:
                    continue
                identity = path
                if identity in seen:
                    continue
                seen.add(identity)
                location = clean_text(job.get("locationsText"))
                if location_filter and not geo(location):
                    location = (
                        f"{location or 'Multiple locations'} "
                        "(approved Ontario location)"
                    )
                out.append(with_coverage(
                    dict(company=tenant, src="workday",
                         title=job.get("title", ""),
                         loc=location,
                         url=f"https://{tenant}.{host}.myworkdayjobs.com/en-US/{site}"
                             + path,
                         external_id=workday_external_id(tenant, path),
                         posted=job.get("postedOn", "") or "",
                         content=""),
                    key))

            if len(posts) < WORKDAY_PAGE_SIZE or (
                total and offset + len(posts) >= total
            ):
                exhausted = True
                break
            offset += WORKDAY_PAGE_SIZE

        if not exhausted and term_pages >= WORKDAY_MAX_PAGES:
            metrics["capped_terms"] += 1

    metrics["records"] = len(out)
    metrics["facet_parameters"] = ",".join(sorted(metrics["facet_parameters"])) or "-"
    metrics["complete"] = not metrics["capped_terms"] and not metrics["errors"]
    set_coverage(key, **metrics)
    return out


def from_amazon(_):
    key = coverage_key("amazon", "amazon")
    out, pages, returned, errors = [], 0, 0, []
    for q in ("intern finance", "intern marketing", "intern operations", "intern business"):
        for off in (0, 100):
            try:
                r = g("https://www.amazon.jobs/en/search.json?radius=100km"
                      f"&offset={off}&result_limit=100&sort=recent&base_query={q.replace(' ','%20')}"
                      "&loc_query=Toronto%2C%20Ontario%2C%20Canada&country=CAN")
                pages += 1
                if r.status_code != 200:
                    errors.append(f"{q}:HTTP {r.status_code}")
                    break
                hits = r.json().get("jobs", [])
            except Exception as exc:
                errors.append(f"{q}:{type(exc).__name__}")
                break
            returned += len(hits)
            out += [
                with_coverage(
                    dict(company="amazon", src="amazon", title=j.get("title", ""),
                         loc=j.get("location", "") or "",
                         url="https://amazon.jobs" + (j.get("job_path", "") or ""),
                         external_id=source_id(j, "id_icims", "id", "job_id"),
                         posted=j.get("posted_date", "") or "",
                         content=(j.get("description_short") or "")[:600]),
                    key)
                for j in hits
            ]
            if len(hits) < 100:
                break
    set_coverage(key, source="amazon", target="amazon", pages=pages,
                 returned=returned, records=len(out), query_total=None,
                 selected_total=None, location_mode="provider",
                 fallback_terms=0, capped_terms=0, errors=errors,
                 complete=not errors)
    return out


def adzuna_queries():
    return [
        {"name": "internship", "params": {"title_only": "internship"}},
        {"name": "intern", "params": {"title_only": "intern"}},
        {"name": "coop", "params": {"what_or": "co-op coop"}},
        {"name": "student", "params": {"title_only": "student"}},
        {"name": "new-grad", "params": {"what_phrase": "new grad"}},
    ]


def _parse_timestamp(value):
    if not value:
        return None
    candidate = str(value)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_adzuna_baseline(now=None):
    now = now or datetime.now(timezone.utc)
    baseline_path = os.environ.get("ADZUNA_BASELINE_PATH", "live_jobs.json")
    try:
        with open(baseline_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"rows": [], "refreshed_at": None, "age_hours": None}

    rows = [
        dict(job) for job in (snapshot.get("jobs") or [])
        if isinstance(job, dict) and job.get("src") == "adzuna"
    ]
    state = ((snapshot.get("source_state") or {}).get("adzuna") or {})
    refreshed_at = _parse_timestamp(
        state.get("refreshed_at")
        or (snapshot.get("fetched_at") if rows else None)
    )
    age_hours = None
    if refreshed_at:
        age_hours = max(0.0, (now - refreshed_at).total_seconds() / 3600)
    return {
        "rows": rows,
        "refreshed_at": refreshed_at,
        "age_hours": age_hours,
    }


def _truthy_env(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _adzuna_carried_rows(rows, key):
    return [with_coverage(dict(row), key) for row in rows]


def from_adzuna(_, now=None):
    key = coverage_key("adzuna", "ca/Toronto")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    baseline = load_adzuna_baseline(now)
    baseline_fresh = (
        bool(baseline["rows"])
        and baseline["age_hours"] is not None
        and baseline["age_hours"] <= ADZUNA_GRACE_HOURS
    )
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        if baseline_fresh:
            rows = _adzuna_carried_rows(baseline["rows"], key)
            set_coverage(
                key, source="adzuna", target="ca/Toronto",
                status="reused-missing-credentials", pages=0,
                request_attempts=0, returned=len(rows), records=len(rows),
                reused=len(rows), query_total=None, selected_total=None,
                location_mode="carry-forward", fallback_terms=0,
                capped_terms=0, repeated_terms=0, errors=[],
                source_refreshed_at=baseline["refreshed_at"].isoformat(),
                source_age_hours=baseline["age_hours"], complete=False)
            return rows
        if baseline["rows"]:
            set_coverage(
                key, source="adzuna", target="ca/Toronto", status="expired",
                pages=0, request_attempts=0, returned=0, records=0, reused=0,
                query_total=None, selected_total=None,
                location_mode="carry-forward", fallback_terms=0,
                capped_terms=0, repeated_terms=0,
                errors=["credentials unavailable after baseline grace"],
                source_refreshed_at=(
                    baseline["refreshed_at"].isoformat()
                    if baseline["refreshed_at"] else None),
                source_age_hours=baseline["age_hours"], complete=False)
            return []
        set_coverage(
            key, source="adzuna", target="ca/Toronto", status="skipped",
            pages=0, returned=0, records=0, query_total=None,
            selected_total=None, location_mode="where+distance",
            fallback_terms=0, capped_terms=0, repeated_terms=0,
            errors=[], complete=True)
        return []

    force_refresh = _truthy_env("ADZUNA_FORCE_REFRESH")
    hour_slot = int(now.timestamp() // 3600)
    refresh_due = (
        force_refresh
        or not baseline["rows"]
        or baseline["refreshed_at"] is None
        or hour_slot % ADZUNA_REFRESH_INTERVAL_HOURS == 0
    )
    if not refresh_due:
        if baseline_fresh:
            rows = _adzuna_carried_rows(baseline["rows"], key)
            set_coverage(
                key, source="adzuna", target="ca/Toronto",
                status="reused-off-hour", pages=0, request_attempts=0,
                returned=len(rows), records=len(rows), reused=len(rows),
                query_total=None, selected_total=None,
                location_mode="carry-forward", fallback_terms=0,
                capped_terms=0, repeated_terms=0, errors=[],
                source_refreshed_at=baseline["refreshed_at"].isoformat(),
                source_age_hours=baseline["age_hours"], complete=True)
            return rows
        set_coverage(
            key, source="adzuna", target="ca/Toronto", status="expired",
            pages=0, request_attempts=0, returned=0, records=0, reused=0,
            query_total=None, selected_total=None,
            location_mode="carry-forward", fallback_terms=0,
            capped_terms=0, repeated_terms=0,
            errors=["baseline grace expired"], source_refreshed_at=(
                baseline["refreshed_at"].isoformat()
                if baseline["refreshed_at"] else None),
            source_age_hours=baseline["age_hours"], complete=False)
        return []

    out, seen = [], set()
    pages = returned = 0
    query_total = 0
    capped_terms = 0
    errors = []
    query_metrics = []
    request_budget = {"remaining": ADZUNA_REQUESTS_PER_ACTIVE_RUN}

    for query in adzuna_queries():
        url = "https://api.adzuna.com/v1/api/jobs/ca/search/1"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": ADZUNA_RESULTS_PER_PAGE,
            "where": ADZUNA_WHERE,
            "distance": ADZUNA_DISTANCE_KM,
            "sort_by": "date",
            "content-type": "application/json",
            **query["params"],
        }
        query_metric = {
            "name": query["name"], "total": None, "returned": 0,
            "kept": 0, "capped": False, "status": "success",
        }
        try:
            response = g(url, params=params, request_budget=request_budget)
            pages += 1
            if response.status_code != 200:
                message = f"{query['name']}:HTTP {response.status_code}"
                errors.append(message)
                query_metric["status"] = message
                query_metrics.append(query_metric)
                continue
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("non-object response")
        except Exception as exc:
            message = f"{query['name']}:{type(exc).__name__}"
            errors.append(message)
            query_metric["status"] = message
            query_metrics.append(query_metric)
            continue

        total = int(data.get("count") or 0)
        query_total += total
        query_metric["total"] = total
        results = data.get("results") or []
        if not isinstance(results, list):
            message = f"{query['name']}:invalid results"
            errors.append(message)
            query_metric["status"] = message
            query_metrics.append(query_metric)
            continue
        returned += len(results)
        query_metric["returned"] = len(results)
        query_metric["capped"] = (
            total > ADZUNA_RESULTS_PER_PAGE
            and len(results) >= ADZUNA_RESULTS_PER_PAGE
        )
        if query_metric["capped"]:
            capped_terms += 1

        for result in results:
            external_id = source_id(result, "id")
            redirect_url = clean_text(result.get("redirect_url"))
            if not external_id or not redirect_url:
                continue
            identity = str(external_id)
            if identity in seen:
                continue
            seen.add(identity)
            query_metric["kept"] += 1
            company = clean_text(
                (result.get("company") or {}).get("display_name")
            ) or "Undisclosed employer"
            location = clean_text(
                (result.get("location") or {}).get("display_name")
            )
            title = clean_text(
                re.sub("<[^>]+>", " ", str(result.get("title") or "")))
            description = clean_text(
                re.sub("<[^>]+>", " ", str(result.get("description") or "")))
            out.append(with_coverage(
                dict(
                    company=company,
                    src="adzuna",
                    title=title,
                    loc=location,
                    url=redirect_url,
                    external_id=identity,
                    posted=result.get("created", "") or "",
                    content=description[:600],
                ),
                key))
        query_metrics.append(query_metric)

    active_success = (
        not errors
        and len(query_metrics) == len(ADZUNA_QUERY_NAMES)
        and all(query["status"] == "success" for query in query_metrics)
    )
    request_attempts = (
        max(
            pages,
            ADZUNA_REQUESTS_PER_ACTIVE_RUN - request_budget["remaining"],
        )
    )
    if active_success:
        refreshed_at = now.isoformat()
        set_coverage(
            key, source="adzuna", target="ca/Toronto", status="fresh",
            query_total=query_total, selected_total=query_total,
            pages=pages, request_attempts=request_attempts,
            returned=returned, records=len(out), reused=0,
            location_mode="where+distance", fallback_terms=0,
            capped_terms=capped_terms, repeated_terms=0, queries=query_metrics,
            errors=[], source_refreshed_at=refreshed_at,
            source_age_hours=0.0, complete=not capped_terms)
        return out

    if baseline_fresh:
        rows = _adzuna_carried_rows(baseline["rows"], key)
        set_coverage(
            key, source="adzuna", target="ca/Toronto",
            status="reused-partial", query_total=query_total,
            selected_total=query_total, pages=pages,
            request_attempts=request_attempts, returned=returned,
            records=len(rows), reused=len(rows),
            partial_records=len(out), location_mode="carry-forward",
            fallback_terms=0, capped_terms=capped_terms, repeated_terms=0,
            queries=query_metrics, errors=errors,
            source_refreshed_at=baseline["refreshed_at"].isoformat(),
            source_age_hours=baseline["age_hours"], complete=False)
        return rows

    set_coverage(
        key, source="adzuna", target="ca/Toronto", status="expired",
        query_total=query_total, selected_total=query_total,
        pages=pages,
        request_attempts=request_attempts,
        returned=returned, records=0, reused=0,
        partial_records=len(out), location_mode="carry-forward",
        fallback_terms=0,
        capped_terms=capped_terms, repeated_terms=0, queries=query_metrics,
        errors=errors or ["baseline unavailable or grace expired"],
        source_refreshed_at=(
            baseline["refreshed_at"].isoformat()
            if baseline["refreshed_at"] else None),
        source_age_hours=baseline["age_hours"], complete=False)
    return []


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
    key = coverage_key("successfactors", label)
    out, seen = [], set()
    pages = returned = repeated_terms = capped_terms = 0
    errors = []
    for term in SEARCH_TERMS:
        start = 0
        exhausted = False
        page_signatures = set()
        term_pages = 0
        while term_pages < SUCCESSFACTORS_MAX_PAGES:
            params = {"q": term, "locationsearch": "Toronto", "startrow": start}
            params.update(extra)
            try:
                r = g(f"{base}/search/", params=params)
            except Exception as exc:
                errors.append(f"{term}@{start}:{type(exc).__name__}")
                break
            pages += 1
            term_pages += 1
            if r.status_code != 200:
                errors.append(f"{term}@{start}:HTTP {r.status_code}")
                break
            rows = SF_ROW.findall(r.text)
            returned += len(rows)
            if not rows:
                exhausted = True
                break
            signature = tuple(href for href, _ in rows)
            if signature in page_signatures:
                repeated_terms += 1
                break
            page_signatures.add(signature)
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
                out.append(with_coverage(
                    dict(company=label, src="successfactors", title=title,
                         loc=loc, url=url, posted="", content=""),
                    key))
            if new == 0:
                repeated_terms += 1
                break
            start += SUCCESSFACTORS_PAGE_SIZE
        if not exhausted and term_pages >= SUCCESSFACTORS_MAX_PAGES:
            capped_terms += 1
    set_coverage(
        key, source="successfactors", target=label, query_total=None,
        selected_total=None, pages=pages, returned=returned, records=len(out),
        location_mode="query", fallback_terms=0, capped_terms=capped_terms,
        repeated_terms=repeated_terms, errors=errors,
        complete=not capped_terms and not errors)
    return out


TASKS = (
    [(from_greenhouse, target, coverage_key("greenhouse", target))
     for target in GREENHOUSE]
    + [(from_ashby, target, coverage_key("ashby", target))
       for target in ASHBY]
    + [(from_workday, spec,
        coverage_key("workday", f"{spec[0]}.{spec[2]}/{spec[1]}"))
       for spec in WORKDAY]
    + [(from_successfactors, spec, coverage_key("successfactors", spec[0]))
       for spec in SUCCESSFACTORS]
    + [(from_amazon, "amazon", coverage_key("amazon", "amazon"))]
    + [(from_adzuna, "adzuna", coverage_key("adzuna", "ca/Toronto"))]
)


def verify(rec):
    """Re-fetch the posting URL. Only keep it if the req is still open."""
    last_error = None
    r = None
    for attempt in range(VERIFY_RETRIES):
        try:
            with SOURCE_REQUESTS:
                r = requests.get(
                    rec["url"], timeout=25, headers=UA, allow_redirects=True)
            if r.status_code != 429 and r.status_code < 500:
                break
            last_error = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last_error = f"error {type(exc).__name__}"
        if attempt + 1 < VERIFY_RETRIES:
            time.sleep(VERIFY_RETRY_BASE_SECONDS * (2 ** attempt))
    if r is None:
        return rec, False, last_error or "error RequestException"
    if r.status_code != 200:
        return rec, False, f"HTTP {r.status_code}"
    low = r.text.lower()
    for dead in ("no longer accepting", "position has been filled",
                 "job is no longer available", "posting is closed",
                 "no longer available", "req is closed"):
        if dead in low:
            return rec, False, "closed text"
    return rec, True, "200"


def _metric(value):
    return "-" if value is None else str(value)


def print_coverage_report(filtered_counts, unique_counts, live_counts):
    print("\n=== SOURCE COVERAGE ===")
    print("source         target                           totals(raw/selected) "
          "pages returned filtered unique live mode status")
    with COVERAGE_LOCK:
        entries = [(key, dict(value)) for key, value in COVERAGE.items()]
    summary_rows, warnings, query_details, source_details = [], [], [], []
    for key, entry in sorted(entries):
        filtered = filtered_counts.get(key, 0)
        unique = unique_counts.get(key, 0)
        live = live_counts.get(key, 0)
        status = entry.get("status") or (
            "complete" if entry.get("complete", True) else "incomplete")
        mode = entry.get("facet_parameters") or entry.get("location_mode") or "-"
        print(
            f"{entry.get('source', '-'):<14} {entry.get('target', '-')[:32]:<32} "
            f"{_metric(entry.get('query_total')):>7}/"
            f"{_metric(entry.get('selected_total')):<8} "
            f"{entry.get('pages', 0):>5} {entry.get('returned', 0):>8} "
            f"{filtered:>8} {unique:>6} {live:>4} "
            f"{mode[:16]:<16} {status}"
        )
        summary_rows.append((
            entry.get("source", "-"), entry.get("target", "-"),
            f"{_metric(entry.get('query_total'))}/{_metric(entry.get('selected_total'))}",
            entry.get("pages", 0), entry.get("returned", 0),
            filtered, unique, live, mode, status,
        ))
        for query in entry.get("queries") or []:
            detail = (
                f"{key}/{query.get('name')}: total={_metric(query.get('total'))}, "
                f"returned={query.get('returned', 0)}, kept={query.get('kept', 0)}, "
                f"capped={str(bool(query.get('capped'))).lower()}, "
                f"status={query.get('status', 'success')}"
            )
            query_details.append(detail)
            print(f"   QUERY {detail}")
        if entry.get("source_age_hours") is not None or entry.get("reused"):
            source_age = entry.get("source_age_hours")
            state_detail = (
                f"{key}: status={status}, age_hours="
                f"{_metric(round(source_age, 2) if source_age is not None else None)}, "
                f"reused={entry.get('reused', 0)}, "
                f"refreshed_at={entry.get('source_refreshed_at') or '-'}"
            )
            source_details.append(state_detail)
            print(f"   SOURCE STATE {state_detail}")
        if entry.get("fallback_terms"):
            warning = (
                f"{key}: no usable target-location facet for "
                f"{entry['fallback_terms']} term(s); nationwide fallback was "
                "paginated and may hit the safety cap"
            )
            warnings.append(warning)
            print(f"   COVERAGE WARNING {warning}")
        if entry.get("capped_terms"):
            warning = (
                f"{key}: safety cap reached for {entry['capped_terms']} "
                "term(s); coverage is incomplete"
            )
            warnings.append(warning)
            print(f"   COVERAGE WARNING {warning}")
        if entry.get("errors"):
            warning = f"{key}: request errors {','.join(entry['errors'])}"
            warnings.append(warning)
            print(f"   COVERAGE WARNING {warning}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as summary:
                summary.write("\n### Source coverage\n\n")
                summary.write(
                    "| Source | Target | Query totals raw/selected | Pages | "
                    "Returned | Filtered | Unique | Live | Mode | Status |\n"
                )
                summary.write("|---|---|---:|---:|---:|---:|---:|---:|---|---|\n")
                for row in summary_rows:
                    summary.write(
                        "| " + " | ".join(str(value).replace("|", "\\|")
                                         for value in row) + " |\n"
                    )
                if warnings:
                    summary.write("\n**Coverage warnings**\n\n")
                    for warning in warnings:
                        summary.write(f"- {warning}\n")
                if query_details:
                    summary.write("\n**Per-query coverage**\n\n")
                    for detail in query_details:
                        summary.write(f"- {detail}\n")
                if source_details:
                    summary.write("\n**Source freshness**\n\n")
                    for detail in source_details:
                        summary.write(f"- {detail}\n")
        except OSError as exc:
            print(f"COVERAGE WARNING could not write GITHUB_STEP_SUMMARY: {exc}")


def source_state_payload():
    key = coverage_key("adzuna", "ca/Toronto")
    with COVERAGE_LOCK:
        entry = dict(COVERAGE.get(key) or {})
    if not entry:
        return {}
    age_hours = entry.get("source_age_hours")
    return {
        "adzuna": {
            "status": entry.get("status"),
            "refreshed_at": entry.get("source_refreshed_at"),
            "age_seconds": (
                round(float(age_hours) * 3600)
                if age_hours is not None else None
            ),
            "requests": entry.get("request_attempts", 0),
            "reused": entry.get("reused", 0),
        }
    }


def main():
    t0 = time.time()
    reset_coverage()
    raw = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(fn, token): key for fn, token, key in TASKS}
        for f in as_completed(futs):
            try:
                raw.extend(f.result() or [])
            except Exception as exc:
                key = futs[f]
                source, _, target = key.partition(":")
                set_coverage(
                    key, source=source, target=target, pages=0, returned=0,
                    query_total=None, selected_total=None, complete=False,
                    errors=[type(exc).__name__])
                print(f"COVERAGE WARNING {key}: task failed with {type(exc).__name__}")
    print(f"pulled {len(raw)} raw postings in {time.time()-t0:.0f}s")

    # Normalize before any filtering: invisible characters would otherwise
    # break regex word boundaries and leak into the published JSON.
    for r in raw:
        r["title"] = clean_text(r.get("title"))
        r["loc"] = clean_text(r.get("loc"))

    # filter: intern + Toronto area. Category is TAGGED, not required -
    # dropping uncategorized rows would hide real postings from the user.
    cand = []
    filtered_counts = {}
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
        key = r.get("_coverage_key")
        filtered_counts[key] = filtered_counts.get(key, 0) + 1

    # Dedupe by source identity. Workday returns the same req multiple times
    # across the intern/co-op/student search terms, while two distinct reqs can
    # legitimately share a title and location.
    seen, uniq = set(), []
    unique_counts = {}
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
        key = r.get("_coverage_key")
        unique_counts[key] = unique_counts.get(key, 0) + 1
    print(f"{len(uniq)} unique Toronto-area internships (before liveness check)")

    # verify every URL is still live
    live, dropped = [], []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for rec, ok, why in ex.map(lambda x: verify(x), uniq):
            (live if ok else dropped).append((rec, why))
    print(f"VERIFIED LIVE: {len(live)}   DROPPED (closed/unreachable): {len(dropped)}")
    for rec, why in dropped:
        print(f"   drop [{why}] {rec['company']} - {rec['title'][:50]}")

    live_counts = {}
    for rec, _ in live:
        key = rec.get("_coverage_key")
        live_counts[key] = live_counts.get(key, 0) + 1
    print_coverage_report(filtered_counts, unique_counts, live_counts)

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

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "categories": list(CATEGORIES.keys()),
        "jobs": out,
    }
    source_state = source_state_payload()
    if source_state:
        payload["source_state"] = source_state
    with open("live_jobs.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nWrote live_jobs.json with {len(out)} VERIFIED postings")


if __name__ == "__main__":
    main()
