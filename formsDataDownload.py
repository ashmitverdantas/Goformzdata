from __future__ import annotations

import html as html_lib
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Load a local .env file when present (local dev only). In the container the
# real values are injected as environment variables / secret references.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - python-dotenv is optional
    pass

LOGGER = logging.getLogger("goformz_extractor")


# ===========================================================================
# 0) Environment helpers
# ===========================================================================
def required_env(name: str) -> str:
    """Return a required environment variable or raise a clear error."""
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(
            f"Required environment variable is missing or empty: {name}"
        )
    return value.strip()


def optional_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


# ===========================================================================
# 1) CONFIG  — all values come from environment variables
# ===========================================================================
# --- GoFormz login (website, not API) --------------------------------------
GOFORMZ_EMAIL = required_env("GOFORMZ_EMAIL")
GOFORMZ_PASSWORD = required_env("GOFORMZ_PASSWORD")

# --- Date range (inclusive). Empty -> completed last Friday..Thursday. ------
START_DATE: Optional[str] = optional_env("START_DATE")
END_DATE: Optional[str] = optional_env("END_DATE")

# --- OneLake / Fabric Lakehouse (service principal) ------------------------
FABRIC_TENANT_ID = optional_env("TENANT_ID")
FABRIC_CLIENT_ID = optional_env("CLIENT_ID")
FABRIC_CLIENT_SECRET = optional_env("CLIENT_SECRET")
ONELAKE_ACCOUNT_URL = optional_env(
    "ONELAKE_ACCOUNT_URL", "https://onelake.dfs.fabric.microsoft.com"
)
FABRIC_WORKSPACE_NAME = required_env("FABRIC_WORKSPACE_NAME")
FABRIC_LAKEHOUSE_NAME = required_env("FABRIC_LAKEHOUSE_NAME")
ONELAKE_TARGET_SUBPATH = optional_env("ONELAKE_TARGET_SUBPATH", "Files/goformz")

# Require durable upload by default. Set REQUIRE_ONELAKE_UPLOAD=false only for
# local dev where writing to disk is acceptable.
REQUIRE_ONELAKE_UPLOAD = env_flag("REQUIRE_ONELAKE_UPLOAD", True)

# Safety cap so a bad run doesn't open thousands of forms.
MAX_FORMS = int(optional_env("MAX_FORMS", "500"))

# --- GoFormz web app -------------------------------------------------------
APP_BASE = optional_env("APP_BASE", "https://app.goformz.com")

# --- Azure AI Document Intelligence (OCR) ----------------------------------
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = required_env("AZURE_DI_ENDPOINT")
AZURE_DOCUMENT_INTELLIGENCE_KEY = required_env("AZURE_DI_KEY")
DOCUMENT_INTELLIGENCE_API_VERSION = optional_env(
    "AZURE_DI_API_VERSION", "2024-11-30"
)
DOCUMENT_INTELLIGENCE_MODEL_ID = optional_env(
    "AZURE_DI_MODEL_ID", "prebuilt-layout"
)

# --- Selenium behaviour ----------------------------------------------------
HEADLESS = env_flag("HEADLESS", True)
CHROME_BIN = optional_env("CHROME_BIN")
CHROMEDRIVER_PATH = optional_env("CHROMEDRIVER_PATH")
LOGIN_WAIT = int(optional_env("LOGIN_WAIT", "40"))
LIST_PAGE_WAIT = int(optional_env("LIST_PAGE_WAIT", "30"))
FORMS_LIST_MAX_PAGES = int(optional_env("FORMS_LIST_MAX_PAGES", "200"))

# --- Timing / networking ---------------------------------------------------
REQUEST_TIMEOUT = int(optional_env("REQUEST_TIMEOUT", "60"))
REQUEST_DELAY_SECONDS = float(optional_env("REQUEST_DELAY_SECONDS", "0.25"))
DOCUMENT_ANALYSIS_TIMEOUT_SECONDS = int(
    optional_env("DOCUMENT_ANALYSIS_TIMEOUT_SECONDS", "180")
)
DOCUMENT_POLL_SECONDS = int(optional_env("DOCUMENT_POLL_SECONDS", "2"))

# Max seconds we are willing to wait out a GoFormz quota (403) window before
# giving up. Keep below the Container Apps Job replica-timeout.
MAX_QUOTA_WAIT_SECONDS = int(optional_env("MAX_QUOTA_WAIT_SECONDS", "900"))

# Fail the run if EVERY record is Failed/Partial (data-quality gate).
FAIL_IF_ALL_PARTIAL = env_flag("FAIL_IF_ALL_PARTIAL", True)


# ===========================================================================
# 2) Output schema + regexes
# ===========================================================================
OUTPUT_COLUMNS = [
    "Form ID",
    "Form Name",
    "Report Type",
    "Owner",
    "Last Updated",
    "Created Date",
    "Report Month",
    "Report Year",
    "Report Month Name",
    "Report Month Number",
    "Form URL",
    "Date Requested",
    "Created Date Source",
    "Extraction Status",
    "Extraction Error",
]

DATE_VALUE_PATTERN = re.compile(
    r"\b("
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}"
    r")\b",
    flags=re.IGNORECASE,
)

REQUESTED_DATE_LABEL_PATTERN = re.compile(
    r"\brequest(?:ed)?[\s_-]*date\b",
    flags=re.IGNORECASE,
)

STYLE_NUMBER_PATTERN = re.compile(
    r"(?:^|;)\s*(left|top|width|height)\s*:\s*(-?\d+(?:\.\d+)?)px",
    flags=re.IGNORECASE,
)

# GoFormz quota exhaustion is reported as HTTP 403 with this marker.
QUOTA_MARKER = "out of call volume quota"


