"""Conservative JSON-LD and visible HTML extraction; absent facts stay unknown."""

import json
import re
from contextlib import suppress
from typing import Any, Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field


class JobFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    company_name: str | None = None
    job_title: str | None = None
    employment_type: Literal["INTERNSHIP", "NEW_GRAD", "EARLY_CAREER", "UNKNOWN"] = "UNKNOWN"
    country_code: str | None = None
    city: str | None = None
    state_region: str | None = None
    salary_min: float | None = Field(default=None, ge=0)
    salary_max: float | None = Field(default=None, ge=0)
    salary_currency: str | None = None
    salary_period: str | None = None
    required_experience_min: float | None = Field(default=None, ge=0)
    required_experience_max: float | None = Field(default=None, ge=0)
    posting_date: str | None = None
    application_deadline: str | None = None
    internship_start: str | None = None
    internship_end: str | None = None
    paid: bool | None = None
    application_open: bool | None = None
    relevant: bool | None = None
    cpt: Literal["SUPPORTED", "NOT_SUPPORTED", "UNKNOWN"] = "UNKNOWN"
    authorization: Literal[
        "OPT_COMPATIBLE", "SPONSORSHIP_SUPPORTED", "SPONSORSHIP_NOT_SUPPORTED", "UNKNOWN"
    ] = "UNKNOWN"
    text: str = ""
    issues: list[str] = Field(default_factory=list)
    semantic_evidence: list[dict[str, object]] = Field(default_factory=list)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if 0 <= result < 10_000_000 else None
    except (TypeError, ValueError):
        return None


