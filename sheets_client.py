import json
import os
import re
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


_client_singleton = None
_sheet_singleton = None


def _client():
    # Re-authorizing from scratch (~1.5s of API round-trips) on every single
    # call was the dominant cost in any request touching more than one
    # sheets_client function — e.g. add_payment_log_entry chains three of
    # them and was taking 10+ seconds almost entirely on repeated auth.
    # gspread's Client renews its own token internally, so this is safe to
    # reuse for the life of the process.
    global _client_singleton
    if _client_singleton is None:
        creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)
        _client_singleton = gspread.authorize(creds)
    return _client_singleton


def _sheet():
    global _sheet_singleton
    if _sheet_singleton is None:
        _sheet_singleton = _client().open_by_key(SPREADSHEET_ID)
    return _sheet_singleton


def avatar_for_name(name: str) -> str:
    h = 0
    for c in name:
        h = (h * 31 + ord(c)) % len(EMOJI_SET)
    return EMOJI_SET[h]


def _to_dmy(s: str) -> str:
    """Convert any YYYY-MM-DD or YYYY.MM.DD or DD.MM.YYYY to DD.MM.YYYY."""
    s = (s or "").strip()
    if not s:
        return s
    parts = s.replace(".", "-").split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    # already DD.MM.YYYY or unrecognized — return with dots
    return s.replace("-", ".")


def _yn(value: str) -> bool:
    return (value or "").strip().lower() == "yes"


def _yn3(value: str) -> str:
    """'yes' / 'no' / '' (not filled in yet — must not be treated as a warning)."""
    v = (value or "").strip().lower()
    return v if v in ("yes", "no") else ""


def _group_id(raw: str) -> str:
    return GROUP_NAME_TO_ID.get((raw or "").strip().lower(), "big")


_SHORT_TERM_LABELS = ("tourist", "short term", "short-term", "краткосрочный", "краткосрочные")