@dataclass(frozen=True)
class Settings:
    app_base: str
    request_timeout: int
    request_delay_seconds: float
    document_intelligence_endpoint: str
    document_intelligence_key: str
    document_intelligence_model_id: str
    document_intelligence_api_version: str
    # OneLake upload
    require_onelake_upload: bool
    onelake_account_url: str
    fabric_workspace_name: str
    fabric_lakehouse_name: str
    onelake_target_subpath: str
    fabric_client_id: Optional[str]
    fabric_client_secret: Optional[str]
    fabric_tenant_id: Optional[str]


@dataclass(frozen=True)
class ExtractionParameters:
    start_date: date
    end_date: date
    max_forms: int


@dataclass(frozen=True)
class PositionedText:
    page_number: int
    x: float
    y: float
    text: str
    source: str


class GoFormzError(RuntimeError):
    """Raised when GoFormz cannot complete a request."""


class GoFormzQuotaError(GoFormzError):
    """Raised when GoFormz reports an out-of-quota (403) condition."""


class SessionExpiredError(GoFormzError):
    """Raised when the copied web session is no longer authenticated."""


class DocumentIntelligenceError(RuntimeError):
    """Raised when Document Intelligence cannot analyze an image."""


# ===========================================================================
# 3) Parameters + logging
# ===========================================================================
def configure_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def get_default_date_range(
    reference_date: Optional[date] = None,
) -> tuple[str, str]:
    """Return the COMPLETED Friday->Thursday cycle relative to today (UTC).

    Run on a Friday, this returns the previous Friday through the Thursday
    that just finished, i.e. the fully completed prior week.
    """
    today = reference_date or datetime.now(timezone.utc).date()
    days_since_thursday = (today.weekday() - 3) % 7
    completed_thursday = today - timedelta(days=days_since_thursday)
    completed_friday = completed_thursday - timedelta(days=6)
    return completed_friday.isoformat(), completed_thursday.isoformat()


def parse_input_date(value: Optional[str], name: str) -> Optional[date]:
    if value is None or not str(value).strip():
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"{name} must use YYYY-MM-DD format. Received: {value}"
        ) from exc


def resolve_date_range(
    start_date: Optional[str], end_date: Optional[str]
) -> tuple[date, date]:
    parsed_start = parse_input_date(start_date, "START_DATE")
    parsed_end = parse_input_date(end_date, "END_DATE")
    if parsed_start is None or parsed_end is None:
        default_start, default_end = get_default_date_range()
        return (
            datetime.strptime(default_start, "%Y-%m-%d").date(),
            datetime.strptime(default_end, "%Y-%m-%d").date(),
        )
    return parsed_start, parsed_end


# ===========================================================================
# 4) General helpers
# ===========================================================================
def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", html_lib.unescape(str(value))).strip()
    return cleaned or None


def first_not_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return None


def extract_display_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return clean_text(value)
    if isinstance(value, dict):
        for key in (
            "displayName", "fullName", "name", "text", "value", "label",
            "title", "userName", "username", "email",
        ):
            if key in value:
                extracted = extract_display_value(value.get(key))
                if extracted:
                    return extracted
    if isinstance(value, list):
        values = []
        for item in value:
            extracted = extract_display_value(item)
            if extracted:
                values.append(extracted)
        if values:
            return ", ".join(dict.fromkeys(values))
    return None


def find_value_recursively(
    record: Any, candidate_keys: Sequence[str]
) -> Optional[str]:
    if isinstance(record, dict):
        key_map = {str(key).lower(): key for key in record}
        for candidate in candidate_keys:
            actual_key = key_map.get(candidate.lower())
            if actual_key is not None:
                extracted = extract_display_value(record.get(actual_key))
                if extracted:
                    return extracted
        for nested in record.values():
            extracted = find_value_recursively(nested, candidate_keys)
            if extracted:
                return extracted
    elif isinstance(record, list):
        for nested in record:
            extracted = find_value_recursively(nested, candidate_keys)
            if extracted:
                return extracted
    return None


def get_owner(record: dict[str, Any]) -> Optional[str]:
    return find_value_recursively(
        record,
        ("owner", "ownerName", "formOwner", "assignedTo", "assignedToName",
         "assignee", "user", "createdBy"),
    )


def get_nested_id(record: dict[str, Any]) -> Optional[str]:
    return clean_text(
        first_not_empty(
            record.get("id"), record.get("formId"), record.get("formID"),
            record.get("formzId"), record.get("formzID"), record.get("uid"),
        )
    )


def get_form_name(record: dict[str, Any]) -> Optional[str]:
    return clean_text(
        first_not_empty(
            record.get("name"), record.get("formName"),
            record.get("title"), record.get("displayName"),
        )
    )


def get_last_updated(record: dict[str, Any]) -> Optional[str]:
    value = find_value_recursively(
        record,
        ("lastUpdated", "lastUpdatedDate", "lastUpdatedDateTime",
         "lastUpdateDate", "modifiedDate", "modifiedDateTime", "lastModified",
         "lastModifiedDate", "lastModifiedDateTime", "updatedDate",
         "updatedDateTime", "dateModified", "modifiedAt", "updatedAt"),
    )
    if not value:
        return None
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.notna(parsed):
        return parsed.strftime("%Y-%m-%d %I:%M %p UTC")
    return clean_text(value)


def build_form_url(form_id: Optional[str], app_base: str) -> Optional[str]:
    return f"{app_base}/forms/{form_id}" if form_id else None


