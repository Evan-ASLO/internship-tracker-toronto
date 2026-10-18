"""
v3 - REAL DATA ONLY.

Rules:
  1. Every posting comes from a live ATS API response. Nothing is invented.
  2. Posted dates come from the API, never estimated.
  3. Every URL is re-fetched and must return HTTP 200 before it ships.
     Closed reqs (404/410) are dropped.
  4. Career-track tags cover Marketing, Business Operations, Finance,
     Consulting, and Software Engineering.
"""

import argparse
import json, os, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Semaphore
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
import requests

from prelaw_sources import (
    DAILY_GRACE_HOURS,
    DAILY_REFRESH_INTERVAL_HOURS,
    LEGAL_CATEGORY,
    ORACLE_COMPANY,
    POLICY_CATEGORY,
    PRELAW_CATEGORIES,
    classify_prelaw,
    collect_cnrl_oracle,
    collect_gc_jobs,
    html_text,
    mandatory_legal_education_reason,
    restricted_program_note,
)
from publish_snapshot import canonicalize_url

TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
SEARCH_TERMS = ("intern", "co-op", "student", "stagiaire", "\u00e9tudiant")
WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_PAGES = 100
WORKDAY_RETRIES = 3
WORKDAY_RETRY_BASE_SECONDS = 0.5
SOURCE_RETRIES = 3
SOURCE_RETRY_BASE_SECONDS = 0.5
SOURCE_REQUESTS = Semaphore(8)
SOURCE_HOST_MIN_INTERVALS = {
    "api.lever.co": 1.0,
    "jobs.lever.co": 1.0,
}
SOURCE_HOST_LOCKS = {host: Lock() for host in SOURCE_HOST_MIN_INTERVALS}
SOURCE_HOST_NEXT_REQUEST_AT = {host: 0.0 for host in SOURCE_HOST_MIN_INTERVALS}
SUCCESSFACTORS_PAGE_SIZE = 25
SUCCESSFACTORS_MAX_PAGES = 100
VERIFY_RETRIES = 3
VERIFY_RETRY_BASE_SECONDS = 0.5
LEVER_PAGE_SIZE = 100
LEVER_MAX_PAGES = 20
SMARTRECRUITERS_PAGE_SIZE = 100
SMARTRECRUITERS_MAX_PAGES = 20
GREENHOUSE_DETAIL_MAX_REQUESTS = 200
PRELAW_DETAIL_MAX_REQUESTS = 60
PUBLIC_BOARD_GRACE_HOURS = 6
ADZUNA_RESULTS_PER_PAGE = 50
ADZUNA_REQUESTS_PER_ACTIVE_RUN = 5
ADZUNA_REFRESH_INTERVAL_HOURS = 2
ADZUNA_GRACE_HOURS = 6
ADZUNA_WHERE = "Canada"
ADZUNA_DISTANCE_KM = None
ADZUNA_COVERAGE_TARGET = "ca/Canada"
ADZUNA_QUERY_NAMES = (
    "internship-p1", "internship-p2",
    "coop-p1", "coop-p2",
    "new-grad-p1",
)
AMAZON_QUERIES = (
    "intern finance",
    "intern marketing",
    "intern operations",
    "intern business",
    "software development engineer intern",
    "applied science intern",
)

COVERAGE = {}
COVERAGE_LOCK = Lock()
FILTER_FUNNEL = {}


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
    "Software Engineering": r"(?:\bsoftware\b|\blogiciels?\b|\b(?:swe|sde)\b|"
                            r"\bsystems?\s+development\s+engineer(?:ing)?\b|"
                            r"\b(?:back[\s-]?end|front[\s-]?end|full[\s-]?stack)\b|"
                            r"\b(?:developers?|d[ée]veloppeu(?:r|se)s?|"
                            r"programmeu(?:r|se)s?|programmers?)\b)",
    "Data Science & AI": r"\bdata\s+(?:science|scientist|engineering|engineer|"
                         r"analytics?|analyst|platform|quality|governance|management)\b|"
                         r"\bdata\s*(?:&|and)\s*reporting\b|"
                         r"\b(?:analytics?|business intelligence|artificial intelligence|"
                         r"intelligence artificielle|machine learning|"
                         r"apprentissage (?:automatique|machine)|applied science|"
                         r"applied research|predictive insights|computer vision|"
                         r"genai|nlp|donn[ée]es)\b|\b(?:ai|ml)\b|"
                         r"\b(?:quantitative|financial)\s+(?:engineering|research|"
                         r"developer|analytics?)\b",
    "Cybersecurity": r"\b(?:cyber(?:security)?|information security|"
                     r"application security|product security|browser security|"
                     r"cloud security|network security|security engineer|"
                     r"security analyst|threat(?: detection| hunting)?|soc analyst|"
                     r"identity and access|iam|cybers[ée]curit[ée])\b",
    "IT & Infrastructure": r"\b(?:devops|site reliability|sre|it support|"
                           r"information technology|information systems|network services|"
                           r"network engineer|networking|systems? administrator|"
                           r"telecom(?:munications)?|data cent(?:er|re)|"
                           r"business systems engineer|technology systems|cloud|"
                           r"infrastructure engineer|platform engineer|mainframe|"
                           r"infonuagique|informatique|r[ée]seaux?)\b",
    "Quality & Test Engineering": r"\b(?:qa|sdet|quality assurance|"
                                  r"quality engineer(?:ing)?|test engineer(?:ing)?|"
                                  r"test automation|software test(?:ing)?|"
                                  r"design validation|hardware validation|ic validation|"
                                  r"validation engineer(?:ing)?|verification(?: and validation)?|"
                                  r"v&v|systems? integration.*(?:test|verification|validation)|"
                                  r"product development test laboratory|assurance qualit[ée]|"
                                  r"automatisation des tests)\b",
    "Hardware & Embedded Systems": r"\b(?:hardware|embedded|firmware|fpga|asic|soc|"
                                   r"silicon|pcb|pcba|microelectronics|"
                                   r"analog (?:design|engineer|layout)|signal integrity|"
                                   r"photonics|modem hw|computer engineering|ic validation|"
                                   r"ams layout|layout engineer|"
                                   r"logiciel embarqu[ée])\b",
    "Electrical & Electronics Engineering": r"\b(?:electrical|electronics?|"
                                          r"power systems?|power engineering|energy engineering|"
                                          r"grid|transmission|distribution (?:engineering|"
                                          r"planning|projects?)|controls? engineer|"
                                          r"control systems?|instrumentation|electromechanical|"
                                          r"circuit|analog (?:design|engineer)|ams layout|"
                                          r"layout engineer|signal integrity|"
                                          r"photonics|[ée]lectrique|[ée]lectronique|"
                                          r"[ée]lectrom[ée]canique|mechatronics?|"
                                          r"m[ée]catronique)\b",
    "Mechanical, Mechatronics & Robotics": r"\b(?:mechanical|mechatronics?|robotics?|"
                                          r"electromechanical|thermal|fluid(?:s| dynamics)?|"
                                          r"hvac|controls? engineer|m[ée]canique|"
                                          r"m[ée]catronique|robotique|[ée]lectrom[ée]canique)\b",
    "Civil & Structural Engineering": r"\b(?:civil|structural|transportation engineering|"
                                      r"tunnell\w*|underground infrastructure|"
                                      r"construction(?: engineering)?|geotechnical|"
                                      r"municipal engineering|rail engineering|"
                                      r"transit and rail|water resources|g[ée]nie civil)\b",
    "Environmental Engineering": r"\b(?:environmental|"
                                 r"environment,?\s+health\s*(?:&|and)\s+safety|"
                                 r"sustainab(?:ility|le)|"
                                 r"forestry operations|waste management|ecolog\w*|"
                                 r"environnemental)\b",
    "Chemical, Process & Materials Engineering": r"\b(?:chemical|process engineer(?:ing)?|"
                                               r"materials? (?:science|engineering|innovation)|"
                                               r"metallurg\w*|chemistry|polymer|battery engineering|"
                                               r"extractive metallurgy|chimique|proc[ée]d[ée]s?|"
                                               r"mat[ée]riaux)\b",
    "Industrial & Manufacturing Engineering": r"\b(?:industrial(?: engineering|ization)?|"
                                             r"manufactur\w*|production engineering|"
                                             r"factory automation|continuous improvement|"
                                             r"operations engineering|warehouse automation|"
                                             r"industriel|manufacturier|fabrication|"
                                             r"am[ée]lioration continue)\b",
    "Aerospace Engineering": r"\b(?:aerospace|aeronaut\w*|avionics?|aircraft|"
                             r"flight (?:systems?|software|controls?|simulator)|"
                             r"propulsion|satellite|spacecraft|a[ée]rospatial|"
                             r"a[ée]ronautique|avionique)\b",
    "Biomedical Engineering": r"\b(?:biomedical|bioengineering|medical devices?|"
                              r"clinical engineering|biom[ée]dical)\b",
    "Mining & Geological Engineering": r"\b(?:mining|geological|geoscience|mineral|"
                                       r"mine engineering|extractive metallurgy|minier|"
                                       r"min[ée]ral|g[ée]ologique)\b",
    "General Engineering": r"\b(?:engineer(?:ing)?|technologist|ing[ée]nieur|"
                           r"ing[ée]nierie|g[ée]nie|systems? integration|"
                           r"system of systems|nuclear)\b",
}
CATEGORIES = {k: re.compile(v, re.I) for k, v in CATEGORIES.items()}
CATEGORIES[LEGAL_CATEGORY] = re.compile(r"(?!x)x")
CATEGORIES[POLICY_CATEGORY] = re.compile(r"(?!x)x")
BUSINESS_CATEGORY_NAMES = (
    "Finance",
    "Marketing",
    "Business Operations",
    "Consulting",
)
SOFTWARE_ENGINEERING_TRACK = "Software Engineering"
SOFTWARE_ENGINEERING_RE = CATEGORIES[SOFTWARE_ENGINEERING_TRACK]
QUALITY_TRACK = "Quality & Test Engineering"
GENERAL_ENGINEERING_TRACK = "General Engineering"
TECHNICAL_CATEGORY_NAMES = (
    SOFTWARE_ENGINEERING_TRACK,
    "Data Science & AI",
    "Cybersecurity",
    "IT & Infrastructure",
    QUALITY_TRACK,
    "Hardware & Embedded Systems",
    "Electrical & Electronics Engineering",
    "Mechanical, Mechatronics & Robotics",
    "Civil & Structural Engineering",
    "Environmental Engineering",
    "Chemical, Process & Materials Engineering",
    "Industrial & Manufacturing Engineering",
    "Aerospace Engineering",
    "Biomedical Engineering",
    "Mining & Geological Engineering",
    GENERAL_ENGINEERING_TRACK,
)
BASE_TECHNICAL_CATEGORY_NAMES = set(TECHNICAL_CATEGORY_NAMES) - {
    QUALITY_TRACK,
    GENERAL_ENGINEERING_TRACK,
}
NON_SOFTWARE_DEVELOPER_RE = re.compile(
    r"\b(?:business|sales|partnerships?|real\s+estate|land|community|talent|"
    r"learning|curriculum|content|product)\s+(?:developers?|"
    r"d[ée]veloppeu(?:r|se)s?|programmeu(?:r|se)s?|programmers?)\b|"
    r"\b(?:developers?|d[ée]veloppeu(?:r|se)s?)\s+"
    r"(?:relations|advocacy|experience|marketing)\b",
    re.I,
)
NON_ENGINEERING_SOFTWARE_RE = re.compile(
    r"\bsoftware\s+(?:sales|marketing|product\s+marketing|account\s+management|"
    r"customer\s+success|product\s+management|business\s+development)\b|"
    r"\b(?:sales|marketing|product\s+marketing|account\s+management|"
    r"customer\s+success|product\s+management|business\s+development)\b"
    r".*\bsoftware\b",
    re.I,
)
STRONG_SOFTWARE_ROLE_RE = re.compile(
    r"\b(?:software|logiciels?)\b.*\b(?:engineer\w*|developers?|development|"
    r"d[ée]veloppeu(?:r|se)s?)\b|"
    r"\b(?:engineer\w*|developers?|development|d[ée]veloppeu(?:r|se)s?)\b"
    r".*\b(?:software|logiciels?)\b|"
    r"\b(?:swe|sde)\b|"
    r"\bsystems?\s+development\s+engineer(?:ing)?\b|"
    r"\b(?:back[\s-]?end|front[\s-]?end|full[\s-]?stack)\b",
    re.I,
)
BUSINESS_REENGINEERING_RE = re.compile(
    r"\b(?:process|business|finance|cfo)\s+re-?engineering\b",
    re.I,
)
NON_TEST_QUALITY_RE = re.compile(r"\bdata quality\b|\bquality coach\b", re.I)