def extract(html: str, status: int = 200) -> JobFacts:
    soup = BeautifulSoup(html, "html.parser")
    postings: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                postings.append(value)
            elif "@graph" in value:
                walk(value["@graph"])

    if html.lstrip().startswith(("{", "[")):
        with suppress(ValueError, RecursionError):
            walk(json.loads(html))
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk(json.loads(script.get_text()))
        except (ValueError, RecursionError):
            continue
    data = postings[0] if len(postings) == 1 else {}
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    description = BeautifulSoup(str(data.get("description", "")), "html.parser").get_text(
        " ", strip=True
    )
    visible = soup.get_text(" ", strip=True)
    text = " ".join((visible, description)).strip()[:30_000]
    title = _string(data.get("title"))
    if title is None:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else None
    organization = _mapping(data.get("hiringOrganization"))
    location = data.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if len(location) == 1 else None
    address = _mapping(_mapping(location).get("address"))
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    countries = {
        "usa": "US",
        "united states": "US",
        "united states of america": "US",
        "canada": "CA",
        "united kingdom": "GB",
    }
    country_code = _string(country)
    if country_code:
        country_code = countries.get(country_code.lower(), country_code.upper())
    salary = _mapping(data.get("baseSalary"))
    value = _mapping(salary.get("value"))
    low = _number(value.get("minValue", value.get("value")))
    high = _number(value.get("maxValue", value.get("value")))
    period = _string(value.get("unitText"))
    facts = JobFacts(
        company_name=_string(organization.get("name")),
        job_title=title,
        country_code=country_code,
        city=_string(address.get("addressLocality")),
        state_region=_string(address.get("addressRegion")),
        salary_min=low,
        salary_max=high,
        salary_currency=_string(salary.get("currency")),
        salary_period=period.upper() if period else None,
        posting_date=_string(data.get("datePosted")),
        application_deadline=_string(data.get("validThrough")),
        internship_start=_string(data.get("jobStartDate")),
        internship_end=_string(data.get("jobEndDate")),
        text=text,
    )
    if len(postings) > 1:
        facts.issues.append("multiple_job_postings")
    combined = f"{title or ''} {data.get('employmentType', '')}".lower()
    if "intern" in combined:
        facts.employment_type = "INTERNSHIP"
    elif re.search(r"new.grad|graduate|entry.level", combined):
        facts.employment_type = "NEW_GRAD"
    elif "full_time" in combined or "full-time" in combined or "early career" in combined:
        facts.employment_type = "EARLY_CAREER"
    # Explicit labeled HTML fields are a conservative fallback, not inferred candidate facts.
    for label, field in (
        ("Company", "company_name"),
        ("Country", "country_code"),
        ("City", "city"),
        ("State", "state_region"),
    ):
        match = re.search(rf"\b{label}:\s*([^;|\n]+?)(?:;|\||$)", text, re.I)
        if getattr(facts, field) is None and match:
            setattr(facts, field, match[1].strip())
    for label, field in (
        ("Start date", "internship_start"),
        ("End date", "internship_end"),
        ("Deadline", "application_deadline"),
    ):
        match = re.search(rf"{label}:\s*(\d{{4}}-\d{{2}}-\d{{2}})", text, re.I)
        if getattr(facts, field) is None and match:
            setattr(facts, field, match[1])
    if facts.city:
        facts.city = {
            "new york": "New York City",
            "nyc": "New York City",
            "sf": "San Francisco",
        }.get(facts.city.casefold(), facts.city)
    if facts.state_region:
        facts.state_region = {"california": "CA", "new york": "NY"}.get(
            facts.state_region.casefold(), facts.state_region.upper()
        )
    if facts.salary_min is None:
        match = re.search(
            r"\$([\d.]+)(?:\s*[-\u2013]\s*\$?([\d.]+))?\s*(?:/\s*(?:hour|hr)|per hour)\b",
            text,
            re.I,
        )
        if match:
            facts.salary_min = _number(match[1])
            facts.salary_max = _number(match[2] or match[1])
            # A bare dollar sign is ambiguous outside an explicitly US posting.
            facts.salary_currency = "USD" if facts.country_code == "US" else None
            facts.salary_period = "HOUR"
    experience = str(data.get("experienceRequirements", ""))
    months = _number(_mapping(data.get("experienceRequirements")).get("monthsOfExperience"))
    matches = re.findall(
        r"(\d+)(?:\s*[-\u2013]\s*(\d+))?\s*\+?\s*years?\s+"
        r"(?:(?:of |professional |industry |relevant |work |software )*)"
        r"experience\s*(?:required|minimum)",
        text,
        re.I,
    )
    if experience and not re.search(r"preferred|ideally", experience, re.I):
        matches += re.findall(r"(\d+)(?:\s*[-\u2013]\s*(\d+))?\s*\+?\s*years?", experience, re.I)
    if months is not None:
        facts.required_experience_min = months / 12
    elif matches:
        facts.required_experience_min = max(float(m[0]) for m in matches)
        facts.required_experience_max = max(float(m[1] or m[0]) for m in matches)
    elif re.search(r"no (?:prior |professional )?experience (?:is )?required", text, re.I):
        facts.required_experience_min = 0
    if re.search(r"\bunpaid\b|without (?:pay|compensation)", text, re.I):
        facts.paid = False
    elif re.search(r"\bpaid internship\b", text, re.I) or (
        facts.salary_min is not None and facts.salary_min > 0
    ):
        facts.paid = True
    if status in {404, 410} or re.search(
        r"no longer accepting applications|position (?:has been |is )?(?:filled|closed)|"
        r"job (?:has been )?removed",
        text,
        re.I,
    ):
        facts.application_open = False
    elif re.search(r"applications (?:are )?open|accepting applications|apply now", text, re.I):
        facts.application_open = True
    if status not in {200, 404, 410}:
        facts.issues.append("http_unavailable")
    if status in {401, 403} or re.search(
        r"captcha|verify (?:you are|you're) human|access denied", text, re.I
    ):
        facts.issues.append("human_challenge")
    if re.search(r"(?:CPT (?:is )?(?:accepted|supported|eligible)|CPT-compatible)", text, re.I):
        facts.cpt = "SUPPORTED"
    if re.search(
        r"(?:no CPT|CPT (?:is )?not (?:accepted|supported)|cannot (?:accept|support) CPT)",
        text,
        re.I,
    ):
        facts.cpt = "NOT_SUPPORTED"
    if re.search(
        r"(?:no (?:visa )?sponsorship|(?:cannot|do not|does not|will not) "
        r"(?:provide |offer )?(?:visa )?sponsor|"
        r"sponsorship (?:is )?not (?:available|provided))",
        text,
        re.I,
    ):
        facts.authorization = "SPONSORSHIP_NOT_SUPPORTED"
    elif re.search(
        r"(?:visa sponsorship (?:is )?(?:available|provided|supported)|"
        r"we (?:offer|provide) (?:visa )?sponsorship)",
        text,
        re.I,
    ):
        facts.authorization = "SPONSORSHIP_SUPPORTED"
    elif re.search(r"OPT (?:is )?(?:accepted|supported|eligible)", text, re.I):
        facts.authorization = "OPT_COMPATIBLE"
    if (
        facts.salary_min is not None
        and facts.salary_max is not None
        and facts.salary_min > facts.salary_max
    ):
        facts.issues.append("contradictory_salary")
    return facts
