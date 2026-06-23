import json
import os
import time
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

EMOJI_SET = ["🧒", "👦", "👧", "👶"]
GROUP_IDS = {"toddler", "middle", "big", "primary"}
GROUP_NAME_TO_ID = {
    "toddler": "toddler",
    "middle": "middle",
    "big": "big",
    "primary school": "primary",
}

_cache = {"children": None, "at": 0}
_CACHE_TTL = 45  # seconds


def _client():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)
    return gspread.authorize(creds)


def _sheet():
    return _client().open_by_key(SPREADSHEET_ID)


def avatar_for_name(name: str) -> str:
    h = 0
    for c in name:
        h = (h * 31 + ord(c)) % len(EMOJI_SET)
    return EMOJI_SET[h]


def _yn(value: str) -> bool:
    return (value or "").strip().lower() == "yes"


def _yn3(value: str) -> str:
    """'yes' / 'no' / '' (not filled in yet — must not be treated as a warning)."""
    v = (value or "").strip().lower()
    return v if v in ("yes", "no") else ""


def _group_id(raw: str) -> str:
    return GROUP_NAME_TO_ID.get((raw or "").strip().lower(), "big")


def _contract(raw: str) -> str:
    return "tourist" if (raw or "").strip().lower() == "tourist" else "longterm"


def compute_rate(full_name: str, attendance_rows: list[dict]) -> int:
    cutoff = datetime.now() - timedelta(days=30)
    total = 0
    present = 0
    for row in attendance_rows:
        if row.get("Child", "").strip() != full_name:
            continue
        date_str = row.get("Date", "").strip()
        if not date_str:
            continue
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if d < cutoff:
            continue
        total += 1
        if row.get("Status", "").strip().lower() == "present":
            present += 1
    if total == 0:
        return 0
    return round(present / total * 100)


def _rows_as_dicts(values: list[list[str]]) -> list[dict]:
    if not values:
        return []
    headers = values[0]
    rows = []
    for raw in values[1:]:
        if not any(c.strip() for c in raw):
            continue
        row = {headers[i]: (raw[i] if i < len(raw) else "") for i in range(len(headers))}
        rows.append(row)
    return rows


def get_children() -> list[dict]:
    now = time.time()
    if _cache["children"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["children"]

    sh = _sheet()
    child_rows = _rows_as_dicts(sh.worksheet("Children").get_all_values())
    try:
        attendance_rows = _rows_as_dicts(sh.worksheet("Attendance").get_all_values())
    except gspread.WorksheetNotFound:
        attendance_rows = []

    children = []
    for row in child_rows:
        first = (row.get("First name") or "").strip()
        last = (row.get("Last name") or "").strip()
        if not first and not last:
            continue
        full_name = f"{first} {last}".strip()

        meals_raw = (row.get("Meals included") or "").strip().lower()
        extra = {
            "address": (row.get("Address") or "").strip(),
            "deposit": (row.get("Deposit") or "").strip(),
            "mealsIncluded": meals_raw in ("yes", "halal"),
            "mealsHalal": meals_raw == "halal",
            "napTime": _yn(row.get("Nap time")),
            "afterSchool": _yn(row.get("After school")),
            "startDate": (row.get("Start date") or "").strip(),
            "clubs": (row.get("Clubs") or "").strip(),
            "clubPaymentType": (row.get("Club payment type") or "").strip(),
        }

        children.append({
            "id": full_name,
            "ru": full_name,
            "en": full_name,
            "emoji": avatar_for_name(full_name),
            "group": _group_id(row.get("Group")),
            "contract": _contract(row.get("Contract type")),
            "dob": (row.get("Birthday") or "").strip(),
            "allergyRu": "",
            "allergyEn": (row.get("Allergies / notes") or "").strip(),
            "noteRu": "",
            "noteEn": "",
            "paracetamol": _yn3(row.get("Paracetamol")),
            "photoConsent": _yn3(row.get("Using Photos for Media")),
            "adaptation": _yn(row.get("Adaptation")),
            "parent1Name": (row.get("Parent name (1)") or "").strip(),
            "parent1Phone": (row.get("Parent contact (1)") or "").strip(),
            "parent2Name": (row.get("Parent name (2)") or "").strip(),
            "parent2Phone": (row.get("Parent contact (2)") or "").strip(),
            "rate": compute_rate(full_name, attendance_rows),
            **extra,
        })

    _cache["children"] = children
    _cache["at"] = now
    return children