# Technical roles must never be classified into a business track just because a
# keyword like "brand" or "media" appears in the title.
EXCLUDE_RE = re.compile(
    r"\b(engineer\w*|developer|swe|backend|front[\s-]?end|fullstack|full[\s-]?stack"
    r"|scientist|machine\s+learning|ml|data\s+engineer|devops|sre|infrastructure"
    r"|designer|ux|ui|security\s+engineer|qa|test|firmware|hardware|silicon)\b", re.I)

INTERN_RE = re.compile(
    r"\b(intern|interns|internship|internships|co-?op|co-?ops|new\s?grad|"
    r"new\s?graduate|students?|stagiaires?|[eé]tudiants?|[eé]tudiantes?)\b", re.I)

# "Campus Talent Acquisition Senior Associate" matched on 'campus' - it is a
# recruiter job, not an internship. Reject seniority markers unless the title
# also explicitly says intern/co-op.
SENIOR_RE = re.compile(r"\b(senior|sr\.?|manager|director|principal|lead|vp"
                       r"|head\s+of|associate\s+partner|executive)\b", re.I)
EXPLICIT_RE = re.compile(r"\b(intern|internship|co-?op)\b", re.I)
NEW_GRAD_TITLE_RE = re.compile(r"\bnew\s*grad(?:uate)?\b", re.I)
NEW_GRAD_SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|director|principal|lead|vp|head\s+of|executive)\b", re.I)


def is_internship(title):
    t = title or ""
    if not INTERN_RE.search(t):
        return False
    if NEW_GRAD_TITLE_RE.search(t):
        return not NEW_GRAD_SENIOR_RE.search(t)
    if SENIOR_RE.search(t) and not EXPLICIT_RE.search(t):
        return False
    return True

CANADIAN_REGIONS = {
    "alberta": "Alberta", "ab": "Alberta",
    "british columbia": "British Columbia", "bc": "British Columbia",
    "manitoba": "Manitoba", "mb": "Manitoba",
    "new brunswick": "New Brunswick", "nb": "New Brunswick",
    "newfoundland and labrador": "Newfoundland and Labrador",
    "newfoundland": "Newfoundland and Labrador", "labrador": "Newfoundland and Labrador",
    "nl": "Newfoundland and Labrador",
    "nova scotia": "Nova Scotia", "ns": "Nova Scotia",
    "ontario": "Ontario", "on": "Ontario",
    "prince edward island": "Prince Edward Island", "pei": "Prince Edward Island",
    "pe": "Prince Edward Island",
    "quebec": "Quebec", "québec": "Quebec", "qc": "Quebec", "pq": "Quebec",
    "saskatchewan": "Saskatchewan", "sk": "Saskatchewan",
    "northwest territories": "Northwest Territories", "nt": "Northwest Territories",
    "nunavut": "Nunavut", "nu": "Nunavut",
    "yukon": "Yukon", "yt": "Yukon",
}
CANADIAN_CITIES = {
    "toronto": "Ontario", "mississauga": "Ontario", "brampton": "Ontario",
    "markham": "Ontario", "vaughan": "Ontario", "richmond hill": "Ontario",
    "scarborough": "Ontario", "etobicoke": "Ontario", "north york": "Ontario",
    "oakville": "Ontario", "ajax": "Ontario",
    "pickering": "Ontario", "whitby": "Ontario", "oshawa": "Ontario",
    "milton": "Ontario", "waterloo": "Ontario",
    "kitchener": "Ontario", "ottawa": "Ontario", "hamilton": "Ontario",
    "guelph": "Ontario", "windsor": "Ontario", "barrie": "Ontario",
    "peterborough": "Ontario", "thornhill": "Ontario",
    "montreal": "Quebec", "montréal": "Quebec", "quebec city": "Quebec",
    "québec city": "Quebec", "laval": "Quebec", "gatineau": "Quebec",
    "vancouver": "British Columbia", "surrey": "British Columbia",
    "burnaby": "British Columbia",
    "victoria": "British Columbia", "north vancouver": "British Columbia",
    "new westminster": "British Columbia", "kelowna": "British Columbia",
    "calgary": "Alberta", "edmonton": "Alberta", "red deer": "Alberta",
    "winnipeg": "Manitoba", "halifax": "Nova Scotia",
    "fredericton": "New Brunswick", "moncton": "New Brunswick",
    "saint john": "New Brunswick", "st. john's": "Newfoundland and Labrador",
    "regina": "Saskatchewan", "saskatoon": "Saskatchewan",
    "charlottetown": "Prince Edward Island", "whitehorse": "Yukon",
    "yellowknife": "Northwest Territories", "iqaluit": "Nunavut",
}
US_LOCATION_RE = re.compile(
    r"\b(united states(?: of america)?|u\.?s\.?a?\.?|california|new york|texas|"
    r"florida|washington|massachusetts|illinois|virginia|colorado|ohio|"
    r"michigan|georgia|pennsylvania|new jersey|north carolina|south carolina)\b|"
    r",\s*(?:ca|ny|tx|fl|wa|ma|il|va|co|oh|mi|ga|pa|nj|nc|sc)\b", re.I)
FOREIGN_COUNTRY_RE = re.compile(
    r"\b(australia|india|singapore|united kingdom|england|scotland|wales|"
    r"ireland|germany|france|spain|italy|netherlands|poland|switzerland|"
    r"austria|belgium|denmark|sweden|norway|finland|portugal|czechia|"
    r"czech republic|romania|hungary|greece|turkey|israel|"
    r"united arab emirates|uae|saudi arabia|south africa|brazil|argentina|"
    r"chile|colombia|peru|mexico|japan|china|hong kong|taiwan|"
    r"south korea|korea|new zealand|philippines|malaysia|indonesia|"
    r"thailand|vietnam)\b",
    re.I,
)
EXPLICIT_CANADA_REMOTE_RE = re.compile(
    r"(?:\bremote\s*[-,(]?\s*(?:in\s+)?canada\b|\bcanada\s*[-,(]?\s*remote\b|"
    r"remote\s*\((?:us\s*\+\s*)?canada(?:\s+only)?\)|"
    r"\bremote\s+(?:us\s*\+\s*canada|canada\s*\+\s*us)\b)", re.I)
FACET_LOCATION_TERMS = tuple(CANADIAN_REGIONS) + tuple(CANADIAN_CITIES) + ("canada",)


def geo(loc):
    text = clean_text(loc)
    lowered = text.casefold()
    if not lowered:
        return None
    if EXPLICIT_CANADA_REMOTE_RE.search(text):
        return "Remote - Canada"
    if (
        (US_LOCATION_RE.search(text) or FOREIGN_COUNTRY_RE.search(text))
        and not re.search(r"\bcanada\b", lowered)
    ):
        return None
    if re.search(r"\bcanada\b", lowered):
        for name, region in CANADIAN_REGIONS.items():
            if len(name) > 2 and re.search(rf"\b{re.escape(name)}\b", lowered):
                return region
        for city, region in CANADIAN_CITIES.items():
            if re.search(rf"\b{re.escape(city)}\b", lowered):
                return region
        return "Canada"
    for city, region in CANADIAN_CITIES.items():
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            return region
    for name, region in CANADIAN_REGIONS.items():
        if len(name) > 2 and re.search(rf"\b{re.escape(name)}\b", lowered):
            return region
        if len(name) == 2 and re.search(rf"(?:,|\()\s*{re.escape(name)}(?:\b|,|\))", lowered):
            return region
    return None


def categorize(title, content=""):
    t = title or ""
    software_role = (
        SOFTWARE_ENGINEERING_RE.search(t)
        and not NON_SOFTWARE_DEVELOPER_RE.search(t)
        and not (
            NON_ENGINEERING_SOFTWARE_RE.search(t)
            and not STRONG_SOFTWARE_ROLE_RE.search(t)
        )
    )
    selected = set()
    if not software_role and not EXCLUDE_RE.search(t):
        selected.update(
            category for category in BUSINESS_CATEGORY_NAMES
            if CATEGORIES[category].search(t)
        )
    if software_role:
        selected.add(SOFTWARE_ENGINEERING_TRACK)
    for category in TECHNICAL_CATEGORY_NAMES:
        if category in {SOFTWARE_ENGINEERING_TRACK, GENERAL_ENGINEERING_TRACK}:
            continue
        if (
            category == QUALITY_TRACK
            and NON_TEST_QUALITY_RE.search(t)
        ):
            continue
        if CATEGORIES[category].search(t):
            selected.add(category)
    if (
        CATEGORIES[GENERAL_ENGINEERING_TRACK].search(t)
        and not BUSINESS_REENGINEERING_RE.search(t)
        and not any(
            category in selected for category in BASE_TECHNICAL_CATEGORY_NAMES
        )
    ):
        selected.add(GENERAL_ENGINEERING_TRACK)
    selected.update(classify_prelaw(t, content))
    return [category for category in CATEGORIES if category in selected]


