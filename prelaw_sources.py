"""Daily official sources and eligibility rules for pre-law placements."""

import html
import re
import time
from datetime import datetime, time as datetime_time, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests


LEGAL_CATEGORY = "Legal, Compliance & Governance"
POLICY_CATEGORY = "Public Policy & Government Relations"
PRELAW_CATEGORIES = (LEGAL_CATEGORY, POLICY_CATEGORY)

DAILY_REFRESH_INTERVAL_HOURS = 24
DAILY_GRACE_HOURS = 72
GC_JOBS_MAX_PAGES = 30
GC_JOBS_MAX_DETAILS = 100
GC_JOBS_REQUEST_DELAY_SECONDS = 1.0
ORACLE_PAGE_SIZE = 200
ORACLE_MAX_PAGES = 5

GC_JOBS_SEARCH_URL = (
    "https://emploisfp-psjobs.cfp-psc.gc.ca/psrs-srfp/applicant/page2440"
)
GC_JOBS_DETAIL_ROOT = (
    "https://emploisfp-psjobs.cfp-psc.gc.ca/psrs-srfp/applicant/"
)
GC_JOBS_SOURCE_TARGET = "public"

ORACLE_HOST = "https://ehaa.fa.ca2.oraclecloud.com"
ORACLE_SITE_NUMBER = "CX_3"
ORACLE_SITE_SLUG = "CNRL-Campus"
ORACLE_SOURCE_TARGET = ORACLE_SITE_SLUG
ORACLE_COMPANY = "Canadian Natural"
ORACLE_LIST_URL = (
    f"{ORACLE_HOST}/hcmRestApi/resources/latest/"
    "recruitingCEJobRequisitions"
)
ORACLE_DETAIL_URL = (
    f"{ORACLE_HOST}/hcmRestApi/resources/latest/"
    "recruitingCEJobRequisitionDetails"
)
ORACLE_PUBLIC_ROOT = (
    f"{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/"
    f"{ORACLE_SITE_SLUG}/job/"
)

PLACEMENT_RE = re.compile(
    r"\b(interns?|internships?|co-?ops?|students?|stagiaires?|"
    r"[eé]tudiants?|[eé]tudiantes?)\b",
    re.I,
)
STUDENT_PROGRAM_RE = re.compile(
    r"\b(student program|research affiliate program|co-?op|internship)\b",
    re.I,
)
LEGAL_TITLE_RE = re.compile(
    r"\b(legal(?: operations?| administration?)?|law clerk|paralegal|"
    r"contract management|compliance|ethics(?: and compliance)?|"
    r"corporate governance|legal governance|compliance governance|"
    r"supervision and governance|privacy|data protection|corporate secretarial|"
    r"regulatory affairs|conformit[ée]|gouvernance|juridique|parajuriste)\b",
    re.I,
)
POLICY_TITLE_RE = re.compile(
    r"\b(public policy|government relations|intergovernmental relations|"
    r"public affairs|government affairs|legislative|parliamentary|"
    r"justice research|human rights policy|advocacy|"
    r"policy (?:research|analyst|student|intern|co-?op)|"
    r"politique publique|"
    r"relations gouvernementales|affaires publiques|l[ée]gislatif)\b",
    re.I,
)
LEGAL_CONTENT_RE = re.compile(
    r"\b(legal operations?|legal documents?|contract(?:ual)? review|"
    r"corporate ethics|ethics & compliance|compliance framework|"
    r"code of conduct|anti-bribery|conflicts? of interest|"
    r"corporate governance|data privacy|legal research|legal drafting|"
    r"litigation support|law clerk|paralegal)\b",
    re.I,
)
POLICY_CONTENT_RE = re.compile(
    r"\b(public policy|government relations|government regulation|"
    r"legislative issues?|legislation|advocacy and engagement|"
    r"briefing notes?|parliamentary|justice policy|human rights policy)\b",
    re.I,
)
FALSE_PRELAW_RE = re.compile(
    r"\b(data governance|sales systems? & data governance|driver compliance|"
    r"product compliance|environmental (?:regulatory )?compliance|"
    r"regulatory facilities|"
    r"credit risk|market risk|risk governance|broking placement|physical security|"
    r"corporate security|software policy|ai policy engineer|"
    r"machine learning policy)\b",
    re.I,
)
SCIENCE_REGULATORY_RE = re.compile(
    r"\b(regulatory affairs|regulatory compliance)\b.*\b("
    r"science|engineering|pharmacy|pharmaceutical|medical device|clinical|"
    r"biology|chemistry)\b",
    re.I | re.S,
)
TECHNICAL_TITLE_RE = re.compile(
    r"\b(software|developers?|engineer(?:ing)?|data|ai|ml|machine learning|"
    r"cloud|devops|sre|security engineer|hardware|firmware|embedded|"
    r"mechanical|electrical|civil|manufactur\w*|scientist)\b",
    re.I,
)
PREFERRED_RE = re.compile(
    r"\b(preferred|preference|asset|nice to have|would be an asset|"
    r"considered an asset|ideally)\b",
    re.I,
)
MANDATORY_RE = re.compile(
    r"\b(must|required|requirement|currently enrolled|currently pursuing|"
    r"pursuing|completion of|completed|graduate of|eligible to|"
    r"member of|licensed|called to)\b",
    re.I,
)
LAW_EDUCATION_RE = re.compile(
    r"\b(law school|law student|juris doctor|j\.?d\.?|ll\.?b\.?|"
    r"bachelor of laws?|law degree|faculty of law|articling|"
    r"eligible to article|law society|called to the bar|licensed lawyer)\b",
    re.I,
)
SPECIALIZED_PROGRAM_RE = re.compile(
    r"\b(law clerk|paralegal)\b.{0,100}\b(program|diploma|certificate|"
    r"college|licen[cs]e)\b|"
    r"\b(program|diploma|certificate|college|licen[cs]e)\b.{0,100}"
    r"\b(law clerk|paralegal)\b",
    re.I,
)