def _contract(raw: str) -> str:
    # "Tourist" is the old label — kept recognized alongside "Short term" (plus
    # a hyphenated spelling and the Russian label the app itself shows, in
    # case someone types what they see in the app straight into the sheet)
    # so rows nobody's gotten around to renaming still read correctly.
    return "tourist" if (raw or "").strip().lower() in _SHORT_TERM_LABELS else "longterm"


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
        if d.weekday() >= 5:  # skip Saturday (5) and Sunday (6)
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

        status_raw = (row.get("Status") or "").strip().lower()
        meals_raw = (row.get("Meals included") or "").strip().lower()
        extra = {
            "active": status_raw != "inactive",
            "address": (row.get("Address") or "").strip(),
            "deposit": (row.get("Deposit") or "").strip(),
            "mealsIncluded": meals_raw in ("yes", "halal"),
            "mealsHalal": meals_raw == "halal",
            "napTime": _yn(row.get("Nap time")),
            "afterSchool": _yn(row.get("After school")),
            "startDate": (row.get("Start date") or "").strip(),
            "clubs": (row.get("Clubs") or "").strip(),
            "clubPaymentType": (row.get("Club payment type") or "").strip(),
            "dayType": (row.get("Day type") or "").strip(),
            "price": (row.get("Price") or "").strip(),
            "paidUntil": _to_dmy((row.get("Paid until") or "").strip()),
            "paidFrom": _to_dmy((row.get("Paid from") or "").strip()),
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


_STATUS_LABEL = {"present": "Present", "absent": "Away", "late": "Away"}


def get_attendance(date: str) -> dict:
    """Read today's marks straight from Ольга's Attendance sheet, so
    anything she edits or deletes there is what the app shows."""
    sh = _sheet()
    try:
        ws = sh.worksheet("Attendance")
    except gspread.WorksheetNotFound:
        return {}
    result = {}
    for row in _rows_as_dicts(ws.get_all_values()):
        if (row.get("Date") or "").strip() != date:
            continue
        name = (row.get("Child") or "").strip()
        if not name:
            continue
        result[name] = "present" if (row.get("Status") or "").strip().lower() == "present" else "absent"
    return result


def upsert_attendance(date: str, statuses: dict) -> None:
    """Mirror today's attendance marks into Ольга's Attendance sheet
    (Date/Child/Group/Status/Notes) so she can see them there too."""
    sh = _sheet()
    ws = sh.worksheet("Attendance")
    values = ws.get_all_values()
    headers = values[0] if values else ["Date", "Child", "Group", "Status", "Notes"]
    col = {h: i for i, h in enumerate(headers)}
    date_i, child_i, group_i, status_i = (
        col.get("Date", 0), col.get("Child", 1), col.get("Group"), col.get("Status", 3),
    )

    groups = {}
    try:
        for row in _rows_as_dicts(sh.worksheet("Children").get_all_values()):
            name = f"{(row.get('First name') or '').strip()} {(row.get('Last name') or '').strip()}".strip()
            if name:
                groups[name] = (row.get("Group") or "").strip()
    except gspread.WorksheetNotFound:
        pass

    existing_row_for = {}
    for i, row in enumerate(values[1:], start=2):
        d = row[date_i] if date_i < len(row) else ""
        c = row[child_i] if child_i < len(row) else ""
        if d == date and c:
            existing_row_for[c] = i

    updates, appends = [], []
    for name, status in statuses.items():
        label = _STATUS_LABEL.get(status, status.capitalize())
        if name in existing_row_for:
            updates.append({
                "range": gspread.utils.rowcol_to_a1(existing_row_for[name], status_i + 1),
                "values": [[label]],
            })
        else:
            managed_width = max(date_i, child_i, group_i or 0, status_i) + 1
            new_row = [""] * managed_width
            new_row[date_i] = date
            new_row[child_i] = name
            if group_i is not None:
                new_row[group_i] = groups.get(name, "")
            new_row[status_i] = label
            appends.append(new_row)

    if updates:
        ws.batch_update(updates)
    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED")


def _parse_dmy(s: str):
    try:
        return datetime.strptime((s or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _parse_log_values(values: list) -> list[dict]:
    """Turn raw Payment log sheet rows into dicts, one shared parser so
    get_payment_log and the write paths below don't each re-read the sheet
    just to recompute the same thing from what they already fetched."""
    if not values:
        return []
    headers = values[0]
    col = {h: i for i, h in enumerate(headers)}
    child_i = col.get("Child")
    if child_i is None:
        return []
    result = []
    for i, row in enumerate(values[1:], start=2):
        if child_i >= len(row) or not row[child_i].strip():
            continue
        def cell(name, _row=row):
            idx = col.get(name)
            return _row[idx].strip() if idx is not None and idx < len(_row) else ""
        result.append({
            "id": i, "child": row[child_i].strip(),
            "tariff": cell("Tariff"), "from": cell("Paid from"), "until": cell("Paid until"),
            "amount": cell("Amount"), "enteredDate": cell("Entered date"),
        })
    return result


def get_payment_log(kid_id: str) -> list[dict]:
    """Every logged payment for one child — an append-only history, so
    short-term kids' separate visits (with gaps between) each keep their
    own row instead of collapsing into a single from/until pair."""
    sh = _sheet()
    values = sh.worksheet("Payment log").get_all_values()
    return [{k: v for k, v in e.items() if k != "child"}
            for e in _parse_log_values(values) if e["child"] == kid_id]


def _best_coverage(entries: list[dict]) -> tuple:
    """Pick whichever entry covers furthest into the future — that's the
    single "current period" Children!Paid from/until caches for the
    overdue badge. Individual gaps between short-term visits live only in
    the log itself, not in this rolled-up pair."""
    best, best_until = None, None
    for e in entries:
        until = _parse_dmy(e["until"])
        if until and (best_until is None or until > best_until):
            best, best_until = e, until
    return (best["from"], best["until"]) if best else ("", "")


def _find_child_row(children_values: list, kid_id: str):
    """Row index + column map for one child, from an already-fetched
    Children!get_all_values() — callers that also need Children data for
    something else (group lookup, etc.) can fetch it once and pass it in
    instead of each doing their own read."""
    headers = children_values[0] if children_values else []
    col = {h: i for i, h in enumerate(headers)}
    first_i, last_i = col.get("First name"), col.get("Last name")
    for i, row in enumerate(children_values[1:], start=2):
        first = row[first_i] if first_i is not None and first_i < len(row) else ""
        last  = row[last_i]  if last_i  is not None and last_i  < len(row) else ""
        if f"{first} {last}".strip() == kid_id:
            return i, row, col
    return None, None, col


def _write_child_coverage(sh, children_values: list, kid_id: str, new_from: str, new_until: str) -> None:
    row_i, row, col = _find_child_row(children_values, kid_id)
    if row_i is None:
        return
    from_i, until_i = col.get("Paid from"), col.get("Paid until")
    updates = []
    if from_i is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(row_i, from_i + 1), "values": [[new_from]]})
    if until_i is not None:
        updates.append({"range": gspread.utils.rowcol_to_a1(row_i, until_i + 1), "values": [[new_until]]})
    if updates:
        sh.worksheet("Children").batch_update(updates)


def add_payment_log_entry(kid_id: str, tariff: str, from_date: str, until_date: str, amount: str) -> dict:
    """Append one payment to the log — the manager types the amount they
    actually received, rather than the app computing it, so a pricing bug
    can't misstate what was collected. Returns the child's recomputed
    current coverage."""
    sh = _sheet()
    ws = sh.worksheet("Payment log")
    values = ws.get_all_values()
    headers = values[0] if values else ["Child", "Group", "Tariff", "Paid from", "Paid until", "Amount", "Entered date"]
    col = {h: i for i, h in enumerate(headers)}

    children_values = sh.worksheet("Children").get_all_values()
    _, child_row, ccol = _find_child_row(children_values, kid_id)
    group_i = ccol.get("Group")
    group = (child_row[group_i] if child_row and group_i is not None and group_i < len(child_row) else "").strip()

    new_from_dmy, new_until_dmy = _to_dmy(from_date), _to_dmy(until_date)
    width = max(col.values(), default=-1) + 1
    new_row = [""] * width
    for field, value in (
        ("Child", kid_id), ("Group", group), ("Tariff", tariff),
        ("Paid from", new_from_dmy), ("Paid until", new_until_dmy),
        ("Amount", amount), ("Entered date", datetime.now().strftime("%d.%m.%Y")),
    ):
        if field in col:
            new_row[col[field]] = value
    ws.append_rows([new_row], value_input_option="USER_ENTERED")

    existing = [e for e in _parse_log_values(values) if e["child"] == kid_id]
    existing.append({"from": new_from_dmy, "until": new_until_dmy})
    new_from, new_until = _best_coverage(existing)
    _write_child_coverage(sh, children_values, kid_id, new_from, new_until)
    return {"paidFrom": new_from, "paidUntil": new_until}


def delete_payment_log_entry(row_id: int) -> dict:
    """Remove one logged payment (a manager correcting a mistake) and
    recompute the owning child's cached coverage from what's left."""
    sh = _sheet()
    ws = sh.worksheet("Payment log")
    values = ws.get_all_values()
    if row_id < 2 or row_id > len(values):
        raise ValueError(f"Payment log row not found: {row_id}")
    entries = _parse_log_values(values)
    target = next((e for e in entries if e["id"] == row_id), None)
    kid_id = target["child"] if target else ""
    ws.delete_rows(row_id)
    if not kid_id:
        return {}

    remaining = [e for e in entries if e["child"] == kid_id and e["id"] != row_id]
    new_from, new_until = _best_coverage(remaining)
    children_values = sh.worksheet("Children").get_all_values()
    _write_child_coverage(sh, children_values, kid_id, new_from, new_until)
    return {"paidFrom": new_from, "paidUntil": new_until}


# ── Child CRUD ────────────────────────────────────────────────────────────────

_GROUP_ID_TO_SHEET = {
    "toddler": "Toddler",
    "middle":  "Middle",
    "big":     "Big",
    "primary": "Primary school",
}

_CHILD_FIELD_MAP = {
    "firstName":     "First name",
    "lastName":      "Last name",
    "group":         "Group",
    "birthday":      "Birthday",
    "contractType":  "Contract type",
    "dayType":       "Day type",
    "price":         "Price",
    "startDate":     "Start date",
    "allergies":     "Allergies / notes",
    "paracetamol":   "Paracetamol",
    "photoConsent":  "Using Photos for Media",
    "adaptation":    "Adaptation",
    "mealsIncluded": "Meals included",
    "napTime":       "Nap time",
    "afterSchool":   "After school",
    "deposit":        "Deposit",
    "paidFrom":       "Paid from",
    "paidUntil":      "Paid until",
    "status":         "Status",
    "parent1Name":   "Parent name (1)",
    "parent1Phone":  "Parent contact (1)",
    "parent2Name":   "Parent name (2)",
    "parent2Phone":  "Parent contact (2)",
    "address":       "Address",
}


def _cell_val(field: str, value) -> str:
    if field == "group":
        return _GROUP_ID_TO_SHEET.get(str(value), str(value))
    if field == "contractType":
        return "Short term" if str(value) == "tourist" else "Long term"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if field in ("paracetamol", "photoConsent"):
        v = str(value).strip().lower()
        return "Yes" if v == "yes" else "No" if v == "no" else ""
    if field == "mealsIncluded":
        v = str(value).strip().lower()
        if v == "halal": return "Halal"
        if v == "yes":   return "Yes"
        return ""
    if field in ("paidUntil", "paidFrom"):
        return _to_dmy(str(value)) if value else ""
    if field == "status":
        return "Inactive" if str(value).strip().lower() == "inactive" else "Active"
    return str(value) if value is not None else ""


def split_club_names(raw: str) -> list[str]:
    """Accept both "Chess + Swimming" (what the app writes) and "Chess, Swimming"
    (what Ольга might type by hand) as the same list."""
    return [c.strip() for c in re.split(r"[+,]", raw) if c.strip()]


_row_cache = {"data": None, "at": 0}
_ROW_CACHE_TTL = 45  # matches _cache's TTL — reused across a burst of club add/removes


def _children_clubs_columns(ws):
    """Row/column lookup for writing the Clubs cell — cached, because Ольга
    adding several kids to a club back-to-back was firing one uncached
    get_all_values() per click and blowing through the Sheets API's
    per-minute read quota (429s). Row *positions* only change when a child
    is added/deleted, which invalidate this below — a stale Clubs *value*
    in the cached snapshot doesn't matter since we only use it for name
    lookup, not to read the current club list."""
    now = time.time()
    if _row_cache["data"] is not None and now - _row_cache["at"] < _ROW_CACHE_TTL:
        return _row_cache["data"]
    values = ws.get_all_values()
    if not values:
        result = (None, None, None, None, None)
    else:
        headers = values[0]
        col = {h: i for i, h in enumerate(headers)}
        result = (values, col.get("First name"), col.get("Last name"), col.get("Clubs"), col)
    _row_cache["data"] = result
    _row_cache["at"] = now
    return result


def get_all_children_clubs() -> dict[str, list[str]]:
    """child full name -> list of club names, from the (cached) Children data —
    this is the single source of truth for club membership, both the app and
    Ольга editing the Clubs column directly end up reading/writing the same cell.
    Goes through get_children()'s cache instead of its own sheet fetch, so this
    no longer adds an extra full-sheet read to every /clubs call."""
    return {c["id"]: split_club_names(c["clubs"]) for c in get_children()}


def get_child_clubs(child_id: str) -> list[str]:
    return get_all_children_clubs().get(child_id, [])


def _write_child_clubs(child_id: str, club_names: list[str]) -> None:
    sh = _sheet()
    ws = sh.worksheet("Children")
    values, first_i, last_i, clubs_i, _ = _children_clubs_columns(ws)
    if not values or clubs_i is None:
        return
    for i, row in enumerate(values[1:], start=2):
        first = row[first_i] if first_i is not None and first_i < len(row) else ""
        last  = row[last_i]  if last_i  is not None and last_i  < len(row) else ""
        if f"{first} {last}".strip() == child_id:
            ws.update_cell(i, clubs_i + 1, " + ".join(club_names))
            _cache["at"] = 0  # so the next get_children()/get_all_children_clubs() sees this write
            return


def add_child_club(child_id: str, club_name: str) -> None:
    names = get_child_clubs(child_id)
    if club_name not in names:
        names.append(club_name)
        _write_child_clubs(child_id, sorted(names))


def remove_child_club(child_id: str, club_name: str) -> None:
    names = [n for n in get_child_clubs(child_id) if n != club_name]
    _write_child_clubs(child_id, names)


def update_child(old_id: str, data: dict) -> None:
    sh = _sheet()
    ws = sh.worksheet("Children")
    values = ws.get_all_values()
    if not values:
        raise ValueError("Children sheet is empty")
    headers = values[0]
    col = {h: i for i, h in enumerate(headers)}
    first_i, last_i = col.get("First name"), col.get("Last name")

    target_row = None
    for i, row in enumerate(values[1:], start=2):
        first = row[first_i] if first_i is not None and first_i < len(row) else ""
        last  = row[last_i]  if last_i  is not None and last_i  < len(row) else ""
        if f"{first} {last}".strip() == old_id:
            target_row = i
            break

    if target_row is None:
        raise ValueError(f"Child not found: {old_id}")

    updates = []
    for field, col_name in _CHILD_FIELD_MAP.items():
        col_idx = col.get(col_name)
        if col_idx is None or field not in data:
            continue
        updates.append({
            "range": gspread.utils.rowcol_to_a1(target_row, col_idx + 1),
            "values": [[_cell_val(field, data[field])]],
        })

    if updates:
        ws.batch_update(updates)
    _cache["at"] = 0


def add_child(data: dict) -> str:
    """Append a new child row. Returns the new child's full_name (= its ID)."""
    sh = _sheet()
    ws = sh.worksheet("Children")
    values = ws.get_all_values()
    if not values:
        raise ValueError("Children sheet is empty")
    headers = values[0]
    col = {h: i for i, h in enumerate(headers)}

    new_row = [""] * len(headers)
    for field, col_name in _CHILD_FIELD_MAP.items():
        col_idx = col.get(col_name)
        if col_idx is None or field not in data:
            continue
        new_row[col_idx] = _cell_val(field, data[field])

    ws.append_rows([new_row], value_input_option="USER_ENTERED")
    _cache["at"] = 0
    _row_cache["at"] = 0

    fn = str(data.get("firstName", "")).strip()
    ln = str(data.get("lastName", "")).strip()
    return f"{fn} {ln}".strip()


def delete_child(child_id: str) -> None:
    sh = _sheet()
    ws = sh.worksheet("Children")
    values = ws.get_all_values()
    if not values:
        raise ValueError("Children sheet is empty")
    headers = values[0]
    col = {h: i for i, h in enumerate(headers)}
    first_i, last_i = col.get("First name"), col.get("Last name")
    for i, row in enumerate(values[1:], start=2):
        first = row[first_i] if first_i is not None and first_i < len(row) else ""
        last  = row[last_i]  if last_i  is not None and last_i  < len(row) else ""
        if f"{first} {last}".strip() == child_id:
            ws.delete_rows(i)
            _cache["at"] = 0
            _row_cache["at"] = 0  # deleting a row shifts every row after it
            return
    raise ValueError(f"Child not found: {child_id}")


def get_staff() -> list[dict]:
    """Read from Staff sheet. Columns: Name, Position, Contract End, Phone, Password.
    Returns password too — caller must strip it before sending to the frontend."""
    sh = _sheet()
    try:
        ws = sh.worksheet("Staff")
    except gspread.WorksheetNotFound:
        return []
    rows = _rows_as_dicts(ws.get_all_values())
    result = []
    for r in rows:
        name = str(r.get("Name", "")).strip()
        if not name:
            continue
        result.append({
            "name":        name,
            "position":    str(r.get("Position", "")).strip(),
            "contractEnd": str(r.get("Contract End", "")).strip(),
            "phone":       str(r.get("Phone", "")).strip(),
            "password":    str(r.get("Password", "")).strip(),
            "rate":        str(r.get("Rate", "")).strip(),
        })
    return result


_STAFF_FIELDS = ["Name", "Position", "Contract End", "Phone", "Password", "Rate"]


def add_staff(data: dict) -> None:
    sh = _sheet()
    ws = sh.worksheet("Staff")
    values = ws.get_all_values()
    headers = values[0] if values else _STAFF_FIELDS
    col = {h: i for i, h in enumerate(headers)}
    new_row = [""] * len(headers)
    for field in _STAFF_FIELDS:
        idx = col.get(field)
        if idx is not None:
            new_row[idx] = str(data.get(field, ""))
    ws.append_rows([new_row], value_input_option="USER_ENTERED")


def update_staff(old_name: str, data: dict) -> None:
    sh = _sheet()
    ws = sh.worksheet("Staff")
    values = ws.get_all_values()
    if not values:
        raise ValueError("Staff sheet is empty")
    headers = values[0]
    col = {h: i for i, h in enumerate(headers)}
    name_i = col.get("Name")
    if name_i is None:
        raise ValueError("Staff sheet has no Name column")
    target_row = None
    for i, row in enumerate(values[1:], start=2):
        if (row[name_i] if name_i < len(row) else "").strip() == old_name:
            target_row = i
            break
    if target_row is None:
        raise ValueError(f"Staff not found: {old_name}")
    updates = []
    for field in _STAFF_FIELDS:
        idx = col.get(field)
        if idx is not None and field in data:
            updates.append({
                "range": gspread.utils.rowcol_to_a1(target_row, idx + 1),
                "values": [[str(data[field])]],
            })
    if updates:
        ws.batch_update(updates)


def delete_staff(name: str) -> None:
    sh = _sheet()
    ws = sh.worksheet("Staff")
    values = ws.get_all_values()
    if not values:
        raise ValueError("Staff sheet is empty")
    headers = values[0]
    col = {h: i for i, h in enumerate(headers)}
    name_i = col.get("Name")
    for i, row in enumerate(values[1:], start=2):
        if (row[name_i] if name_i is not None and name_i < len(row) else "").strip() == name:
            ws.delete_rows(i)
            return
    raise ValueError(f"Staff not found: {name}")


def get_clubs_from_sheets() -> list[dict]:
    sh = _sheet()
    ws = sh.worksheet("Clubs")
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return []
    headers = values[0]
    col = {h: i for i, h in enumerate(headers)}
    clubs = []
    for row in values[1:]:
        def g(h): return row[col[h]].strip() if h in col and col[h] < len(row) else ""
        name_ru = g("Name")
        if not name_ru:
            continue
        price_str = g("Price")
        price = None
        if price_str:
            try:
                price = int(float(price_str.replace(" ", "").replace(",", ".")))
            except ValueError:
                pass
        clubs.append({
            "name_ru": name_ru,
            "name_en": g("Name (EN)"),
            "emoji":   g("Emoji"),
            "days":    g("Days"),
            "time":    g("Time"),
            "price":   price,
        })
    return clubs