# ===========================================================================
# 5) Date helpers
# ===========================================================================
def normalize_extracted_date(value: Optional[str]) -> Optional[str]:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    parsed = pd.to_datetime(cleaned, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y-%m-%d")


def build_date_columns(created_date: Optional[str]) -> dict[str, Optional[str]]:
    result = {
        "Created Date": None, "Report Month": None, "Report Year": None,
        "Report Month Name": None, "Report Month Number": None,
    }
    normalized = normalize_extracted_date(created_date)
    if not normalized:
        return result
    parsed = datetime.strptime(normalized, "%Y-%m-%d")
    result.update({
        "Created Date": normalized,
        "Report Month": parsed.strftime("%Y-%m-%b").upper(),
        "Report Year": parsed.strftime("%Y"),
        "Report Month Name": parsed.strftime("%b").upper(),
        "Report Month Number": parsed.strftime("%m"),
    })
    return result


def parse_form_name(form_name: Optional[str]) -> dict[str, Optional[str]]:
    result = build_date_columns(None)
    result["Report Type"] = None
    if not form_name:
        return result
    parts = [part.strip() for part in form_name.split("_")]
    if parts:
        result.update(build_date_columns(parts[0]))
    if len(parts) >= 3:
        result["Report Type"] = parts[2] or None
    return result


def extract_date_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = DATE_VALUE_PATTERN.search(text)
    if not match:
        return None
    return normalize_extracted_date(match.group(1))


# ===========================================================================
# 6) Selenium login
# ===========================================================================
def build_driver() -> webdriver.Chrome:
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.page_load_strategy = "eager"

    # Point Chrome at the Chromium binary installed by the Dockerfile.
    if CHROME_BIN:
        opts.binary_location = CHROME_BIN

    if CHROMEDRIVER_PATH:
        driver = webdriver.Chrome(
            service=Service(CHROMEDRIVER_PATH), options=opts
        )
    else:
        # Local fallback: Selenium Manager resolves the driver automatically.
        driver = webdriver.Chrome(options=opts)

    driver.set_page_load_timeout(60)
    return driver


def selenium_login(driver: webdriver.Chrome) -> None:
    """Log in to app.goformz.com with email + password."""
    LOGGER.info("Logging in to GoFormz web app.")
    driver.get(f"{APP_BASE}/login")
    wait = WebDriverWait(driver, LOGIN_WAIT)

    email_box = wait.until(EC.presence_of_element_located(
        (By.CSS_SELECTOR,
         "input[type='email'], input[name='email'], input#email")
    ))
    email_box.clear()
    email_box.send_keys(GOFORMZ_EMAIL)

    try:
        pwd_box = driver.find_element(
            By.CSS_SELECTOR,
            "input[type='password'], input[name='password'], input#password",
        )
    except Exception:
        for xp in ("//button[contains(., 'Next')]",
                   "//button[contains(., 'Continue')]",
                   "//button[@type='submit']"):
            try:
                driver.find_element(By.XPATH, xp).click()
                break
            except Exception:
                continue
        pwd_box = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input[type='password']")
        ))

    pwd_box.clear()
    pwd_box.send_keys(GOFORMZ_PASSWORD)

    for xp in ("//button[@type='submit']",
               "//button[contains(., 'Log')]",
               "//button[contains(., 'Sign')]"):
        try:
            driver.find_element(By.XPATH, xp).click()
            break
        except Exception:
            continue

    wait.until(lambda d: "login" not in d.current_url.lower())
    time.sleep(2)
    LOGGER.info("Login successful.")