def clean_text(value):
    if value in (None, ""):
        return ""
    return " ".join(str(value).replace("\xa0", " ").split())


def html_text(value):
    if not value:
        return ""
    text = html.unescape(str(value))
    text = re.sub(
        r"<(?:br|/p|/li|/h\d|/div|/dt|/dd)[^>]*>",
        "\n",
        text,
        flags=re.I,
    )
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return "\n".join(
        clean_text(line) for line in text.splitlines() if clean_text(line)
    )


def _sentences(value):
    text = html_text(value)
    chunks = re.split(r"[\n\r]+|(?<=[.!?])\s+", text)
    return [clean_text(chunk) for chunk in chunks if clean_text(chunk)]


def mandatory_legal_education_reason(value):
    for sentence in _sentences(value):
        if not LAW_EDUCATION_RE.search(sentence):
            continue
        if PREFERRED_RE.search(sentence) and not re.search(
            r"\b(must|required|currently enrolled|eligible to|licensed|"
            r"called to)\b",
            sentence,
            re.I,
        ):
            continue
        if MANDATORY_RE.search(sentence) or re.search(
            r"\b(?:law student|articling student)\b",
            sentence,
            re.I,
        ):
            return sentence[:300]
    return None


def restricted_program_note(title, value):
    if not re.search(r"\b(law clerk|paralegal)\b", title or "", re.I):
        return None
    for sentence in _sentences(value):
        if (
            SPECIALIZED_PROGRAM_RE.search(sentence)
            and MANDATORY_RE.search(sentence)
        ):
            program = (
                "Law Clerk"
                if re.search(r"\blaw clerk\b", sentence, re.I)
                else "Paralegal"
            )
            return (
                f"Restricted qualification: current {program} program "
                "enrolment or the specified credential is required."
            )
    return None


def classify_prelaw(title, content=""):
    title = clean_text(title)
    text = f"{title}\n{html_text(content)}"
    if mandatory_legal_education_reason(text):
        return []
    categories = []
    false_positive = FALSE_PRELAW_RE.search(text)
    legal_title = bool(LEGAL_TITLE_RE.search(title) and not false_positive)
    policy_title = bool(POLICY_TITLE_RE.search(title) and not false_positive)
    content_can_establish = not TECHNICAL_TITLE_RE.search(title)
    if (
        (
            legal_title
            or (content_can_establish and LEGAL_CONTENT_RE.search(text))
        )
        and not false_positive
        and not SCIENCE_REGULATORY_RE.search(text)
    ):
        categories.append(LEGAL_CATEGORY)
    if (
        (
            policy_title
            or (content_can_establish and POLICY_CONTENT_RE.search(text))
        )
        and not false_positive
    ):
        categories.append(POLICY_CATEGORY)
    return categories