class _SourceHTTPAdapter(requests.adapters.HTTPAdapter):
    def _send_complete(self, request, **kwargs):
        with SOURCE_REQUESTS:
            response = super().send(request, **kwargs)
            if not kwargs.get("stream", False):
                # Session would otherwise read the body after releasing this slot.
                response.content
            return response

    def send(self, request, **kwargs):
        host = urlsplit(request.url).hostname
        interval = SOURCE_HOST_MIN_INTERVALS.get(host)
        if interval is None:
            return self._send_complete(request, **kwargs)
        with SOURCE_HOST_LOCKS[host]:
            while True:
                delay = SOURCE_HOST_NEXT_REQUEST_AT[host] - time.monotonic()
                if delay <= 0:
                    break
                time.sleep(delay)
            try:
                return self._send_complete(request, **kwargs)
            finally:
                SOURCE_HOST_NEXT_REQUEST_AT[host] = time.monotonic() + interval


def _source_get(url, **kwargs):
    # Session dispatch also paces each redirect, not just the initial URL.
    with requests.Session() as session:
        for scheme in ("http://", "https://"):
            session.mount(scheme, _SourceHTTPAdapter())
        return session.get(url, **kwargs)


def g(url, request_budget=None, **kw):
    last_response = None
    last_error = None
    for attempt in range(SOURCE_RETRIES):
        if request_budget is not None:
            if request_budget["remaining"] <= 0:
                raise SourceRequestBudgetExceeded("request budget exhausted")
            request_budget["remaining"] -= 1
        try:
            response = _source_get(url, timeout=TIMEOUT, headers=UA, **kw)
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
        FILTER_FUNNEL.clear()


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
    return bool(text and geo(text))


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

# Verified public boards; evidence and addition procedure are in README.md.
PUBLIC_BOARDS = [
    ("greenhouse", "internshiplist2000", "Geotab"),
    ("greenhouse", "coveoen", "Coveo"),
    ("greenhouse", "momentenergy", "Moment Energy"),
    ("greenhouse", "cresta", "Cresta"),
    ("greenhouse", "rocketlab", "Rocket Lab"),
    ("greenhouse", "aperaaiinc", "Apera AI"),
    ("greenhouse", "spaceium", "Spaceium"),
    ("greenhouse", "tenstorrentuniversity", "Tenstorrent"),
    ("greenhouse", "doordashcanada", "DoorDash"),
    ("greenhouse", "drweng", "DRW"),
    ("greenhouse", "orennia", "Orennia"),
    ("greenhouse", "procogia", "Procogia"),
    ("greenhouse", "stackadapt", "StackAdapt"),
    ("greenhouse", "towerresearchcapital", "Tower Research Capital"),
    ("greenhouse", "bestpass", "Drivewyze"),
    ("greenhouse", "brave", "Brave"),
    ("greenhouse", "captiveaire", "CaptiveAire"),
    ("greenhouse", "globalrelay", "Global Relay"),
    ("greenhouse", "instalilyai", "InstaLILY AI"),
    ("greenhouse", "kao", "Kao"),
    ("greenhouse", "khanacademy", "Khan Academy"),
    ("greenhouse", "later", "Later"),
    ("greenhouse", "openfarminc", "Open Farm"),
    ("greenhouse", "rivaltechnologies", "Rival Technologies"),
    ("greenhouse", "sonyinteractiveentertainmentglobal", "Sony Interactive Entertainment"),
    ("greenhouse", "stepstone", "StepStone Group"),
    ("greenhouse", "thebrattlegroup", "The Brattle Group"),
    ("greenhouse", "tribalscale", "TribalScale"),
    ("ashby", "human-computer-lab", "Human Computer Lab"),
    ("ashby", "bree", "Bree"),
    ("ashby", "trulioo-coopandinterns", "Trulioo"),
    ("ashby", "zip", "Zip"),
    ("ashby", "Superhuman Platform Inc", "Superhuman"),
    ("ashby", "eastwoodcleef", "Eastwood & Cleef"),
    ("ashby", "exegy", "Exegy"),
    ("ashby", "fellowai", "Fellow"),
    ("ashby", "georgian", "Georgian"),
    ("ashby", "remarcable-inc", "Remarcable"),
    ("ashby", "rivianvw.tech", "Rivian and Volkswagen Group Technologies"),
    ("ashby", "veeda-ai", "Veeda AI"),
    ("ashby", "viggle", "Viggle"),
    ("ashby", "yotta", "Yotta"),
    ("ashby", "quora", "Quora"),
    ("ashby", "uncountable", "Uncountable"),
    ("ashby", "wealthsimple", "Wealthsimple"),
    ("lever", "eqbank", "EQ Bank"),
    ("lever", "arcteryx.com", "Arc'teryx"),
    ("lever", "acceldata", "Acceldata"),
    ("lever", "gomaterials", "GoMaterials"),
    ("lever", "thinkingbox", "Thinkingbox"),
    ("lever", "extremenetworks", "Extreme Networks"),
    ("lever", "fullscript", "Fullscript"),
    ("lever", "geocomply-2", "GeoComply"),
    ("lever", "kepler", "Kepler Communications"),
    ("lever", "shyftlabs", "ShyftLabs"),
    ("lever", "univeris.com", "Univeris"),
    ("lever", "waremalcomb", "Ware Malcomb"),
    ("lever", "waabi", "Waabi"),
    ("lever", "pointclickcare", "PointClickCare"),
    ("lever", "caseware", "Caseware"),
    ("lever", "benchsci", "BenchSci"),
    ("smartrecruiters", "gdmsi", "General Dynamics Mission Systems Canada"),
    ("smartrecruiters", "veoliaenvironnementsa", "Veolia"),
    ("smartrecruiters", "syngentagroup", "Syngenta"),
    ("smartrecruiters", "aecom2", "AECOM"),
    ("smartrecruiters", "crb", "CRB"),
    ("smartrecruiters", "boschgroup", "Bosch"),
    ("smartrecruiters", "houseofcommonscanadachambredescommunescanada",
     "House of Commons of Canada"),
    ("smartrecruiters", "beumergroup1", "BEUMER Group"),
    ("smartrecruiters", "cima2", "CIMA+"),
    ("smartrecruiters", "groupecanam", "Canam"),
    ("smartrecruiters", "groupemontoni", "MONTONI"),
    ("smartrecruiters", "videotron", "Videotron"),
    ("smartrecruiters", "louisdreyfuscompany", "Louis Dreyfus Company"),
    ("smartrecruiters", "qsl", "QSL"),
    ("smartrecruiters", "sgs", "SGS"),
    ("smartrecruiters", "torstar", "Torstar"),
]
PUBLIC_BOARD_KEYS = {coverage_key(source, board) for source, board, _ in PUBLIC_BOARDS}
PUBLIC_GREENHOUSE_BOARDS = {
    board for source, board, _ in PUBLIC_BOARDS if source == "greenhouse"
}
PUBLIC_INTERNSHIP_RE = re.compile(
    r"\b(interns?|internships?|co-?ops?|students?|stagiaires?|"
    r"[eé]tudiants?|[eé]tudiantes?)\b", re.I)
TALENT_POOL_RE = re.compile(
    r"\b(talent (?:pool|community|network)|general (?:application|interest)|"
    r"expression of interest|future opportunities|register your interest)\b", re.I)