def build_requests_session_from_driver(
    driver: webdriver.Chrome,
) -> requests.Session:
    """Copy Selenium cookies into a retry-enabled requests session."""
    retry = Retry(
        total=5, connect=5, read=5, status=5, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True, raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,*/*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"
        ),
        "Referer": f"{APP_BASE}/forms",
    })
    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie.get("name"),
            cookie.get("value"),
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    return session


# ===========================================================================
# 7) Enumerate forms from the WEB UI
# ===========================================================================
NEXT_PAGE_SELECTORS = (
    "button[aria-label='Next']",
    "button[aria-label='Next page']",
    "a[rel='next']",
    "li.pagination-next:not(.disabled) a",
)

FORM_ID_RE = re.compile(r"/forms/([0-9a-fA-F-]{6,})")


def _collect_forms_on_page(
    driver: webdriver.Chrome,
) -> list[dict[str, Any]]:
    """Scrape form id / name / owner / last-updated from one grid page."""
    found: dict[str, dict[str, Any]] = {}

    for anchor in driver.find_elements(By.CSS_SELECTOR, "a[href*='/forms/']"):
        try:
            href = anchor.get_attribute("href") or ""
            match = FORM_ID_RE.search(href)
            if not match:
                continue
            form_id = match.group(1)
            name = clean_text(anchor.get_attribute("title") or anchor.text)
            row = {"id": form_id, "name": name}
            try:
                tr = anchor.find_element(By.XPATH, "./ancestor-or-self::tr[1]")
                cells = [clean_text(td.text)
                         for td in tr.find_elements(By.CSS_SELECTOR, "td")]
                cells = [c for c in cells if c]
                if cells:
                    if not row.get("name"):
                        row["name"] = cells[0]
                    for cell in cells:
                        if extract_date_from_text(cell):
                            row.setdefault("lastUpdated", cell)
                        elif cell != row.get("name"):
                            row.setdefault("owner", cell)
            except Exception:
                pass
            found[form_id] = {
                **found.get(form_id, {}),
                **{k: v for k, v in row.items() if v},
            }
        except Exception:
            continue

    for tr in driver.find_elements(
        By.CSS_SELECTOR, "tr[data-formid], [data-formid]"
    ):
        try:
            form_id = tr.get_attribute("data-formid")
            if not form_id:
                continue
            cells = [clean_text(td.text)
                     for td in tr.find_elements(By.CSS_SELECTOR, "td")]
            cells = [c for c in cells if c]
            row: dict[str, Any] = {"id": form_id}
            if cells:
                row["name"] = cells[0]
                for cell in cells[1:]:
                    if extract_date_from_text(cell):
                        row.setdefault("lastUpdated", cell)
                    else:
                        row.setdefault("owner", cell)
            found[form_id] = {
                **found.get(form_id, {}),
                **{k: v for k, v in row.items() if v},
            }
        except Exception:
            continue

    return list(found.values())


def _page_marker(driver: webdriver.Chrome) -> Optional[str]:
    """Return the first form id on the current page (pagination fingerprint)."""
    for anchor in driver.find_elements(By.CSS_SELECTOR, "a[href*='/forms/']"):
        match = FORM_ID_RE.search(anchor.get_attribute("href") or "")
        if match:
            return match.group(1)
    for tr in driver.find_elements(By.CSS_SELECTOR, "[data-formid]"):
        fid = tr.get_attribute("data-formid")
        if fid:
            return fid
    return None


def _go_to_next_page(
    driver: webdriver.Chrome, previous_marker: Optional[str]
) -> bool:
    """Click Next and confirm the grid content actually changed."""
    for selector in NEXT_PAGE_SELECTORS:
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, selector)
            for btn in buttons:
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].click();", btn)
                    try:
                        WebDriverWait(driver, LIST_PAGE_WAIT).until(
                            lambda d: _page_marker(d) not in (
                                None, previous_marker
                            )
                        )
                        return True
                    except Exception:
                        return False
        except Exception:
            continue
    return False


def enumerate_forms_from_ui(
    driver: webdriver.Chrome,
) -> list[dict[str, Any]]:
    """Walk the Forms grid in the UI and collect all visible forms."""
    LOGGER.info("Opening Forms list in the web UI.")
    driver.get(f"{APP_BASE}/forms")
    WebDriverWait(driver, LIST_PAGE_WAIT).until(
        lambda d: d.find_elements(
            By.CSS_SELECTOR, "a[href*='/forms/'], tr[data-formid]"
        )
    )
    time.sleep(1.5)

    all_forms: dict[str, dict[str, Any]] = {}
    for page_index in range(1, FORMS_LIST_MAX_PAGES + 1):
        marker = _page_marker(driver)
        page_forms = _collect_forms_on_page(driver)
        new_count = 0
        for form in page_forms:
            fid = form.get("id")
            if fid and fid not in all_forms:
                all_forms[fid] = form
                new_count += 1
        LOGGER.info(
            "List page %s: %s forms on page, %s total collected.",
            page_index, len(page_forms), len(all_forms),
        )
        if not _go_to_next_page(driver, marker) or new_count == 0:
            break

    return list(all_forms.values())


# ===========================================================================
# 8) Form HTML + positioned content
# ===========================================================================
def response_is_login_page(response: requests.Response) -> bool:
    url = response.url.lower()
    sample = response.text[:20000].lower()
    return any(
        marker in url or marker in sample
        for marker in ("/login", "/signin", "accounts.goformz.com",
                       'type="password"', "connect/authorize")
    )


def _raise_for_quota(response: requests.Response) -> None:
    """Raise GoFormzQuotaError if the response is a 403 quota exhaustion."""
    if response.status_code == 403 and QUOTA_MARKER in response.text.lower():
        retry_after = response.headers.get("Retry-After")
        raise GoFormzQuotaError(
            f"GoFormz quota exhausted (HTTP 403). Retry-After={retry_after}."
        )


def fetch_form_html(
    app_session: requests.Session, form_url: str, timeout: int
) -> str:
    try:
        response = app_session.get(
            form_url, timeout=timeout, allow_redirects=True
        )
    except requests.RequestException as exc:
        raise GoFormzError(f"Form HTML request failed: {exc}") from exc
    _raise_for_quota(response)
    if response.status_code == 403:
        raise GoFormzError("Form HTML returned HTTP 403 (authorization).")
    if not response.ok:
        raise GoFormzError(f"Form HTML returned HTTP {response.status_code}.")
    if response_is_login_page(response):
        raise SessionExpiredError(
            "Form URL redirected to login (session expired)."
        )
    if "<html" not in response.text[:10000].lower():
        raise GoFormzError("Form response was not HTML.")
    return response.text


def parse_style_numbers(style: Optional[str]) -> dict[str, float]:
    if not style:
        return {}
    return {
        key.lower(): float(value)
        for key, value in STYLE_NUMBER_PATTERN.findall(style)
    }


def nearest_field_wrapper(element: Tag) -> Optional[Tag]:
    parent = element
    while isinstance(parent, Tag):
        if parent.get("data-testid") == "field-wrapper":
            return parent
        parent = parent.parent
    return None


def element_position(element: Tag) -> tuple[float, float]:
    wrapper = nearest_field_wrapper(element)
    candidate = wrapper or element
    x_value = candidate.get("x")
    y_value = candidate.get("y")
    try:
        if x_value is not None and y_value is not None:
            return float(x_value), float(y_value)
    except (TypeError, ValueError):
        pass
    style_values = parse_style_numbers(element.get("style"))
    return style_values.get("left", 0.0), style_values.get("top", 0.0)


def page_dimensions(page_wrapper: Tag) -> tuple[float, float]:
    style = parse_style_numbers(page_wrapper.get("style"))
    return style.get("width", 1224.0), style.get("height", 1584.0)


def extract_dom_positioned_text(soup: BeautifulSoup) -> list[PositionedText]:
    output = []
    for page_number, page in enumerate(soup.select(".page-wrapper"), start=1):
        seen = set()
        for element in page.select('[data-testid="label-text"]'):
            text = clean_text(element.get_text(" ", strip=True))
            if not text:
                continue
            x_value, y_value = element_position(element)
            key = (round(x_value, 2), round(y_value, 2), text)
            if key not in seen:
                seen.add(key)
                output.append(
                    PositionedText(page_number, x_value, y_value, text, "DOM")
                )
        for element in page.select("input[value], textarea"):
            value = (element.get("value") if element.name == "input"
                     else element.get_text(" ", strip=True))
            text = clean_text(value)
            if not text:
                continue
            x_value, y_value = element_position(element)
            key = (round(x_value, 2), round(y_value, 2), text)
            if key not in seen:
                seen.add(key)
                output.append(
                    PositionedText(page_number, x_value, y_value, text, "DOM")
                )
    return output


def extract_page_backgrounds(
    soup: BeautifulSoup, base_url: str
) -> list[tuple[int, str, float, float]]:
    backgrounds = []
    for page_number, page in enumerate(soup.select(".page-wrapper"), start=1):
        width, height = page_dimensions(page)
        image = page.select_one(":scope > .page > img")
        if image is None:
            image = page.select_one(".page > img")
        if image is None:
            continue
        source = image.get("src")
        if source:
            backgrounds.append((
                page_number,
                urljoin(base_url, html_lib.unescape(source)),
                width, height,
            ))
    return backgrounds


def download_image(
    session: requests.Session, image_url: str, timeout: int
) -> tuple[bytes, str]:
    try:
        response = session.get(image_url, timeout=timeout)
    except requests.RequestException as exc:
        raise GoFormzError(f"Page image download failed: {exc}") from exc
    _raise_for_quota(response)
    if not response.ok:
        raise GoFormzError(f"Page image returned HTTP {response.status_code}.")
    content_type = (response.headers.get("Content-Type", "image/png")
                    .split(";", maxsplit=1)[0].strip())
    if not response.content:
        raise GoFormzError("Page image response was empty.")
    return response.content, content_type


# ===========================================================================
# 9) Azure AI Document Intelligence REST (retry-enabled session)
# ===========================================================================
def build_di_session() -> requests.Session:
    retry = Retry(
        total=4, connect=4, read=4, status=4, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True, raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_DI_SESSION = build_di_session()


def validate_document_intelligence_settings(settings: Settings) -> None:
    endpoint = settings.document_intelligence_endpoint
    key = settings.document_intelligence_key
    if not endpoint or "<RESOURCE_NAME>" in endpoint:
        raise ValueError("A valid Document Intelligence endpoint is required.")
    if not key or key.startswith("<"):
        raise ValueError("A valid Document Intelligence key is required.")


def analyze_image_with_document_intelligence(
    image_bytes: bytes, content_type: str, settings: Settings
) -> dict[str, Any]:
    validate_document_intelligence_settings(settings)
    endpoint = settings.document_intelligence_endpoint.rstrip("/")
    analyze_url = (
        f"{endpoint}/documentintelligence/documentModels/"
        f"{settings.document_intelligence_model_id}:analyze"
    )
    try:
        response = _DI_SESSION.post(
            analyze_url,
            params={"api-version": settings.document_intelligence_api_version},
            headers={
                "Ocp-Apim-Subscription-Key": settings.document_intelligence_key,
                "Content-Type": content_type,
            },
            data=image_bytes, timeout=settings.request_timeout,
        )
    except requests.RequestException as exc:
        raise DocumentIntelligenceError(
            f"Analyze request failed: {exc}"
        ) from exc

    if response.status_code not in (200, 202):
        raise DocumentIntelligenceError(
            f"Analyze request returned HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )
    if response.status_code == 200:
        return response.json()

    operation_url = response.headers.get("Operation-Location")
    if not operation_url:
        raise DocumentIntelligenceError(
            "Analyze response did not include Operation-Location."
        )

    deadline = time.monotonic() + DOCUMENT_ANALYSIS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(DOCUMENT_POLL_SECONDS)
        try:
            poll = _DI_SESSION.get(
                operation_url,
                headers={
                    "Ocp-Apim-Subscription-Key":
                        settings.document_intelligence_key
                },
                timeout=settings.request_timeout,
            )
        except requests.RequestException as exc:
            raise DocumentIntelligenceError(
                f"Analyze polling failed: {exc}"
            ) from exc
        if not poll.ok:
            raise DocumentIntelligenceError(
                f"Analyze polling returned HTTP {poll.status_code}: "
                f"{poll.text[:1000]}"
            )
        payload = poll.json()
        status = str(payload.get("status", "")).lower()
        if status == "succeeded":
            return payload
        if status in {"failed", "canceled"}:
            raise DocumentIntelligenceError(
                f"Document analysis {status}: {str(payload)[:1000]}"
            )
    raise DocumentIntelligenceError("Document analysis polling timed out.")


def polygon_center(polygon: Any) -> tuple[float, float]:
    if not isinstance(polygon, list) or not polygon:
        return 0.0, 0.0
    if isinstance(polygon[0], dict):
        x_values = [float(point.get("x", 0.0)) for point in polygon]
        y_values = [float(point.get("y", 0.0)) for point in polygon]
    else:
        x_values = [float(value) for value in polygon[0::2]]
        y_values = [float(value) for value in polygon[1::2]]
    if not x_values or not y_values:
        return 0.0, 0.0
    return sum(x_values) / len(x_values), sum(y_values) / len(y_values)


def extract_ocr_positioned_text(
    analysis_payload: dict[str, Any], form_page_number: int,
    rendered_width: float, rendered_height: float,
) -> list[PositionedText]:
    analyze_result = analysis_payload.get("analyzeResult", analysis_payload)
    pages = analyze_result.get("pages", [])
    if not pages:
        return []
    result = []
    for page in pages:
        source_width = float(page.get("width") or rendered_width or 1.0)
        source_height = float(page.get("height") or rendered_height or 1.0)
        x_scale = rendered_width / source_width
        y_scale = rendered_height / source_height
        for line in page.get("lines", []):
            text = clean_text(line.get("content"))
            if not text:
                continue
            center_x, center_y = polygon_center(
                line.get("polygon") or line.get("boundingPolygon")
            )
            result.append(PositionedText(
                page_number=form_page_number, x=center_x * x_scale,
                y=center_y * y_scale, text=text, source="OCR",
            ))
    return result


# ===========================================================================
# 10) Date-priority resolver
# ===========================================================================
def sort_positioned_text(
    items: Sequence[PositionedText],
) -> list[PositionedText]:
    return sorted(items, key=lambda item: (item.page_number, item.y, item.x))


def requested_date_candidates(
    items: Sequence[PositionedText],
) -> tuple[Optional[str], set[int]]:
    ordered = sort_positioned_text(items)
    excluded: set[int] = set()
    requested_date = None
    for index, item in enumerate(ordered):
        match = REQUESTED_DATE_LABEL_PATTERN.search(item.text)
        if not match:
            continue
        excluded.add(index)
        same_item_date = extract_date_from_text(item.text[match.end():])
        if same_item_date and requested_date is None:
            requested_date = same_item_date
        nearby = []
        for candidate_index, candidate in enumerate(ordered):
            if (candidate_index == index
                    or candidate.page_number != item.page_number):
                continue
            vertical_distance = abs(candidate.y - item.y)
            if vertical_distance > 55:
                continue
            candidate_date = extract_date_from_text(candidate.text)
            if not candidate_date:
                continue
            horizontal_penalty = 0 if candidate.x >= item.x else 500
            score = (vertical_distance * 10
                     + abs(candidate.x - item.x) + horizontal_penalty)
            nearby.append((score, candidate_index, candidate_date))
        if nearby:
            _, candidate_index, candidate_date = min(nearby)
            excluded.add(candidate_index)
            if requested_date is None:
                requested_date = candidate_date
        if requested_date is None and index + 1 < len(ordered):
            next_item = ordered[index + 1]
            if next_item.page_number == item.page_number:
                next_date = extract_date_from_text(next_item.text)
                if next_date:
                    excluded.add(index + 1)
                    requested_date = next_date
    return requested_date, excluded


def resolve_date_from_positioned_text(
    items: Sequence[PositionedText],
) -> tuple[Optional[str], Optional[str], bool]:
    ordered = sort_positioned_text(items)
    requested_date, request_indexes = requested_date_candidates(ordered)
    for index, item in enumerate(ordered):
        if index in request_indexes:
            continue
        if REQUESTED_DATE_LABEL_PATTERN.search(item.text):
            continue
        date_value = extract_date_from_text(item.text)
        if date_value:
            return (date_value,
                    f"Form HTML/{item.source}: Date available in form", True)
    if requested_date:
        return (requested_date,
                "Form HTML/OCR: Request Date or Requested Date", True)
    return None, None, False


def get_date_requested_value(
    form_name_has_date: bool, form_date_found: bool
) -> Optional[str]:
    if form_name_has_date:
        return None
    return "Yes" if form_date_found else "No"


# ===========================================================================
# 11) Filter candidates by date range
# ===========================================================================
def record_created_date(record: dict[str, Any]) -> Optional[date]:
    value = parse_form_name(get_form_name(record))["Created Date"]
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


def filter_candidate_forms(
    all_forms: Sequence[dict[str, Any]], parameters: ExtractionParameters
) -> list[dict[str, Any]]:
    """Keep in-range name-dates and forms whose name has no date."""
    candidates = []
    seen = set()
    for item in all_forms:
        name_date = record_created_date(item)
        if name_date is not None and not (
            parameters.start_date <= name_date <= parameters.end_date
        ):
            continue
        form_id = get_nested_id(item)
        key = form_id or f"missing-id:{id(item)}"
        if key not in seen:
            seen.add(key)
            candidates.append(item)
    LOGGER.info("Filtered to %s candidate forms in range.", len(candidates))
    return candidates


# ===========================================================================
# 12) Record processing (with controlled re-login on session expiry)
# ===========================================================================
class SessionManager:
    """Owns the Selenium driver + copied requests session.

    Supports a single controlled re-login if the copied session expires
    part-way through a long run.
    """

    def __init__(self) -> None:
        self.driver: Optional[webdriver.Chrome] = None
        self.app_session: Optional[requests.Session] = None
        self._relogins = 0
        self._max_relogins = 1

    def start(self) -> None:
        self.driver = build_driver()
        selenium_login(self.driver)
        self.app_session = build_requests_session_from_driver(self.driver)

    def relogin(self) -> bool:
        if self._relogins >= self._max_relogins or self.driver is None:
            return False
        self._relogins += 1
        LOGGER.warning(
            "Session expired; attempting controlled re-login (%s/%s).",
            self._relogins, self._max_relogins,
        )
        selenium_login(self.driver)
        if self.app_session is not None:
            self.app_session.close()
        self.app_session = build_requests_session_from_driver(self.driver)
        return True

    def close(self) -> None:
        if self.app_session is not None:
            self.app_session.close()
        if self.driver is not None:
            self.driver.quit()


def wait_out_quota(seconds: float) -> None:
    capped = min(seconds, MAX_QUOTA_WAIT_SECONDS)
    LOGGER.warning("GoFormz quota hit; sleeping %.0fs before retry.", capped)
    time.sleep(capped)


def extract_form_details(
    list_record: dict[str, Any], settings: Settings, manager: SessionManager
) -> dict[str, Any]:
    form_id = get_nested_id(list_record)
    form_name = get_form_name(list_record)
    parsed_name = parse_form_name(form_name)
    name_date = parsed_name["Created Date"]
    name_has_date = bool(name_date)
    created_date = name_date
    created_date_source = "Form Name" if name_has_date else None
    form_date_found = False
    errors = []

    if not name_has_date:
        form_url = build_form_url(form_id, settings.app_base)
        if not form_url:
            errors.append("Form URL could not be built.")
        else:
            try:
                session = manager.app_session
                try:
                    rendered_html = fetch_form_html(
                        session, form_url, settings.request_timeout
                    )
                except SessionExpiredError:
                    if manager.relogin():
                        rendered_html = fetch_form_html(
                            manager.app_session, form_url,
                            settings.request_timeout,
                        )
                    else:
                        raise

                soup = BeautifulSoup(rendered_html, "html.parser")
                positioned_items = extract_dom_positioned_text(soup)
                backgrounds = extract_page_backgrounds(
                    soup, settings.app_base
                )
                for page_number, image_url, width, height in backgrounds:
                    try:
                        image_bytes, content_type = download_image(
                            manager.app_session, image_url,
                            settings.request_timeout,
                        )
                        analysis = analyze_image_with_document_intelligence(
                            image_bytes, content_type, settings
                        )
                        positioned_items.extend(
                            extract_ocr_positioned_text(
                                analysis, page_number, width, height
                            )
                        )
                    except GoFormzQuotaError:
                        raise
                    except Exception as exc:
                        errors.append(f"Page {page_number} OCR failed: {exc}")
                resolved_date, source, form_date_found = (
                    resolve_date_from_positioned_text(positioned_items)
                )
                if resolved_date:
                    created_date = resolved_date
                    created_date_source = source
            except GoFormzQuotaError:
                raise
            except Exception as exc:
                errors.append(str(exc))

    date_columns = build_date_columns(created_date)
    date_requested = get_date_requested_value(name_has_date, form_date_found)

    status = "Extracted"
    if not form_id:
        status = "Failed"
        errors.append("Form ID was not found.")
    elif not date_columns["Created Date"]:
        status = "Partial"
        errors.append(
            "No usable date was found in Form Name or rendered form content."
        )

    return {
        "Form ID": form_id,
        "Form Name": form_name,
        "Report Type": parsed_name["Report Type"],
        "Owner": get_owner(list_record),
        "Last Updated": get_last_updated(list_record),
        **date_columns,
        "Form URL": build_form_url(form_id, settings.app_base),
        "Date Requested": date_requested,
        "Created Date Source": created_date_source,
        "Extraction Status": status,
        "Extraction Error": (
            " | ".join(dict.fromkeys(errors)) if errors else None
        ),
    }


def process_forms(
    form_items: Sequence[dict[str, Any]], settings: Settings,
    parameters: ExtractionParameters, manager: SessionManager,
) -> pd.DataFrame:
    records = []
    index = 0
    total = len(form_items)
    while index < total:
        item = form_items[index]
        LOGGER.info("Processing form %s of %s.", index + 1, total)
        try:
            record = extract_form_details(item, settings, manager)
        except GoFormzQuotaError as exc:
            # Quota hit: wait out the window (capped) and retry the same form.
            retry_after = 60.0
            match = re.search(r"Retry-After=(\d+)", str(exc))
            if match:
                retry_after = float(match.group(1))
            wait_out_quota(retry_after)
            continue  # retry same index
        except Exception as exc:
            form_id = get_nested_id(item)
            form_name = get_form_name(item)
            name_has_date = bool(parse_form_name(form_name)["Created Date"])
            record = {
                "Form ID": form_id, "Form Name": form_name,
                "Report Type": None, "Owner": get_owner(item),
                "Last Updated": get_last_updated(item),
                **build_date_columns(None),
                "Form URL": build_form_url(form_id, settings.app_base),
                "Date Requested": None if name_has_date else "No",
                "Created Date Source": None, "Extraction Status": "Failed",
                "Extraction Error": str(exc),
            }
        records.append(record)
        index += 1
        time.sleep(settings.request_delay_seconds)

    if not records:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    dataframe = pd.DataFrame(records)
    dataframe["_created_sort"] = pd.to_datetime(
        dataframe["Created Date"], errors="coerce"
    )
    dataframe["_updated_sort"] = pd.to_datetime(
        dataframe["Last Updated"], errors="coerce", utc=True
    )
    in_range = (
        (dataframe["_created_sort"].dt.date >= parameters.start_date)
        & (dataframe["_created_sort"].dt.date <= parameters.end_date)
    )
    unresolved = dataframe["_created_sort"].isna()
    dataframe = (
        dataframe.loc[in_range | unresolved]
        .sort_values(by=["_created_sort", "_updated_sort"],
                     ascending=[False, False], na_position="last")
        .head(parameters.max_forms)
        .drop(columns=["_created_sort", "_updated_sort"])
        .reset_index(drop=True)
    )
    return dataframe.reindex(columns=OUTPUT_COLUMNS)


# ===========================================================================
# 13) Save + run
# ===========================================================================
def _running_inside_fabric() -> bool:
    """True if the Fabric OneLake mount is present (i.e. a Fabric notebook)."""
    return Path("/lakehouse/default/Files").exists()


def upload_csv_to_onelake(
    csv_bytes: bytes, file_name: str, settings: Settings
) -> str:
    """Upload a CSV to <workspace>/<lakehouse>.Lakehouse/<subpath> via OneLake.

    Raises on any failure so callers can fail the job.
    """
    from azure.identity import ClientSecretCredential
    from azure.storage.filedatalake import DataLakeServiceClient

    tenant_id = settings.fabric_tenant_id
    client_id = settings.fabric_client_id
    client_secret = settings.fabric_client_secret

    if not all([tenant_id, client_id, client_secret]):
        raise RuntimeError(
            "OneLake upload requires TENANT_ID, CLIENT_ID and CLIENT_SECRET."
        )

    lakehouse = settings.fabric_lakehouse_name
    if not lakehouse.endswith(".Lakehouse"):
        lakehouse = f"{lakehouse}.Lakehouse"

    subpath = settings.onelake_target_subpath.strip("/")
    directory_path = f"{lakehouse}/{subpath}"

    credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    service = DataLakeServiceClient(
        account_url=settings.onelake_account_url, credential=credential,
    )
    file_system = service.get_file_system_client(
        settings.fabric_workspace_name
    )

    directory = file_system.get_directory_client(directory_path)
    try:
        directory.create_directory()
    except Exception:
        # Directory already exists — expected on subsequent runs.
        pass

    file_client = directory.create_file(file_name)
    file_client.upload_data(csv_bytes, overwrite=True)

    onelake_path = (
        f"onelake:/{settings.fabric_workspace_name}/"
        f"{directory_path}/{file_name}"
    )
    LOGGER.info("Uploaded to OneLake: %s", onelake_path)
    return onelake_path


def save_results(dataframe: pd.DataFrame, settings: Settings) -> str:
    """Persist results durably. Raises if durable output cannot be created."""
    if dataframe.empty:
        raise RuntimeError("No records were extracted; nothing to save.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")
    file_name = f"goformz_forms_{timestamp}.csv"
    csv_bytes = dataframe.to_csv(
        index=False, encoding="utf-8-sig"
    ).encode("utf-8-sig")

    # Inside a Fabric notebook: write straight to the mount.
    if _running_inside_fabric():
        directory = Path("/lakehouse/default/Files/goformz")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / file_name
        target.write_bytes(csv_bytes)
        LOGGER.info("Saved %s records to %s.", len(dataframe), target)
        return str(target)

    # Container / local: durable OneLake upload is required by default.
    if settings.require_onelake_upload:
        return upload_csv_to_onelake(csv_bytes, file_name, settings)

    # Dev fallback only (REQUIRE_ONELAKE_UPLOAD=false).
    local_dir = Path.home() / "Downloads" / "goformz"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / file_name
    local_path.write_bytes(csv_bytes)
    LOGGER.warning(
        "REQUIRE_ONELAKE_UPLOAD=false — saved locally only to %s.", local_path
    )
    return str(local_path)


def log_summary(dataframe: pd.DataFrame) -> None:
    """Log summary counts only — never the full dataframe."""
    if dataframe.empty:
        LOGGER.info("Extraction summary: total=0")
        return
    status = dataframe["Extraction Status"]
    LOGGER.info(
        "Extraction summary: total=%s extracted=%s partial=%s failed=%s",
        len(dataframe),
        int((status == "Extracted").sum()),
        int((status == "Partial").sum()),
        int((status == "Failed").sum()),
    )


def enforce_quality_gate(dataframe: pd.DataFrame) -> None:
    if dataframe.empty:
        raise RuntimeError("Quality gate failed: no records were extracted.")
    if FAIL_IF_ALL_PARTIAL:
        bad = dataframe["Extraction Status"].isin(["Failed", "Partial"]).sum()
        if bad == len(dataframe):
            raise RuntimeError(
                "Quality gate failed: every record was Failed or Partial."
            )


def run_extraction(
    settings: Settings, parameters: ExtractionParameters
) -> tuple[pd.DataFrame, str]:
    manager = SessionManager()
    try:
        manager.start()
        all_forms = enumerate_forms_from_ui(manager.driver)
        LOGGER.info("Total forms discovered in UI: %s.", len(all_forms))
        candidates = filter_candidate_forms(all_forms, parameters)
        dataframe = process_forms(candidates, settings, parameters, manager)
        log_summary(dataframe)
        enforce_quality_gate(dataframe)
        output_path = save_results(dataframe, settings)
        return dataframe, output_path
    finally:
        manager.close()


def build_settings() -> Settings:
    return Settings(
        app_base=APP_BASE,
        request_timeout=REQUEST_TIMEOUT,
        request_delay_seconds=REQUEST_DELAY_SECONDS,
        document_intelligence_endpoint=AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
        document_intelligence_key=AZURE_DOCUMENT_INTELLIGENCE_KEY,
        document_intelligence_model_id=DOCUMENT_INTELLIGENCE_MODEL_ID,
        document_intelligence_api_version=DOCUMENT_INTELLIGENCE_API_VERSION,
        require_onelake_upload=REQUIRE_ONELAKE_UPLOAD,
        onelake_account_url=ONELAKE_ACCOUNT_URL,
        fabric_workspace_name=FABRIC_WORKSPACE_NAME,
        fabric_lakehouse_name=FABRIC_LAKEHOUSE_NAME,
        onelake_target_subpath=ONELAKE_TARGET_SUBPATH,
        fabric_client_id=FABRIC_CLIENT_ID,
        fabric_client_secret=FABRIC_CLIENT_SECRET,
        fabric_tenant_id=FABRIC_TENANT_ID,
    )


def main(
    start_date: Optional[str] = START_DATE, end_date: Optional[str] = END_DATE,
    max_forms: int = MAX_FORMS, log_level: str = "INFO",
) -> tuple[pd.DataFrame, str]:
    configure_logging(log_level)
    resolved_start, resolved_end = resolve_date_range(start_date, end_date)
    if resolved_start > resolved_end:
        raise ValueError("START_DATE cannot be later than END_DATE.")

    parameters = ExtractionParameters(
        start_date=resolved_start, end_date=resolved_end, max_forms=max_forms
    )
    settings = build_settings()

    LOGGER.info(
        "Extraction range: %s to %s.",
        parameters.start_date, parameters.end_date,
    )
    dataframe, output_path = run_extraction(settings, parameters)
    LOGGER.info("Forms returned: %s.", len(dataframe))
    LOGGER.info("Output path: %s.", output_path)
    return dataframe, output_path


if __name__ == "__main__":
    # Any unhandled exception exits non-zero so the Container Apps Job is
    # correctly marked as Failed.
    main()