def prelaw_candidate_hint(title, program=""):
    return bool(
        PLACEMENT_RE.search(title or "")
        or STUDENT_PROGRAM_RE.search(program or "")
    )


def _description(source_label, content, deadline=None, restricted_note=None):
    parts = [f"Source: {source_label}."]
    if restricted_note:
        parts.append(restricted_note)
    education = next(
        (
            sentence
            for sentence in _sentences(content)
            if re.search(
                r"\b(currently enrolled|currently pursuing|pursuing an? "
                r"undergraduate|degree program|law clerk|paralegal)\b",
                sentence,
                re.I,
            )
        ),
        None,
    )
    if education:
        parts.append(f"Eligibility: {education}")
    if deadline:
        parts.append(f"Closing date: {deadline[:10]}.")
    return clean_text(" ".join(parts))[:600]


def _gc_result_blocks(page):
    total_match = re.search(r"Jobs open to the public \((\d+)\)", page)
    page_match = re.search(r"</a>\s*of\s*(\d+)\s*\[", page, re.I)
    rows = []
    blocks = re.findall(
        r'<li class="searchResult">(.*?)</li>',
        page,
        re.I | re.S,
    )
    for block in blocks:
        link = re.search(
            r'href="([^"]*page1800\?poster=(\d+))"[^>]*>(.*?)</a>',
            block,
            re.I | re.S,
        )
        if not link:
            continue
        lines = [
            clean_text(line)
            for line in html_text(block).splitlines()
            if clean_text(line)
        ]
        deadline_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.startswith("Closing date:")
            ),
            None,
        )
        if deadline_index is None:
            continue
        pre_deadline = lines[1:deadline_index]
        after = lines[deadline_index + 1 :]
        organization = after[0] if after else ""
        location_index = 1
        while (
            location_index < len(after)
            and after[location_index].startswith("-")
        ):
            location_index += 1
        location = after[location_index] if location_index < len(after) else ""
        if location.casefold() == "various locations":
            location = "Canada"
        rows.append(
            {
                "poster": link.group(2),
                "title": clean_text(html_text(link.group(3))),
                "program": clean_text(" ".join(pre_deadline)),
                "deadline_date": clean_text(
                    lines[deadline_index].split(":", 1)[1]
                ),
                "company": organization,
                "location": location,
                "url": urljoin(GC_JOBS_DETAIL_ROOT, html.unescape(link.group(1))),
            }
        )
    return {
        "total": int(total_match.group(1)) if total_match else None,
        "pages": int(page_match.group(1)) if page_match else 1,
        "block_count": len(blocks),
        "rows": rows,
    }


def _gc_deadline(value):
    if not value:
        return None
    try:
        date = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    pacific = datetime.combine(
        date,
        datetime_time(23, 59, 59),
        ZoneInfo("America/Vancouver"),
    )
    return pacific.astimezone(timezone.utc).isoformat()


def _gc_detail_text(page):
    lines = [
        clean_text(line)
        for line in html_text(page).splitlines()
        if clean_text(line)
    ]

    def section(start_label, end_labels):
        starts = [
            index for index, line in enumerate(lines)
            if line.casefold() == start_label.casefold()
        ]
        if not starts:
            return []
        start = starts[-1] + 1
        end = len(lines)
        for index in range(start, len(lines)):
            if any(
                lines[index].casefold() == label.casefold()
                for label in end_labels
            ):
                end = index
                break
        return lines[start:end]

    relevant = []
    relevant.extend(section(
        "Duties",
        (
            "Work environment",
            "Intent of the process",
            "Positions to be filled",
            "Important messages",
            "Conditions of employment",
        ),
    ))
    relevant.extend(section(
        "You need (essential for the job)",
        (
            "Language requirements (essential for the job)",
            "You may need (asset qualifications)",
            "Our commitment",
        ),
    ))
    relevant.extend(section(
        "Who can apply",
        ("Organization information",),
    ))
    return "\n".join(relevant)