# tenant, site, host  - only combos proven to resolve, plus business-heavy tries
WORKDAY = [
    ("cibc", "search", "wd3"),
    ("cibc", "Campus", "wd3"),
    ("rbc", "RBCEarlyTalent1", "wd3"),
    ("td", "TD_Bank_Careers", "wd3"), ("scotiabank", "Scotiabank_Careers", "wd3"),
    ("bmo", "External", "wd3"),
    ("manulife", "MFCJH_Jobs", "wd3"), ("sunlife", "Campus", "wd3"),
    ("telus", "TELUS_Careers", "wd3"), ("loblaw", "LCLCareers", "wd3"),
    ("rogers", "rogers-jobs", "wd3"), ("omers", "OMERS_External", "wd3"),
    ("pwc", "Global_Experienced_Careers", "wd3"),
    # Accenture is on wd103, not wd3 - wd3 returns HTTP 422 and silently
    # contributed nothing. Confirmed from accenture.com/ca-en/careers.
    ("accenture", "AccentureCareers", "wd103"),
    ("deloittecanada", "External", "wd3"), ("kpmg", "External", "wd3"),
    ("cpp", "CPPIB_Careers", "wd3"),
    ("otppb", "OntarioTeachers_Careers", "wd3"),
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
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("invalid jobs response")
        jobs = data["jobs"]
        total = (data.get("meta") or {}).get("total", len(jobs))
        if total != len(jobs):
            raise ValueError("incomplete jobs response")
        out = []
        for j in jobs:
            if not isinstance(j, dict) or not source_id(j, "id"):
                raise ValueError("job missing provider ID")
            if "internal_job_id" not in j:
                raise ValueError("job missing prospect status")
            if j["internal_job_id"] is None:
                continue
            if not j.get("title") or not j.get("absolute_url"):
                raise ValueError("job missing title or URL")
            out.append(with_coverage(
                dict(company=tok, src="greenhouse", title=j.get("title", ""),
                     loc=(j.get("location") or {}).get("name", ""),
                     url=canonicalize_url(j["absolute_url"]),
                     external_id=source_id(j, "id"),
                     _greenhouse_board=tok,
                     application_deadline=j.get("application_deadline"),
                     posted=j.get("first_published", "") or "",
                     content=clean_text(
                         html_text(j.get("content", "") or "")
                     )[:12000]),
                key))
        set_coverage(key, source="greenhouse", target=tok, pages=1,
                     returned=len(jobs), records=len(out),
                     query_total=len(jobs), selected_total=len(jobs),
                     location_mode="provider", fallback_terms=0, capped_terms=0,
                     errors=[], complete=True)
        return out
    except (requests.RequestException, ValueError, TypeError) as exc:
        set_coverage(key, source="greenhouse", target=tok, pages=0,
                     returned=0, query_total=0, complete=False,
                     errors=[f"{type(exc).__name__}: {exc}"])
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
        jobs = d.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError("invalid jobs response")
        out = []
        for j in jobs:
            if not isinstance(j, dict):
                raise ValueError("invalid job record")
            if not isinstance(j.get("isListed"), bool):
                raise ValueError("job missing listed status")
            if j.get("isListed") is not True:
                continue
            if not source_id(j, "id") or not j.get("title") or not j.get("jobUrl"):
                raise ValueError("job missing ID, title or URL")
            locations = [ashby_location(j)]
            locations.extend(
                ashby_location(location)
                for location in (j.get("secondaryLocations") or [])
            )
            out.append(with_coverage(
                dict(company=tok, src="ashby", title=j["title"],
                     loc=public_location(locations) or locations[0],
                     locations=locations, url=canonicalize_url(j["jobUrl"]),
                     external_id=source_id(j, "id"),
                     posted=j.get("publishedAt", "") or "",
                     content=clean_text(j.get("descriptionPlain") or re.sub(
                         "<[^>]+>", " ", j.get("descriptionHtml", "") or ""))[:12000]),
                key))
        set_coverage(key, source="ashby", target=tok, pages=1,
                     returned=len(jobs), records=len(out),
                     query_total=len(jobs), selected_total=len(jobs),
                     location_mode="provider", fallback_terms=0, capped_terms=0,
                     errors=[], complete=True)
        return out
    except (requests.RequestException, ValueError, TypeError) as exc:
        set_coverage(key, source="ashby", target=tok, pages=0,
                     returned=0, query_total=0, complete=False,
                     errors=[f"{type(exc).__name__}: {exc}"])
        return []


def ashby_location(record):
    address = record.get("address") or {}
    address = address.get("postalAddress") or address
    country = clean_text(address.get("addressCountry")).casefold()
    location = clean_text(record.get("location"))
    lowered = location.casefold()
    if country and country not in {"ca", "can", "canada"}:
        if EXPLICIT_CANADA_REMOTE_RE.search(location):
            return location
        for city, region in CANADIAN_CITIES.items():
            if re.search(rf"\b{re.escape(city)}\b", lowered):
                return f"{city.title()}, {region}, Canada"
        return ""
    if country in {"ca", "can", "canada"}:
        if geo(location):
            return location
        if location:
            return f"{location}, Canada"
        locality = clean_text(address.get("addressLocality"))
        region = clean_text(address.get("addressRegion"))
        return ", ".join(part for part in (locality, region, "Canada") if part)
    if geo(location):
        return location
    for city, region in CANADIAN_CITIES.items():
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            return f"{city.title()}, {region}, Canada"
    return ""


def public_location(locations):
    for location in locations:
        for part in clean_text(location).split(";"):
            part = part.strip()
            if geo(part):
                return part
    return ""


def public_board_url_matches(source, board, url, external_id):
    parsed = urlsplit(canonicalize_url(url))
    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    if source == "lever":
        return (
            parsed.hostname == "jobs.lever.co"
            and bool(path_parts)
            and path_parts[0].casefold() == board.casefold()
        )
    if source == "ashby":
        return (
            parsed.hostname == "jobs.ashbyhq.com"
            and bool(path_parts)
            and path_parts[0].casefold() == board.casefold()
        )
    if source == "smartrecruiters":
        return (
            parsed.hostname == "jobs.smartrecruiters.com"
            and len(path_parts) >= 2
            and path_parts[0].casefold() == board.casefold()
            and path_parts[1] == str(external_id)
        )
    if source == "greenhouse":
        query = parse_qs(parsed.query)
        query_ids = (query.get("gh_jid") or []) + (query.get("job_id") or [])
        if str(external_id) in query_ids:
            return True
        if parsed.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
            if path_parts and path_parts[0].casefold() == board.casefold():
                return True
        return any(
            part == str(external_id)
            for part in path_parts[-2:]
        )
    return False


def from_lever(tok):
    key = coverage_key("lever", tok)
    out, seen = [], set()
    pages = returned = 0
    errors = []
    exhausted = False
    capped = False
    try:
        for page in range(LEVER_MAX_PAGES):
            response = g(
                f"https://api.lever.co/v0/postings/{tok}",
                params={"mode": "json", "skip": page * LEVER_PAGE_SIZE,
                        "limit": LEVER_PAGE_SIZE},
            )
            pages += 1
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            jobs = response.json()
            if not isinstance(jobs, list):
                raise ValueError("invalid jobs response")
            returned += len(jobs)
            for job in jobs:
                if not isinstance(job, dict) or not source_id(job, "id"):
                    raise ValueError("job missing provider ID")
                external_id = source_id(job, "id")
                if external_id in seen:
                    raise ValueError("repeated posting during pagination")
                seen.add(external_id)
                if not job.get("text") or not job.get("hostedUrl"):
                    raise ValueError("job missing title or URL")
                categories = job.get("categories") or {}
                primary = categories.get("location") or ""
                locations = list(categories.get("allLocations") or [primary])
                country = clean_text(job.get("country")).upper()
                if country not in ("", "CA", "CAN", "CANADA"):
                    locations = [loc for loc in locations if loc != primary]
                eligible_location = public_location(locations)
                if not eligible_location and country in {"CA", "CAN", "CANADA"}:
                    eligible_location = (
                        "Remote - Canada"
                        if clean_text(job.get("workplaceType")).casefold() == "remote"
                        else (
                            f"{clean_text(primary)}, Canada"
                            if clean_text(primary) else "Canada"
                        )
                    )
                created = job.get("createdAt")
                posted = (
                    datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
                    if isinstance(created, (int, float)) else ""
                )
                out.append(with_coverage(
                    dict(company=tok, src="lever", title=job["text"],
                         loc=eligible_location, locations=locations,
                         url=canonicalize_url(job["hostedUrl"]),
                         external_id=external_id, posted=posted,
                         content=clean_text(job.get("descriptionPlain") or "")[:12000]),
                    key))
            if len(jobs) < LEVER_PAGE_SIZE:
                exhausted = True
                break
        if not exhausted:
            capped = True
            errors.append("pagination safety cap reached")
    except (requests.RequestException, ValueError, TypeError, OverflowError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    set_coverage(
        key, source="lever", target=tok, pages=pages, returned=returned,
        records=len(out), query_total=None, selected_total=None,
        location_mode="provider", capped_terms=int(capped),
        errors=errors, complete=exhausted and not errors,
    )
    return out


def from_smartrecruiters(tok):
    key = coverage_key("smartrecruiters", tok)
    out, seen = [], set()
    pages = returned = 0
    errors = []
    exhausted = False
    capped = False
    try:
        total = None
        for page in range(SMARTRECRUITERS_MAX_PAGES):
            response = g(
                f"https://api.smartrecruiters.com/v1/companies/{tok}/postings",
                params={
                    "limit": SMARTRECRUITERS_PAGE_SIZE,
                    "offset": page * SMARTRECRUITERS_PAGE_SIZE,
                    "country": "ca",
                },
            )
            pages += 1
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise ValueError("invalid postings response")
            if total is None:
                total = data.get("totalFound")
                if not isinstance(total, int) or total < 0:
                    raise ValueError("invalid postings total")
            postings = data["content"]
            returned += len(postings)
            for posting in postings:
                if not isinstance(posting, dict) or not source_id(posting, "id"):
                    raise ValueError("posting missing provider ID")
                external_id = source_id(posting, "id")
                if external_id in seen:
                    raise ValueError("repeated posting during pagination")
                seen.add(external_id)
                company = posting.get("company") or {}
                if clean_text(company.get("identifier")).casefold() != tok.casefold():
                    raise ValueError("company identifier mismatch")
                if posting.get("visibility") not in (None, "PUBLIC"):
                    continue
                title = clean_text(posting.get("name"))
                location = posting.get("location") or {}
                country = clean_text(location.get("country")).casefold()
                if country not in {"ca", "can", "canada"}:
                    continue
                full_location = clean_text(location.get("fullLocation"))
                if not full_location:
                    full_location = ", ".join(
                        part for part in (
                            clean_text(location.get("city")),
                            clean_text(location.get("region")),
                            "Canada",
                        ) if part
                    )
                if location.get("remote") and not geo(full_location):
                    full_location = "Remote - Canada"
                if not title or not full_location:
                    raise ValueError("posting missing title or location")
                out.append(with_coverage(
                    dict(
                        company=clean_text(company.get("name")) or tok,
                        src="smartrecruiters",
                        title=title,
                        loc=full_location,
                        locations=[full_location],
                        url=canonicalize_url(
                            f"https://jobs.smartrecruiters.com/"
                            f"{quote(company.get('identifier') or tok, safe='')}/"
                            f"{external_id}"
                        ),
                        external_id=external_id,
                        posted=posting.get("releasedDate", "") or "",
                        content="",
                    ),
                    key,
                ))
            if len(postings) < SMARTRECRUITERS_PAGE_SIZE or returned >= total:
                exhausted = True
                break
        if not exhausted:
            capped = True
            errors.append("pagination safety cap reached")
    except (requests.RequestException, ValueError, TypeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    set_coverage(
        key, source="smartrecruiters", target=tok, pages=pages,
        returned=returned, records=len(out), query_total=total,
        selected_total=total, location_mode="country=ca",
        capped_terms=int(capped), errors=errors,
        complete=exhausted and not errors,
    )
    return out


def from_public_board(spec, now=None):
    source, board, company = spec
    key = coverage_key(source, board)
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    adapter = {
        "greenhouse": from_greenhouse,
        "ashby": from_ashby,
        "lever": from_lever,
        "smartrecruiters": from_smartrecruiters,
    }
    rows = adapter[source](board)
    fetch_complete = COVERAGE.get(key, {}).get("complete", False)
    if source == "greenhouse" and fetch_complete:
        for row in rows:
            row["url"] = canonicalize_url(
                f"https://job-boards.greenhouse.io/{quote(board, safe='')}/jobs/"
                f"{row['external_id']}"
            )
    invalid_urls = [] if not fetch_complete else [
        row for row in rows
        if not public_board_url_matches(
            source, board, row.get("url"), row.get("external_id"))
    ]
    if invalid_urls:
        set_coverage(
            key, complete=False,
            errors=[f"tenant URL identity mismatch ({len(invalid_urls)} rows)"],
        )
        rows = []
    if any(
        row.get("application_deadline")
        and _parse_timestamp(row["application_deadline"]) is None
        for row in rows
    ):
        set_coverage(key, complete=False, errors=["invalid application deadline"])
    if COVERAGE[key]["complete"]:
        for row in rows:
            row["company"] = company
            row["external_id"] = f"{board}:{row['external_id']}"
            row["source_target"] = board
            row["last_seen"] = now.isoformat()
            row["loc"] = public_location(row.get("locations") or [row["loc"]])
        set_coverage(key, status="fresh", source_refreshed_at=now.isoformat(),
                     source_age_hours=0.0, reused=0)
        return [
            row for row in rows if not row.get("application_deadline")
            or _parse_timestamp(row["application_deadline"]) > now
        ]

    return reuse_last_good_source(key, source, board, now)


def reuse_last_good_source(key, source, target, now):
    print(f"COVERAGE WARNING {key}: incomplete fetch; checking last-good slice")
    baseline_path = os.environ.get("SOURCE_BASELINE_PATH", "live_jobs.json")
    try:
        with open(baseline_path, encoding="utf-8") as handle:
            baseline = json.load(handle)
        if not isinstance(baseline, dict) or not isinstance(baseline.get("jobs"), list):
            raise ValueError("invalid baseline snapshot")
        state = baseline.get("source_state") or {}
        if not isinstance(state, dict) or not all(
            isinstance(row, dict) for row in baseline["jobs"]
        ):
            raise ValueError("invalid baseline records")
        public_states = state.get("public_boards") or {}
        if not isinstance(public_states, dict) or not isinstance(
            public_states.get(key, {}), dict
        ):
            raise ValueError("invalid source state")
        refreshed_at = _parse_timestamp(public_states.get(key, {}).get("refreshed_at"))
        age_hours = (
            (now - refreshed_at).total_seconds() / 3600 if refreshed_at else None
        )
        prior_rows = []
        for row in baseline["jobs"]:
            if row.get("src") != source or row.get("source_target") != target:
                continue
            carried = dict(row)
            carried["_carried_source_row"] = True
            prior_rows.append(with_coverage(carried, key))
        if any(
            row.get("application_deadline")
            and _parse_timestamp(row["application_deadline"]) is None
            for row in prior_rows
        ):
            raise ValueError("invalid cached application deadline")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"COVERAGE WARNING {key}: baseline unavailable ({type(exc).__name__})")
        prior_rows, refreshed_at, age_hours = [], None, None
    can_reuse = age_hours is not None and 0 <= age_hours <= PUBLIC_BOARD_GRACE_HOURS
    set_coverage(
        key, status="reused-incomplete" if can_reuse else "blocked",
        source_refreshed_at=refreshed_at.isoformat() if refreshed_at else None,
        source_age_hours=age_hours, reused=len(prior_rows) if can_reuse else 0,
    )
    if not can_reuse:
        return []
    return [
        row for row in prior_rows if not row.get("application_deadline")
        or _parse_timestamp(row["application_deadline"]) > now
    ]


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


def workday_location_from_path(external_path):
    decoded = clean_text(unquote(external_path))
    match = re.search(r"/job/([^/]+)/", decoded, re.I)
    if not match:
        return ""
    descriptor = clean_text(
        match.group(1).replace("_", " ").replace("-", " ")
    )
    region = geo(descriptor)
    if not region:
        return ""
    for city, city_region in sorted(
        CANADIAN_CITIES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if (
            city_region == region
            and re.search(rf"\b{re.escape(city)}\b", descriptor, re.I)
        ):
            return f"{city.title()}, {region}, Canada"
    return "Canada" if region == "Canada" else f"{region}, Canada"


GENERIC_WORKDAY_LOCATION_RE = re.compile(
    r"^(?:(?:\d+|multiple)\s+locations?|various locations?)$",
    re.I,
)


def workday_can_use_facet_location(location):
    text = clean_text(location)
    return not text or bool(GENERIC_WORKDAY_LOCATION_RE.fullmatch(text))


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
                if not location:
                    location = workday_location_from_path(path)
                if (
                    location_filter
                    and not geo(location)
                    and workday_can_use_facet_location(location)
                ):
                    location = (
                        f"{location or 'Multiple locations'} "
                        "(approved Canada location)"
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
    for q in AMAZON_QUERIES:
        for off in (0, 100):
            try:
                r = g("https://www.amazon.jobs/en/search.json?radius=100km"
                      f"&offset={off}&result_limit=100&sort=recent&base_query={q.replace(' ','%20')}"
                      "&loc_query=Canada&country=CAN")
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
        {
            "name": "internship-p1", "page": 1, "last_page": False,
            "params": {"title_only": "internship"},
        },
        {
            "name": "internship-p2", "page": 2, "last_page": True,
            "params": {"title_only": "internship"},
        },
        {
            "name": "coop-p1", "page": 1, "last_page": False,
            "params": {"what_or": "co-op coop"},
        },
        {
            "name": "coop-p2", "page": 2, "last_page": True,
            "params": {"what_or": "co-op coop"},
        },
        {
            "name": "new-grad-p1", "page": 1, "last_page": True,
            "params": {"what_phrase": "new grad"},
        },
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
    refreshed_at = _parse_timestamp(state.get("refreshed_at"))
    if refreshed_at and refreshed_at > now:
        refreshed_at = None
    if rows and refreshed_at is None:
        print(
            f"COVERAGE WARNING adzuna:{ADZUNA_COVERAGE_TARGET}: missing or invalid successful "
            "refresh timestamp; cached rows cannot be reused"
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
    key = coverage_key("adzuna", ADZUNA_COVERAGE_TARGET)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    baseline = load_adzuna_baseline(now)
    baseline_fresh = (
        baseline["age_hours"] is not None
        and baseline["age_hours"] <= ADZUNA_GRACE_HOURS
    )
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        if baseline_fresh:
            rows = _adzuna_carried_rows(baseline["rows"], key)
            set_coverage(
                key, source="adzuna", target=ADZUNA_COVERAGE_TARGET,
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
                key, source="adzuna", target=ADZUNA_COVERAGE_TARGET, status="expired",
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
            key, source="adzuna", target=ADZUNA_COVERAGE_TARGET, status="skipped",
            pages=0, returned=0, records=0, query_total=None,
            selected_total=None, location_mode="where+distance",
            fallback_terms=0, capped_terms=0, repeated_terms=0,
            errors=[], complete=True)
        return []

    force_refresh = _truthy_env("ADZUNA_FORCE_REFRESH")
    refresh_due = (
        force_refresh
        or baseline["age_hours"] is None
        or baseline["age_hours"] >= ADZUNA_REFRESH_INTERVAL_HOURS
    )
    if not refresh_due:
        rows = _adzuna_carried_rows(baseline["rows"], key)
        set_coverage(
            key, source="adzuna", target=ADZUNA_COVERAGE_TARGET,
            status="reused-fresh", pages=0, request_attempts=0,
            returned=len(rows), records=len(rows), reused=len(rows),
            query_total=None, selected_total=None,
            location_mode="carry-forward", fallback_terms=0,
            capped_terms=0, repeated_terms=0, errors=[],
            source_refreshed_at=baseline["refreshed_at"].isoformat(),
            source_age_hours=baseline["age_hours"], complete=True)
        return rows

    out, seen = [], set()
    pages = returned = 0
    query_total = 0
    capped_terms = 0
    errors = []
    query_metrics = []
    request_budget = {"remaining": ADZUNA_REQUESTS_PER_ACTIVE_RUN}

    for query in adzuna_queries():
        url = f"https://api.adzuna.com/v1/api/jobs/ca/search/{query['page']}"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": ADZUNA_RESULTS_PER_PAGE,
            "where": ADZUNA_WHERE,
            "sort_by": "date",
            "content-type": "application/json",
            **query["params"],
        }
        if ADZUNA_DISTANCE_KM is not None:
            params["distance"] = ADZUNA_DISTANCE_KM
        query_metric = {
            "name": query["name"], "page": query["page"],
            "total": None, "returned": 0,
            "kept": 0, "rejected": 0, "capped": False, "status": "success",
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
            query["last_page"]
            and total > query["page"] * ADZUNA_RESULTS_PER_PAGE
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
            title = clean_text(
                re.sub("<[^>]+>", " ", str(result.get("title") or "")))
            if query["name"] == "new-grad-p1" and not NEW_GRAD_TITLE_RE.search(title):
                query_metric["rejected"] += 1
                continue
            seen.add(identity)
            query_metric["kept"] += 1
            company = clean_text(
                (result.get("company") or {}).get("display_name")
            ) or "Undisclosed employer"
            location = clean_text(
                (result.get("location") or {}).get("display_name")
            )
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
            key, source="adzuna", target=ADZUNA_COVERAGE_TARGET, status="fresh",
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
            key, source="adzuna", target=ADZUNA_COVERAGE_TARGET,
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
        key, source="adzuna", target=ADZUNA_COVERAGE_TARGET, status="expired",
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

RETAINED_SOURCES = [
    ("successfactors", ("bell", "https://jobs.bce.ca", {}), "Bell"),
    ("successfactors", ("hydroone", "https://jobs.hydroone.com", {}), "Hydro One"),
    ("successfactors", ("fortisbc", "https://careers.fortisbc.com", {}), "FortisBC"),
    ("successfactors", ("saskpower", "https://jobs.saskpower.com", {}), "SaskPower"),
    ("successfactors", ("hatch", "https://jobs.hatch.com", {}), "Hatch"),
    ("successfactors", ("aecon", "https://jobs.aecon.com", {}), "Aecon"),
    (
        "successfactors",
        ("kiewit", "https://kiewitcareers.kiewit.com", {}),
        "Kiewit",
    ),
    (
        "successfactors",
        ("cityoftoronto", "https://jobs.toronto.ca/jobsatcity", {}),
        "City of Toronto",
    ),
    (
        "successfactors",
        ("cityofmississauga", "https://jobs.mississauga.ca", {}),
        "City of Mississauga",
    ),
    (
        "successfactors",
        ("cityofottawa", "https://jobs-emplois.ottawa.ca/city-jobs", {}),
        "City of Ottawa",
    ),
    ("workday", ("bb", "Student", "wd3"), "BlackBerry"),
    ("workday", ("autodesk", "uni", "wd1"), "Autodesk"),
    ("workday", ("cae", "career", "wd3"), "CAE"),
    (
        "workday",
        ("canadiantirecorporation", "enterprise_external_careers_site", "wd3"),
        "Canadian Tire",
    ),
    ("workday", ("intactfc", "intactfc", "wd3"), "Intact Financial"),
    ("workday", ("cenovus", "careers", "wd3"), "Cenovus"),
    ("workday", ("tcenergy", "career_site_tc", "wd3"), "TC Energy"),
    (
        "workday",
        ("colliers", "colliers-external-career-site", "wd3"),
        "Colliers",
    ),
    (
        "workday",
        ("thomsonreuters", "external_career_site", "wd5"),
        "Thomson Reuters",
    ),
    ("workday", ("bmo", "Campus", "wd3"), "BMO"),
    ("workday", ("olg", "careers-students", "wd3"), "OLG"),
    ("workday", ("mastercard", "campus", "wd1"), "Mastercard"),
    (
        "workday",
        (
            "rockwellautomation",
            "external-rockwell-automation-early-careers",
            "wd1",
        ),
        "Rockwell Automation",
    ),
    ("workday", ("abb", "external_career_page", "wd3"), "ABB"),
    ("workday", ("ciena", "careers", "wd5"), "Ciena"),
    ("workday", ("cisco", "cisco_careers", "wd5"), "Cisco"),
    ("workday", ("generalmotors", "careers_gm", "wd5"), "General Motors"),
    ("workday", ("gevernova", "vernova_externalsite", "wd5"), "GE Vernova"),
    ("workday", ("intel", "external", "wd1"), "Intel"),
    ("workday", ("marvell", "marvellcareers", "wd1"), "Marvell"),
    (
        "workday",
        ("motorolasolutions", "careers", "wd5"),
        "Motorola Solutions",
    ),
    (
        "workday",
        ("nvidia", "nvidiaexternalcareersite", "wd5"),
        "NVIDIA",
    ),
]


def retained_source_target(source, adapter_spec):
    if source == "workday":
        tenant, site, host = adapter_spec
        return f"{tenant}.{host}/{site}"
    return adapter_spec[0]


def retained_source_key(source, adapter_spec):
    return coverage_key(source, retained_source_target(source, adapter_spec))


EXPANSION_SOURCE_KEYS = {
    retained_source_key(source, adapter_spec)
    for source, adapter_spec, _ in RETAINED_SOURCES
}
RETAINED_SOURCE_KEYS = PUBLIC_BOARD_KEYS | EXPANSION_SOURCE_KEYS
DAILY_PRELAW_SOURCES = (
    ("gcjobs", "public", collect_gc_jobs),
    ("oracle", "CNRL-Campus", collect_cnrl_oracle),
)
DAILY_SOURCE_KEYS = {
    coverage_key(source, target)
    for source, target, _ in DAILY_PRELAW_SOURCES
}


def retained_source_url_matches(source, adapter_spec, url):
    parsed = urlsplit(canonicalize_url(url))
    if source == "workday":
        tenant, site, host = adapter_spec
        return (
            parsed.hostname == f"{tenant}.{host}.myworkdayjobs.com"
            and f"/{site.casefold()}/job/" in parsed.path.casefold()
        )
    base = urlsplit(adapter_spec[1])
    return (
        parsed.hostname == base.hostname
        and bool(re.search(r"/(?:[A-Za-z0-9_-]+/)?job/", parsed.path))
    )


def from_retained_source(spec, now=None):
    source, adapter_spec, company = spec
    target = retained_source_target(source, adapter_spec)
    key = coverage_key(source, target)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    adapter = {
        "workday": from_workday,
        "successfactors": from_successfactors,
    }[source]
    rows = adapter(adapter_spec)
    fetch_complete = COVERAGE.get(key, {}).get("complete", False)
    invalid_urls = [] if not fetch_complete else [
        row for row in rows
        if not retained_source_url_matches(source, adapter_spec, row.get("url"))
    ]
    if invalid_urls:
        set_coverage(
            key,
            complete=False,
            errors=[f"source URL identity mismatch ({len(invalid_urls)} rows)"],
        )
        rows = []
    if COVERAGE[key]["complete"]:
        for row in rows:
            row["company"] = company
            row["source_target"] = target
            row["last_seen"] = now.isoformat()
        set_coverage(
            key,
            status="fresh",
            source_refreshed_at=now.isoformat(),
            source_age_hours=0.0,
            reused=0,
        )
        return rows
    return reuse_last_good_source(key, source, target, now)

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


def successfactors_location(href):
    decoded = clean_text(unquote(href)).casefold()
    words = re.sub(r"[^a-zà-ÿ]+", " ", decoded)
    for city, region in sorted(
        CANADIAN_CITIES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if re.search(rf"\b{re.escape(city)}\b", words):
            return f"{city.title()}, {region}, Canada"
    suffix = re.search(
        r"-(ab|bc|mb|nb|nl|ns|nt|nu|on|pe|qc|sk|yt)/\d+/?$",
        decoded,
        re.I,
    )
    if suffix:
        return f"{CANADIAN_REGIONS[suffix.group(1).casefold()]}, Canada"
    if "canada" in words:
        return "Canada"
    return ""


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
            params = {"q": term, "locationsearch": "Canada", "startrow": start}
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
                url = urljoin(base + "/", href)
                if url in seen:
                    continue
                seen.add(url); new += 1
                title = _unescape(title)
                # The listing page does not expose a machine-readable location
                # or date; the slug carries the city, which is enough to place
                # it. Date is left empty rather than invented.
                loc = successfactors_location(href)
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


def load_daily_source_baseline(source, target, now):
    baseline_path = os.environ.get("SOURCE_BASELINE_PATH", "live_jobs.json")
    try:
        with open(baseline_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        jobs = snapshot.get("jobs")
        states = (snapshot.get("source_state") or {}).get("daily_sources") or {}
        state = states.get(coverage_key(source, target)) or {}
        if (
            not isinstance(jobs, list)
            or not isinstance(states, dict)
            or not isinstance(state, dict)
        ):
            raise ValueError("invalid daily-source baseline")
        refreshed_at = _parse_timestamp(state.get("refreshed_at"))
        if refreshed_at and refreshed_at > now:
            refreshed_at = None
        rows = [
            dict(job) for job in jobs
            if (
                isinstance(job, dict)
                and job.get("src") == source
                and job.get("source_target") == target
            )
        ]
        if any(
            row.get("application_deadline")
            and _parse_timestamp(row["application_deadline"]) is None
            for row in rows
        ):
            raise ValueError("invalid cached application deadline")
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        return {"rows": [], "refreshed_at": None, "age_hours": None}
    age_hours = (
        (now - refreshed_at).total_seconds() / 3600
        if refreshed_at else None
    )
    return {
        "rows": rows,
        "refreshed_at": refreshed_at,
        "age_hours": age_hours,
    }


def _daily_carried_rows(rows, key, now):
    carried = []
    for row in rows:
        deadline = _parse_timestamp(row.get("application_deadline"))
        if deadline and deadline <= now:
            continue
        copy = dict(row)
        copy["_carried_source_row"] = True
        carried.append(with_coverage(copy, key))
    return carried


def from_daily_prelaw_source(spec, now=None):
    source, target, collector = spec
    key = coverage_key(source, target)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    baseline = load_daily_source_baseline(source, target, now)
    due = (
        _truthy_env("PRELAW_FORCE_REFRESH")
        or baseline["age_hours"] is None
        or baseline["age_hours"] >= DAILY_REFRESH_INTERVAL_HOURS
    )
    if not due:
        rows = _daily_carried_rows(baseline["rows"], key, now)
        set_coverage(
            key,
            source=source,
            target=target,
            query_total=None,
            selected_total=len(rows),
            pages=0,
            returned=len(rows),
            records=len(rows),
            complete=True,
            errors=[],
            status="reused-fresh",
            source_refreshed_at=baseline["refreshed_at"].isoformat(),
            source_age_hours=baseline["age_hours"],
            reused=len(rows),
        )
        return rows

    result = collector(now=now)
    if result["complete"]:
        rows = []
        for row in result["rows"]:
            row["source_target"] = target
            row["last_seen"] = now.isoformat()
            rows.append(with_coverage(row, key))
        set_coverage(
            key,
            source=source,
            target=target,
            query_total=result.get("query_total"),
            selected_total=len(rows),
            pages=result.get("pages", 0),
            returned=result.get("returned", 0),
            records=len(rows),
            details=result.get("details", 0),
            complete=True,
            errors=[],
            status="fresh",
            source_refreshed_at=now.isoformat(),
            source_age_hours=0.0,
            reused=0,
        )
        return rows

    can_reuse = (
        baseline["age_hours"] is not None
        and 0 <= baseline["age_hours"] <= DAILY_GRACE_HOURS
    )
    rows = (
        _daily_carried_rows(baseline["rows"], key, now)
        if can_reuse else []
    )
    set_coverage(
        key,
        source=source,
        target=target,
        query_total=result.get("query_total"),
        selected_total=len(rows),
        pages=result.get("pages", 0),
        returned=result.get("returned", 0),
        records=len(rows),
        details=result.get("details", 0),
        complete=False,
        errors=result.get("errors", []),
        status="reused-incomplete" if can_reuse else "blocked",
        source_refreshed_at=(
            baseline["refreshed_at"].isoformat()
            if baseline["refreshed_at"] else None
        ),
        source_age_hours=baseline["age_hours"],
        reused=len(rows),
    )
    return rows


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
    + [(from_retained_source, spec, retained_source_key(spec[0], spec[1]))
       for spec in RETAINED_SOURCES]
    + [(from_daily_prelaw_source, spec, coverage_key(spec[0], spec[1]))
       for spec in DAILY_PRELAW_SOURCES]
    + [(from_amazon, "amazon", coverage_key("amazon", "amazon"))]
    + [(from_adzuna, "adzuna", coverage_key("adzuna", ADZUNA_COVERAGE_TARGET))]
    + [(from_public_board, spec, coverage_key(spec[0], spec[1]))
       for spec in PUBLIC_BOARDS]
)


SOURCE_NAMES_PATH = Path(__file__).with_name("source_names.json")
SOURCE_NAME_ALIASES = {
    "Intact": "Intact Financial",
    "Ontario Lottery and Gaming": "OLG",
    "Canadian Natural": "Canadian Natural Resources",
    "FSWEP": "Government of Canada FSWEP",
    "CBC": "CBC/Radio-Canada",
    "MaRS": "MaRS Discovery District",
    "Government Job Bank": "Job Bank Canada",
    "LinkedIn": "LinkedIn Jobs",
    "Indeed": "Indeed Canada",
    "Glassdoor": "Glassdoor Canada",
    "ZipRecruiter": "ZipRecruiter Canada",
    "SuccessFactors": "SAP SuccessFactors",
    "cpp": "CPP Investments",
    "otpp": "Ontario Teachers' Pension Plan",
    "otppb": "Ontario Teachers' Pension Plan",
    "sunlife": "Sun Life",
    "deloittecanada": "Deloitte",
}


def load_source_names():
    with SOURCE_NAMES_PATH.open(encoding="utf-8") as handle:
        names = json.load(handle)
    if (
        not isinstance(names, list)
        or not names
        or any(
            not isinstance(name, str) or not name or name != clean_text(name)
            for name in names
        )
    ):
        raise ValueError("source_names.json must contain a nonempty list of names")
    if len(names) != len({name.casefold() for name in names}):
        raise ValueError("source_names.json contains duplicate names")
    return tuple(names)


SOURCE_NAMES = load_source_names()
_SOURCE_NAME_LOOKUP = {
    name.casefold(): name
    for name in (
        [company for _, _, company in PUBLIC_BOARDS]
        + [company for _, _, company in RETAINED_SOURCES]
        + list(SOURCE_NAMES)
    )
}
_SOURCE_ALIAS_LOOKUP = {
    alias.casefold(): name for alias, name in SOURCE_NAME_ALIASES.items()
}


def canonical_source_name(value):
    name = clean_text(value)
    # Exact aliases go first: MaRS is the district, not the employer Mars.
    name = SOURCE_NAME_ALIASES.get(name, name)
    return _SOURCE_NAME_LOOKUP.get(
        name.casefold(), _SOURCE_ALIAS_LOOKUP.get(name.casefold(), name)
    )


def source_name_inventory():
    inventory = {
        name.casefold(): {"name": name, "source_keys": []}
        for name in SOURCE_NAMES
    }
    for collector, spec, key in TASKS:
        if collector in (from_public_board, from_retained_source):
            name = spec[2]
        elif collector in (from_workday, from_successfactors):
            name = spec[0]
        elif collector is from_daily_prelaw_source:
            name = {
                "gcjobs": "Government of Canada Jobs",
                "oracle": ORACLE_COMPANY,
            }[spec[0]]
        elif collector in (from_greenhouse, from_ashby, from_amazon, from_adzuna):
            name = spec
        else:
            raise ValueError(f"No source inventory name for configured task {key}")
        name = canonical_source_name(name)
        entry = inventory.setdefault(
            name.casefold(), {"name": name, "source_keys": []}
        )
        entry["source_keys"].append(key)
    return [
        {
            "name": entry["name"],
            "status": (
                "configured" if entry["source_keys"] else "pending_configuration"
            ),
            "source_keys": sorted(set(entry["source_keys"])),
        }
        for _, entry in sorted(inventory.items())
    ]


def print_source_name_summary():
    inventory = source_name_inventory()
    configured = sum(entry["status"] == "configured" for entry in inventory)
    print(
        f"SOURCE NAMES: {configured} configured; "
        f"{len(inventory) - configured} pending_configuration (not scheduled). "
        "Use --list-source-names for the full inventory."
    )


def verify(rec):
    """Re-fetch the posting URL. Only keep it if the req is still open."""
    last_error = None
    r = None
    for attempt in range(VERIFY_RETRIES):
        try:
            r = _source_get(
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
    if (
        r.status_code == 202
        and rec.get("src") == "greenhouse"
        and rec.get("source_target") in PUBLIC_GREENHOUSE_BOARDS
    ):
        return rec, True, "202"
    if (
        r.status_code == 403
        and rec.get("src") == "greenhouse"
        and rec.get("source_target") in PUBLIC_GREENHOUSE_BOARDS
    ):
        board = rec["source_target"]
        provider_id = str(rec.get("external_id") or "").rsplit(":", 1)[-1]
        if provider_id:
            try:
                detail = g(
                    f"https://boards-api.greenhouse.io/v1/boards/"
                    f"{quote(board, safe='')}/jobs/"
                    f"{quote(provider_id, safe='')}"
                )
                if detail.status_code == 404:
                    return rec, False, "HTTP 404"
                if detail.status_code == 200:
                    job = detail.json()
                    if (
                        isinstance(job, dict)
                        and str(job.get("id")) == provider_id
                        and job.get("internal_job_id") is not None
                        and job.get("title")
                        and job.get("absolute_url")
                    ):
                        return rec, True, "API-verified 403"
            except (requests.RequestException, ValueError, TypeError):
                pass
    if r.status_code != 200:
        return rec, False, f"HTTP {r.status_code}"
    low = r.text.lower()
    for dead in ("no longer accepting", "position has been filled",
                 "job is no longer available", "posting is closed",
                 "no longer available", "req is closed",
                 "not accepting any further applicants"):
        if dead in low:
            return rec, False, "closed text"
    return rec, True, "200"


DIRECT_JOB_SOURCES = {
    "workday", "greenhouse", "successfactors", "ashby", "lever",
    "smartrecruiters", "amazon"
}


def _listing_text(value):
    text = clean_text(value).casefold()
    text = text.replace("co-op", "coop").replace("co op", "coop")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _listing_location(value):
    text = _listing_text(value)
    tokens = [
        "on" if token == "ontario" else token
        for token in text.split()
        if token not in {"can", "canada"}
    ]
    return " ".join(tokens)


def same_listing(left, right):
    if _listing_text(left.get("company")) != _listing_text(right.get("company")):
        return False
    if _listing_text(left.get("title")) != _listing_text(right.get("title")):
        return False
    left_location = _listing_location(left.get("loc"))
    right_location = _listing_location(right.get("loc"))
    return not left_location or not right_location or left_location == right_location


def listing_url(record):
    parsed = urlsplit(canonicalize_url(record.get("url")))
    query = urlencode([
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key not in {"gh_src", "lever-source"}
    ])
    path = parsed.path.rstrip("/")
    if parsed.hostname in {"jobs.ashbyhq.com", "jobs.lever.co"}:
        path = re.sub(r"/(?:apply|application)$", "", path)
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def workday_requisition_base(record):
    if record.get("src") != "workday" or not record.get("external_id"):
        return None
    tenant, separator, requisition = str(record["external_id"]).partition(":")
    if not separator:
        return None
    return tenant.casefold(), re.sub(r"-\d+$", "", requisition).casefold()


def dedupe_candidates(candidates):
    seen_identity = set()
    seen_urls = set()
    unique = []
    unique_counts = {}
    direct_records = []
    direct_overlap_counts = {}
    ordered = sorted(
        enumerate(candidates),
        key=lambda item: (
            item[1].get("src") == "adzuna",
            not item[1].get("_baseline_existing", False),
            item[1].get("source_target") or "",
            item[0],
        ),
    )
    for _, record in ordered:
        if record.get("external_id"):
            identity = ("external", record["src"], record["external_id"])
        elif record.get("url"):
            identity = ("url", record["src"], record["url"])
        else:
            identity = (
                "fallback", record["src"], record["company"],
                record["title"].strip().lower(), record["loc"].strip().lower(),
            )
        if identity in seen_identity:
            continue
        seen_identity.add(identity)
        canonical_url = listing_url(record) if record.get("url") else ""
        if canonical_url and canonical_url in seen_urls:
            continue

        key = record.get("_coverage_key")
        if record.get("src") == "adzuna" and any(
            same_listing(record, direct) for direct in direct_records
        ):
            direct_overlap_counts[key] = direct_overlap_counts.get(key, 0) + 1
            continue
        if any(
            workday_requisition_base(record)
            and workday_requisition_base(record) == workday_requisition_base(direct)
            and record.get("source_target") != direct.get("source_target")
            and same_listing(record, direct)
            for direct in direct_records
        ):
            continue
        if any(
            record.get("src") != direct.get("src")
            and (record.get("source_target") or direct.get("source_target"))
            and same_listing(record, direct)
            for direct in direct_records
        ):
            continue

        unique.append(record)
        if canonical_url:
            seen_urls.add(canonical_url)
        unique_counts[key] = unique_counts.get(key, 0) + 1
        if record.get("src") in DIRECT_JOB_SOURCES:
            direct_records.append(record)
    return unique, unique_counts, direct_overlap_counts


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
                f"page={query.get('page', '-')}, "
                f"returned={query.get('returned', 0)}, kept={query.get('kept', 0)}, "
                f"rejected={query.get('rejected', 0)}, "
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
        if entry.get("direct_overlap"):
            overlap_detail = (
                f"{key}: omitted {entry['direct_overlap']} aggregate row(s) "
                "in favor of matching direct ATS records"
            )
            source_details.append(overlap_detail)
            print(f"   SOURCE DEDUPE {overlap_detail}")
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
    key = coverage_key("adzuna", ADZUNA_COVERAGE_TARGET)
    with COVERAGE_LOCK:
        entry = dict(COVERAGE.get(key) or {})
        public_entries = {
            public_key: dict(COVERAGE[public_key])
            for public_key in sorted(RETAINED_SOURCE_KEYS) if public_key in COVERAGE
        }
        daily_entries = {
            daily_key: dict(COVERAGE[daily_key])
            for daily_key in sorted(DAILY_SOURCE_KEYS) if daily_key in COVERAGE
        }
    result = {}
    if entry:
        age_hours = entry.get("source_age_hours")
        result["adzuna"] = {
            "status": entry.get("status"),
            "refreshed_at": entry.get("source_refreshed_at"),
            "age_seconds": (
                round(float(age_hours) * 3600)
                if age_hours is not None else None
            ),
            "requests": entry.get("request_attempts", 0),
            "reused": entry.get("reused", 0),
        }
    if public_entries:
        result["public_boards"] = {
            public_key: {
                "status": values.get("status", "blocked"),
                "refreshed_at": values.get("source_refreshed_at"),
                "complete": values.get("complete", False),
                "reused": values.get("reused", 0),
            }
            for public_key, values in public_entries.items()
        }
    if daily_entries:
        result["daily_sources"] = {
            daily_key: {
                "status": values.get("status", "blocked"),
                "refreshed_at": values.get("source_refreshed_at"),
                "complete": values.get("complete", False),
                "reused": values.get("reused", 0),
                "age_seconds": (
                    round(float(values["source_age_hours"]) * 3600)
                    if values.get("source_age_hours") is not None else None
                ),
            }
            for daily_key, values in daily_entries.items()
        }
    return result


def mark_baseline_records(records):
    baseline_path = os.environ.get("SOURCE_BASELINE_PATH", "live_jobs.json")
    try:
        with open(baseline_path, encoding="utf-8") as handle:
            baseline = json.load(handle)
        baseline_jobs = baseline.get("jobs")
        if not isinstance(baseline_jobs, list):
            raise ValueError("invalid baseline jobs")
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return
    baseline_ids = {
        (job.get("src"), str(job.get("external_id"))): job
        for job in baseline_jobs if job.get("src") and job.get("external_id")
    }
    baseline_urls = {
        listing_url(job): job for job in baseline_jobs if job.get("url")
    }
    for record in records:
        identity = (
            record.get("src"),
            str(record.get("external_id")),
        )
        baseline_job = baseline_ids.get(identity)
        if baseline_job is None and record.get("url"):
            baseline_job = baseline_urls.get(listing_url(record))
        if baseline_job is not None:
            record["_baseline_existing"] = True
            record["_baseline_categories"] = list(
                baseline_job.get("cats") or []
            )


def _workday_detail_url(record):
    parsed = urlsplit(record.get("url") or "")
    host_parts = (parsed.hostname or "").split(".")
    match = re.match(
        r"^/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.*)$",
        parsed.path,
    )
    if len(host_parts) < 4 or not match:
        return None
    tenant = host_parts[0]
    site, job_path = match.groups()
    return (
        f"{parsed.scheme}://{parsed.netloc}/wday/cxs/"
        f"{tenant}/{site}{job_path}"
    )


def _smartrecruiters_detail_content(record):
    board = clean_text(record.get("source_target"))
    external_id = str(record.get("external_id") or "").rsplit(":", 1)[-1]
    if not board or not external_id:
        return None
    response = g(
        f"https://api.smartrecruiters.com/v1/companies/"
        f"{quote(board, safe='')}/postings/{quote(external_id, safe='')}"
    )
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")
    data = response.json()
    sections = ((data.get("jobAd") or {}).get("sections") or {})
    content = [
        (sections.get(name) or {}).get("text") or ""
        for name in (
            "jobDescription",
            "qualifications",
            "additionalInformation",
        )
    ]
    return clean_text(" ".join(html_text(value) for value in content if value))


def _workday_detail_content(record):
    url = _workday_detail_url(record)
    if not url:
        return None
    response = g(url)
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")
    data = response.json()
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        raise ValueError("missing jobPostingInfo")
    return clean_text(html_text(info.get("jobDescription") or ""))


def _successfactors_detail_content(record):
    response = g(record["url"])
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")
    text = clean_text(html_text(response.text))
    if re.search(r"not accepting any further applicants", text, re.I):
        return None
    return text


def hydrate_prelaw_details(records):
    errors = {}
    hydrated = 0
    grouped = {}
    for record in records:
        if (
            record.get("content")
            or (
                not is_internship(record.get("title"))
                and not record.get("_placement_evidence")
            )
            or not classify_prelaw(record.get("title"))
        ):
            continue
        loader = {
            "smartrecruiters": _smartrecruiters_detail_content,
            "workday": _workday_detail_content,
            "successfactors": _successfactors_detail_content,
        }.get(record.get("src"))
        if not loader:
            continue
        identity = (
            record.get("src"),
            record.get("url"),
            record.get("external_id"),
        )
        grouped.setdefault(identity, {"loader": loader, "records": []})[
            "records"
        ].append(record)
    identities = sorted(grouped, key=lambda value: tuple(str(x) for x in value))
    capped = max(0, len(identities) - PRELAW_DETAIL_MAX_REQUESTS)
    for identity in identities[PRELAW_DETAIL_MAX_REQUESTS:]:
        for record in grouped[identity]["records"]:
            record["_prelaw_detail_unavailable"] = True
    for identity in identities[:PRELAW_DETAIL_MAX_REQUESTS]:
        group = grouped[identity]
        record = group["records"][0]
        try:
            content = group["loader"](record)
            if content:
                for current in group["records"]:
                    current["content"] = content[:12000]
                hydrated += len(group["records"])
            else:
                for current in group["records"]:
                    current["_prelaw_detail_unavailable"] = True
        except (requests.RequestException, ValueError, TypeError) as exc:
            for current in group["records"]:
                current["_prelaw_detail_unavailable"] = True
                key = current.get("_coverage_key")
                errors.setdefault(key, []).append(type(exc).__name__)
    for key, values in errors.items():
        if not key:
            continue
        with COVERAGE_LOCK:
            current = COVERAGE.setdefault(key, {})
            current["prelaw_detail_errors"] = len(values)
            current["prelaw_detail_error_types"] = ",".join(sorted(set(values)))
        print(
            f"COVERAGE WARNING {key}: {len(values)} pre-law detail "
            "lookup(s) unavailable"
        )
    if hydrated:
        print(
            "PRE-LAW DETAIL ENRICHMENT: "
            f"requests={min(len(identities), PRELAW_DETAIL_MAX_REQUESTS)} "
            f"hydrated_rows={hydrated} capped={capped}"
        )


def hydrate_greenhouse_posted_dates(records):
    pending = {}
    for record in records:
        if (
            record.get("src") != "greenhouse"
            or record.get("posted")
            or not record.get("_greenhouse_board")
            or not record.get("external_id")
        ):
            continue
        board = record["_greenhouse_board"]
        provider_id = str(record["external_id"]).rsplit(":", 1)[-1]
        pending.setdefault((board, provider_id), []).append(record)

    lookups = sorted(pending)[:GREENHOUSE_DETAIL_MAX_REQUESTS]
    capped = len(pending) - len(lookups)
    if not lookups:
        result = {"requested": 0, "resolved": 0, "unavailable": 0, "capped": capped}
        print(
            "GREENHOUSE POSTED DATES: "
            "detail_requests=0 resolved=0 unavailable=0 "
            f"capped={result['capped']}"
        )
        return result

    def fetch_posted_at(identity):
        board, provider_id = identity
        try:
            response = g(
                f"https://boards-api.greenhouse.io/v1/boards/"
                f"{quote(board, safe='')}/jobs/{quote(provider_id, safe='')}"
            )
            if response.status_code != 200:
                return identity, None
            job = response.json()
            if (
                not isinstance(job, dict)
                or str(job.get("id")) != provider_id
                or _parse_timestamp(job.get("first_published")) is None
            ):
                return identity, None
            return identity, job["first_published"]
        except (requests.RequestException, ValueError, TypeError):
            return identity, None

    resolved = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        for identity, posted_at in executor.map(fetch_posted_at, lookups):
            if not posted_at:
                continue
            resolved += 1
            for record in pending[identity]:
                record["posted"] = posted_at

    result = {
        "requested": len(lookups),
        "resolved": resolved,
        "unavailable": len(lookups) - resolved,
        "capped": capped,
    }
    print(
        "GREENHOUSE POSTED DATES: "
        f"detail_requests={result['requested']} "
        f"resolved={result['resolved']} "
        f"unavailable={result['unavailable']} "
        f"capped={result['capped']}"
    )
    return result


def filter_candidates(records):
    candidates, counts = [], {}
    role_eligible = 0
    for record in records:
        record["title"] = clean_text(record.get("title"))
        record["loc"] = clean_text(record.get("loc"))
        if not record["title"] or not record.get("url"):
            continue
        if (
            not is_internship(record["title"])
            and not record.get("_placement_evidence")
        ):
            continue
        if TALENT_POOL_RE.search(record["title"]):
            continue
        if (
            NEW_GRAD_TITLE_RE.search(record["title"])
            and not PUBLIC_INTERNSHIP_RE.search(record["title"])
            and not record.get("_baseline_existing")
        ):
            continue
        if (
            record.get("source_target")
            and not PUBLIC_INTERNSHIP_RE.search(record["title"])
            and not record.get("_placement_evidence")
        ):
            continue
        role_eligible += 1
        region = geo(record["loc"])
        if not region:
            continue
        record["geo"] = region
        content = record.get("content") or ""
        categories = set(categorize(record["title"], content))
        if not mandatory_legal_education_reason(content):
            categories.update(record.get("_prelaw_categories") or [])
        if record.get("_carried_source_row"):
            categories.difference_update(PRELAW_CATEGORIES)
            categories.update(
                category
                for category in (record.get("_baseline_categories") or [])
                if category in PRELAW_CATEGORIES
            )
        elif record.get("_prelaw_detail_unavailable"):
            categories.difference_update(PRELAW_CATEGORIES)
            categories.update(
                category
                for category in (record.get("_baseline_categories") or [])
                if category in PRELAW_CATEGORIES
            )
        record["cats"] = [
            category for category in CATEGORIES if category in categories
        ]
        restriction = restricted_program_note(record["title"], content)
        if restriction and any(
            category in categories for category in PRELAW_CATEGORIES
        ):
            record["content"] = clean_text(
                f"{restriction} {content}"
            )[:12000]
        candidates.append(record)
        key = record.get("_coverage_key")
        counts[key] = counts.get(key, 0) + 1
    FILTER_FUNNEL.update(
        raw_active_listed=len(records),
        role_eligible=role_eligible,
        canada_eligible=len(candidates),
    )
    return candidates, counts


def public_verification_failure(record, reason):
    key = coverage_key(record.get("src"), record.get("source_target"))
    if (
        key not in (RETAINED_SOURCE_KEYS | DAILY_SOURCE_KEYS)
        or reason in {"HTTP 404", "HTTP 410", "closed text"}
    ):
        return
    with COVERAGE_LOCK:
        errors = list(COVERAGE[key].get("errors") or [])
    errors.append(f"posting verification inconclusive: {reason}")
    set_coverage(key, status="blocked", complete=False, errors=errors)
    print(f"COVERAGE WARNING {key}: publication blocked by inconclusive verification")


def snapshot_job(record):
    row = {
        "company": record["company"], "title": record["title"], "loc": record["loc"],
        "geo": record["geo"], "cats": record["cats"], "url": record["url"],
        "posted": record["posted"], "src": record["src"],
        "external_id": record.get("external_id"),
        "content": " ".join((record.get("content") or "").split())[:400],
    }
    if record.get("source_target"):
        row.update(source_target=record["source_target"], last_seen=record["last_seen"])
        if record.get("application_deadline"):
            row["application_deadline"] = record["application_deadline"]
    return row


def main():
    t0 = time.time()
    print_source_name_summary()
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
                    errors=[type(exc).__name__],
                    status=(
                        "blocked"
                        if key in (RETAINED_SOURCE_KEYS | DAILY_SOURCE_KEYS)
                        else "incomplete"
                    ))
                print(f"COVERAGE WARNING {key}: task failed with {type(exc).__name__}")
    print(f"pulled {len(raw)} raw postings in {time.time()-t0:.0f}s")

    mark_baseline_records(raw)
    hydrate_prelaw_details(raw)
    cand, filtered_counts = filter_candidates(raw)
    hydrate_greenhouse_posted_dates(cand)
    print(
        "FILTER FUNNEL: "
        f"active/listed={FILTER_FUNNEL['raw_active_listed']} "
        f"internship/co-op/new-grad={FILTER_FUNNEL['role_eligible']} "
        f"Canada-eligible={FILTER_FUNNEL['canada_eligible']}"
    )

    # Dedupe by source identity, then prefer direct ATS records when an Adzuna
    # aggregate row matches the same normalized company/title/location.
    uniq, unique_counts, direct_overlap_counts = dedupe_candidates(cand)
    adzuna_key = coverage_key("adzuna", ADZUNA_COVERAGE_TARGET)
    if direct_overlap_counts.get(adzuna_key):
        set_coverage(
            adzuna_key,
            direct_overlap=direct_overlap_counts[adzuna_key],
        )
    print(f"{len(uniq)} unique Canada-eligible internships (before liveness check)")

    # verify every URL is still live
    live, dropped = [], []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for rec, ok, why in ex.map(lambda x: verify(x), uniq):
            (live if ok else dropped).append((rec, why))
    print(f"VERIFIED LIVE: {len(live)}   DROPPED (closed/unreachable): {len(dropped)}")
    for rec, why in dropped:
        print(f"   drop [{why}] {rec['company']} - {rec['title'][:50]}")
        public_verification_failure(rec, why)

    live_counts = {}
    for rec, _ in live:
        key = rec.get("_coverage_key")
        live_counts[key] = live_counts.get(key, 0) + 1
    print_coverage_report(filtered_counts, unique_counts, live_counts)

    out = []
    for rec, _ in live:
        out.append(snapshot_job(rec))
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


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-source-names",
        action="store_true",
        help="List configured and pending source names as JSON without scraping",
    )
    args = parser.parse_args(argv)
    if args.list_source_names:
        print(json.dumps(source_name_inventory(), indent=2))
    else:
        main()


if __name__ == "__main__":
    cli()