def collect_gc_jobs(
    *,
    now=None,
    session_factory=requests.Session,
    sleep=time.sleep,
):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    errors = []
    pages = 0
    returned = 0
    details = 0
    try:
        session = session_factory()
        session.headers.update({"User-Agent": "internship-tracker-toronto/1.0"})
        session.get(
            GC_JOBS_SEARCH_URL,
            params={"fromMenu": "true", "toggleLanguage": "en"},
            timeout=30,
        ).raise_for_status()
        searched = session.get(
            GC_JOBS_SEARCH_URL,
            params={
                "tab": "1",
                "search": "Search jobs",
                "international": "international",
            },
            timeout=30,
        )
        searched.raise_for_status()
        first = session.get(
            searched.url
            + ("&" if "?" in searched.url else "?")
            + "isSecondPartOfPage=1&isInitialNetworkCheck=1",
            timeout=30,
        )
        first.raise_for_status()
        parsed = _gc_result_blocks(first.text)
        pages = 1
        rows = list(parsed["rows"])
        result_blocks = parsed["block_count"]
        page_count = parsed["pages"]
        if page_count > GC_JOBS_MAX_PAGES:
            raise ValueError(
                f"GC Jobs pagination exceeds cap: {page_count}"
            )
        for page_number in range(2, page_count + 1):
            sleep(GC_JOBS_REQUEST_DELAY_SECONDS)
            response = session.get(
                GC_JOBS_SEARCH_URL,
                params={
                    "requestedPage": page_number,
                    "fromPage": 1,
                    "tab": 1,
                    "log": "false",
                    "isSecondPartOfPage": 1,
                },
                timeout=30,
            )
            response.raise_for_status()
            current = _gc_result_blocks(response.text)
            pages += 1
            if not current["rows"]:
                if current["block_count"] == 0:
                    raise ValueError(
                        f"GC Jobs page {page_number} returned no rows"
                    )
            rows.extend(current["rows"])
            result_blocks += current["block_count"]
        by_poster = {row["poster"]: row for row in rows}
        returned = result_blocks
        if parsed["total"] is None or result_blocks != parsed["total"]:
            raise ValueError(
                f"GC Jobs incomplete results: expected {parsed['total']}, "
                f"received {result_blocks}"
            )
        candidates = [
            row for row in by_poster.values()
            if prelaw_candidate_hint(row["title"], row["program"])
        ]
        if len(candidates) > GC_JOBS_MAX_DETAILS:
            raise ValueError(
                f"GC Jobs detail candidates exceed cap: {len(candidates)}"
            )
        output = []
        for row in candidates:
            sleep(GC_JOBS_REQUEST_DELAY_SECONDS)
            response = session.get(row["url"], timeout=30)
            response.raise_for_status()
            details += 1
            detail = _gc_detail_text(response.text)
            categories = classify_prelaw(row["title"], detail)
            if not categories:
                continue
            deadline = _gc_deadline(row["deadline_date"])
            if not deadline:
                raise ValueError(
                    f"GC Jobs invalid deadline for poster {row['poster']}"
                )
            if datetime.fromisoformat(deadline) <= now:
                continue
            restricted = restricted_program_note(row["title"], detail)
            output.append(
                {
                    "company": row["company"],
                    "src": "gcjobs",
                    "title": row["title"],
                    "loc": row["location"],
                    "url": row["url"],
                    "external_id": row["poster"],
                    "posted": "",
                    "application_deadline": deadline,
                    "content": _description(
                        "Government of Canada, GC Jobs",
                        detail,
                        deadline=deadline,
                        restricted_note=restricted,
                    ),
                    "_prelaw_categories": categories,
                    "_placement_evidence": True,
                }
            )
        return {
            "rows": output,
            "complete": True,
            "errors": [],
            "pages": pages,
            "returned": returned,
            "details": details,
            "query_total": parsed["total"],
        }
    except (requests.RequestException, ValueError, TypeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        return {
            "rows": [],
            "complete": False,
            "errors": errors,
            "pages": pages,
            "returned": returned,
            "details": details,
            "query_total": None,
        }


def _oracle_unwrap(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Oracle response missing items")
    if not data["items"]:
        return [], 0
    envelope = data["items"][0]
    rows = envelope.get("requisitionList")
    total = envelope.get("TotalJobsCount")
    if not isinstance(rows, list) or not isinstance(total, int):
        raise ValueError("Oracle response missing requisition list or total")
    return rows, total


def collect_cnrl_oracle(*, now=None, session=requests):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    pages = 0
    returned = 0
    details = 0
    errors = []
    try:
        rows = []
        offset = 0
        total = None
        while pages < ORACLE_MAX_PAGES:
            response = session.get(
                ORACLE_LIST_URL,
                params={
                    "onlyData": "true",
                    "finder": (
                        f"findReqs;siteNumber={ORACLE_SITE_NUMBER},"
                        f"limit={ORACLE_PAGE_SIZE},offset={offset}"
                    ),
                    "expand": "requisitionList.secondaryLocations",
                },
                headers={
                    "User-Agent": "internship-tracker-toronto/1.0",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            response.raise_for_status()
            current, current_total = _oracle_unwrap(response.json())
            pages += 1
            if total is None:
                total = current_total
                if total > ORACLE_PAGE_SIZE * ORACLE_MAX_PAGES:
                    raise ValueError(
                        f"Oracle result count exceeds cap: {total}"
                    )
            rows.extend(current)
            if len(rows) >= total:
                break
            if not current:
                raise ValueError("Oracle pagination ended before total")
            offset += ORACLE_PAGE_SIZE
        returned = len(rows)
        if total is None or returned != total:
            raise ValueError(
                f"Oracle incomplete results: expected {total}, "
                f"received {returned}"
            )
        output = []
        seen = set()
        for listing in rows:
            external_id = clean_text(listing.get("Id"))
            title = clean_text(listing.get("Title"))
            if (
                not external_id
                or external_id in seen
                or not prelaw_candidate_hint(title)
            ):
                continue
            seen.add(external_id)
            response = session.get(
                ORACLE_DETAIL_URL,
                params={"onlyData": "true", "q": f"Id={external_id}"},
                headers={
                    "User-Agent": "internship-tracker-toronto/1.0",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            response.raise_for_status()
            detail_body = response.json()
            details += 1
            items = detail_body.get("items") if isinstance(detail_body, dict) else None
            if not isinstance(items, list) or len(items) != 1:
                raise ValueError(
                    f"Oracle detail missing for requisition {external_id}"
                )
            detail = items[0]
            content = "\n".join(
                clean_text(detail.get(field))
                for field in (
                    "ExternalDescriptionStr",
                    "ExternalResponsibilitiesStr",
                    "ExternalQualificationsStr",
                )
                if clean_text(detail.get(field))
            )
            categories = classify_prelaw(title, content)
            if not categories:
                continue
            location = clean_text(
                detail.get("PrimaryLocation") or listing.get("PrimaryLocation")
            )
            if (
                clean_text(listing.get("PrimaryLocationCountry")).casefold()
                != "ca"
                or not location
            ):
                continue
            posted = clean_text(
                detail.get("ExternalPostedStartDate")
                or listing.get("PostedDate")
            )
            if not posted:
                raise ValueError(
                    f"Oracle posted date missing for requisition {external_id}"
                )
            deadline = clean_text(detail.get("ExternalPostedEndDate")) or None
            if deadline:
                candidate = deadline.replace("Z", "+00:00")
                parsed_deadline = datetime.fromisoformat(candidate)
                if parsed_deadline.tzinfo is None:
                    parsed_deadline = parsed_deadline.replace(tzinfo=timezone.utc)
                if parsed_deadline.astimezone(timezone.utc) <= now:
                    continue
                deadline = parsed_deadline.astimezone(timezone.utc).isoformat()
            restricted = restricted_program_note(title, content)
            output.append(
                {
                    "company": ORACLE_COMPANY,
                    "src": "oracle",
                    "title": title,
                    "loc": location,
                    "url": ORACLE_PUBLIC_ROOT + external_id,
                    "external_id": f"{ORACLE_SITE_SLUG}:{external_id}",
                    "posted": posted,
                    "application_deadline": deadline,
                    "content": _description(
                        "Canadian Natural official careers",
                        content,
                        deadline=deadline,
                        restricted_note=restricted,
                    ),
                    "_prelaw_categories": categories,
                    "_placement_evidence": True,
                }
            )
        return {
            "rows": output,
            "complete": True,
            "errors": [],
            "pages": pages,
            "returned": returned,
            "details": details,
            "query_total": total,
        }
    except (requests.RequestException, ValueError, TypeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        return {
            "rows": [],
            "complete": False,
            "errors": errors,
            "pages": pages,
            "returned": returned,
            "details": details,
            "query_total": None,
        }
