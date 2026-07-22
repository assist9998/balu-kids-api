import json
import os
import re
import time
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

import pg_dual_write

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
    """Phase 4, module 4 (the last and most central one — see
    project_sheets_to_postgres_migration): tries Postgres first for both the
    Children rows and the Attendance rows compute_rate needs, falling back
    to the exact old Sheets reads on any failure. Everything below this
    point — every derived field — runs unchanged regardless of which source
    provided the raw rows."""
    now = time.time()
    if _cache["children"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["children"]

    try:
        child_rows = pg_dual_write.read_children_rows()
        attendance_rows = pg_dual_write.read_attendance_rows_for_rate()
    except Exception as e:
        print(f"[phase4] get_children: Postgres read failed, falling back to Sheets: {e}")
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
    """Read today's marks — normally straight from Ольга's Attendance sheet,
    so anything she edits or deletes there is what the app shows.

    Phase 4 of the Sheets -> Postgres migration, module 2 (see
    get_attendance_history for module 1): tries Postgres first, falls back
    to the exact old Sheets code on any failure. A direct edit she makes in
    the sheet itself can lag up to one shadow_sync cycle (~5 min) before
    showing here — an accepted, temporary tradeoff on the way to Sheets
    becoming a pure mirror (no direct editing at all) once the migration
    finishes, same end state as Gorizont's."""
    try:
        return pg_dual_write.read_attendance(date)
    except Exception as e:
        print(f"[phase4] get_attendance: Postgres read failed, falling back to Sheets: {e}")

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


def get_attendance_history(kid_id: str) -> dict:
    """Every recorded day for one kid, across the whole Attendance sheet —
    used by the per-kid attendance calendar, which shows history rather
    than one date at a time like get_attendance does.

    Phase 4 of the Sheets -> Postgres migration: this is the first read
    switched over (see pg_dual_write.read_attendance_history) — it's the
    most isolated one, a single kid's calendar view that nothing else reads
    or depends on. Any Postgres hiccup falls back to the exact old
    Sheets-reading code below, so this can't make the feature worse, only
    occasionally slower."""
    try:
        return pg_dual_write.read_attendance_history(kid_id)
    except Exception as e:
        print(f"[phase4] get_attendance_history: Postgres read failed, falling back to Sheets: {e}")

    sh = _sheet()
    try:
        ws = sh.worksheet("Attendance")
    except gspread.WorksheetNotFound:
        return {}
    result = {}
    for row in _rows_as_dicts(ws.get_all_values()):
        if (row.get("Child") or "").strip() != kid_id:
            continue
        date = (row.get("Date") or "").strip()
        if not date:
            continue
        result[date] = "present" if (row.get("Status") or "").strip().lower() == "present" else "absent"
    return result


def get_club_attendance_history(club_name: str, kid_id: str) -> dict:
    """Same idea as get_attendance_history, but for one club's own sheet
    (e.g. "Chess attendance") instead of the shared garden Attendance
    sheet — a kid's club calendar should show whether they showed up to
    THAT club, not whether they were at the garden that day. Same Phase 4
    Postgres-first-with-fallback treatment as get_attendance_history."""
    try:
        return pg_dual_write.read_club_attendance_history(club_name, kid_id)
    except Exception as e:
        print(f"[phase4] get_club_attendance_history: Postgres read failed, falling back to Sheets: {e}")

    sh = _sheet()
    try:
        ws = sh.worksheet(f"{club_name} attendance")
    except gspread.WorksheetNotFound:
        return {}
    result = {}
    for row in _rows_as_dicts(ws.get_all_values()):
        if (row.get("Child") or "").strip() != kid_id:
            continue
        date = (row.get("Date") or "").strip()
        if not date:
            continue
        result[date] = "present" if (row.get("Status") or "").strip().lower() == "present" else "absent"
    return result


def upsert_attendance(date: str, statuses: dict, marked_by: str = "") -> None:
    """Mirror today's attendance marks into Ольга's Attendance sheet
    (Date/Child/Group/Status/Marked by/Notes) so she can see them there too."""
    sh = _sheet()
    ws = sh.worksheet("Attendance")
    values = ws.get_all_values()
    headers = values[0] if values else ["Date", "Child", "Group", "Status", "Marked by", "Notes"]
    col = {h: i for i, h in enumerate(headers)}
    date_i, child_i, group_i, status_i, marker_i = (
        col.get("Date", 0), col.get("Child", 1), col.get("Group"), col.get("Status", 3), col.get("Marked by"),
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

    updates, appends, labels = [], [], {}
    for name, status in statuses.items():
        label = _STATUS_LABEL.get(status, status.capitalize())
        labels[name] = label
        if name in existing_row_for:
            row_i = existing_row_for[name]
            updates.append({
                "range": gspread.utils.rowcol_to_a1(row_i, status_i + 1),
                "values": [[label]],
            })
            if marker_i is not None:
                updates.append({
                    "range": gspread.utils.rowcol_to_a1(row_i, marker_i + 1),
                    "values": [[marked_by]],
                })
        else:
            managed_width = max(date_i, child_i, group_i or 0, status_i, marker_i or 0) + 1
            new_row = [""] * managed_width
            new_row[date_i] = date
            new_row[child_i] = name
            if group_i is not None:
                new_row[group_i] = groups.get(name, "")
            new_row[status_i] = label
            if marker_i is not None:
                new_row[marker_i] = marked_by
            appends.append(new_row)

    if updates:
        ws.batch_update(updates)
    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED")

    for name, label in labels.items():
        pg_dual_write.upsert_attendance(date, name, groups.get(name, ""), label, marked_by)

    _apply_day_carryover(date, statuses)


def get_club_attendance(club_name: str, date: str) -> dict:
    """Same idea as get_attendance, but one sheet per club (e.g. "Chess
    attendance") instead of one shared sheet — clubs don't have a Group
    column, and each club's roster is small enough that a dedicated tab
    is easier for Ольга to read than a shared sheet with a Club column."""
    sh = _sheet()
    try:
        ws = sh.worksheet(f"{club_name} attendance")
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


def upsert_club_attendance(club_name: str, date: str, statuses: dict, marked_by: str = "") -> None:
    sh = _sheet()
    ws = sh.worksheet(f"{club_name} attendance")
    values = ws.get_all_values()
    headers = values[0] if values else ["Date", "Child", "Status", "Marked by"]
    col = {h: i for i, h in enumerate(headers)}
    date_i, child_i, status_i, marker_i = (
        col.get("Date", 0), col.get("Child", 1), col.get("Status", 2), col.get("Marked by"),
    )

    existing_row_for = {}
    for i, row in enumerate(values[1:], start=2):
        d = row[date_i] if date_i < len(row) else ""
        c = row[child_i] if child_i < len(row) else ""
        if d == date and c:
            existing_row_for[c] = i

    updates, appends, labels = [], [], {}
    for name, status in statuses.items():
        label = _STATUS_LABEL.get(status, status.capitalize())
        labels[name] = label
        if name in existing_row_for:
            row_i = existing_row_for[name]
            updates.append({
                "range": gspread.utils.rowcol_to_a1(row_i, status_i + 1),
                "values": [[label]],
            })
            if marker_i is not None:
                updates.append({
                    "range": gspread.utils.rowcol_to_a1(row_i, marker_i + 1),
                    "values": [[marked_by]],
                })
        else:
            managed_width = max(date_i, child_i, status_i, marker_i or 0) + 1
            new_row = [""] * managed_width
            new_row[date_i] = date
            new_row[child_i] = name
            new_row[status_i] = label
            if marker_i is not None:
                new_row[marker_i] = marked_by
            appends.append(new_row)

    if updates:
        ws.batch_update(updates)
    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED")

    for name, label in labels.items():
        pg_dual_write.upsert_club_attendance(club_name, date, name, label, marked_by)

    _apply_club_day_carryover(club_name, date, statuses)


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
            "markedBy": cell("Marked by"),
        })
    return result


def get_payment_log(kid_id: str) -> list[dict]:
    """Every logged payment for one child — an append-only history, so
    short-term kids' separate visits (with gaps between) each keep their
    own row instead of collapsing into a single from/until pair.

    Phase 4, module 3: tries Postgres first (id here is then Postgres's own
    serial, not a sheet row position — see delete_payment_log_entry, which
    handles both cases), falls back to Sheets on any failure."""
    try:
        return pg_dual_write.get_payment_log_entries(kid_id)
    except Exception as e:
        print(f"[phase4] get_payment_log: Postgres read failed, falling back to Sheets: {e}")
    sh = _sheet()
    values = sh.worksheet("Payment log").get_all_values()
    return [{k: v for k, v in e.items() if k != "child"}
            for e in _parse_log_values(values) if e["child"] == kid_id]


def _best_coverage(entries: list[dict]) -> tuple:
    """Merge overlapping/adjacent entries into contiguous covered ranges,
    then cache whichever range covers *today* — so a one-off future
    payment with a real gap before it (e.g. a stray extra day booked
    weeks out) doesn't clobber the "from" of an earlier period that's
    still what's actually covering right now. Falls back to whichever
    range reaches furthest into the future if none of them cover today
    (paid ahead for a period that hasn't started yet). Confirmed live:
    logging month (01.07-31.07) then an isolated day with a gap
    (05.08) used to jump paidFrom to 05.08, making an already-paid
    "today" inside July read as unpaid."""
    ranges = []
    for e in entries:
        until = _parse_dmy(e["until"])
        if not until:
            continue
        start = _parse_dmy(e["from"]) or until
        ranges.append((start, until))
    if not ranges:
        return ("", "")
    ranges.sort()
    merged = [ranges[0]]
    for start, until in ranges[1:]:
        last_start, last_until = merged[-1]
        if start <= last_until + timedelta(days=1):
            merged[-1] = (last_start, max(last_until, until))
        else:
            merged.append((start, until))
    today = datetime.now().date()
    covering_today = next((r for r in merged if r[0] <= today <= r[1]), None)
    best = covering_today or max(merged, key=lambda r: r[1])
    return (best[0].strftime("%d.%m.%Y"), best[1].strftime("%d.%m.%Y"))


_DAY_RATE_MAX_DAYS = 26  # [ПРАВИЛО] paid period > 26 days = monthly plan, <= 26 = per-day plan


def _is_day_rate(from_dmy: str, until_dmy: str) -> bool:
    """A kid's current paid period counts as a per-day plan (as opposed to
    monthly) purely by how many days it spans — see _DAY_RATE_MAX_DAYS."""
    f, u = _parse_dmy(from_dmy), _parse_dmy(until_dmy)
    if not f or not u:
        return False
    return 0 < (u - f).days + 1 <= _DAY_RATE_MAX_DAYS


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


def add_payment_log_entry(kid_id: str, tariff: str, from_date: str, until_date: str, amount: str, marked_by: str = "") -> dict:
    """Append one payment to the log — the manager types the amount they
    actually received, rather than the app computing it, so a pricing bug
    can't misstate what was collected. Returns the child's recomputed
    current coverage."""
    sh = _sheet()
    ws = sh.worksheet("Payment log")
    values = ws.get_all_values()
    headers = values[0] if values else ["Child", "Group", "Tariff", "Paid from", "Paid until", "Amount", "Entered date", "Marked by"]
    col = {h: i for i, h in enumerate(headers)}

    children_values = sh.worksheet("Children").get_all_values()
    _, child_row, ccol = _find_child_row(children_values, kid_id)
    group_i = ccol.get("Group")
    group = (child_row[group_i] if child_row and group_i is not None and group_i < len(child_row) else "").strip()

    new_from_dmy, new_until_dmy = _to_dmy(from_date), _to_dmy(until_date)
    entered_date = datetime.now().strftime("%d.%m.%Y")
    width = max(col.values(), default=-1) + 1
    new_row = [""] * width
    for field, value in (
        ("Child", kid_id), ("Group", group), ("Tariff", tariff),
        ("Paid from", new_from_dmy), ("Paid until", new_until_dmy),
        ("Amount", amount), ("Entered date", entered_date),
        ("Marked by", marked_by),
    ):
        if field in col:
            new_row[col[field]] = value
    ws.append_rows([new_row], value_input_option="USER_ENTERED")
    pg_dual_write.insert_payment_log(kid_id, group, tariff, new_from_dmy, new_until_dmy, amount, entered_date, marked_by)

    existing = [e for e in _parse_log_values(values) if e["child"] == kid_id]
    existing.append({"from": new_from_dmy, "until": new_until_dmy})
    new_from, new_until = _best_coverage(existing)
    _write_child_coverage(sh, children_values, kid_id, new_from, new_until)
    pg_dual_write.update_child_coverage(kid_id, new_from, new_until)
    return {"paidFrom": new_from, "paidUntil": new_until}


_COMPENSATION_TARIFF = "compensation"


def _apply_day_carryover(date: str, statuses: dict) -> None:
    """A kid on a per-day plan (see _is_day_rate) who's marked absent on a
    weekday inside their already-paid window doesn't lose that day — this
    logs a zero-amount 'compensation' row that pushes their coverage
    forward by one day, same as a real payment would.

    Written to the sheet (not just computed) so Ольга can see in Payment
    log *why* a kid's paid-until moved without her collecting anything —
    the existing "Marked by" column, always blank for entries she enters
    herself, says "Система: перенос пропуска <date>" for these. That same
    marker is also the idempotency check: toggling a day between
    present/absent any number of times must never grant more than one
    compensation day per missed date.
    """
    try:
        d = datetime.strptime((date or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return
    if d.weekday() >= 5:
        return
    missed_dmy = d.strftime("%d.%m.%Y")
    absent_kids = [kid_id for kid_id, status in statuses.items() if status == "absent"]
    if not absent_kids:
        return

    sh = _sheet()
    ws = sh.worksheet("Payment log")
    values = ws.get_all_values()
    headers = values[0] if values else [
        "Child", "Group", "Tariff", "Paid from", "Paid until", "Amount", "Entered date", "Marked by",
    ]
    col = {h: i for i, h in enumerate(headers)}
    entries = _parse_log_values(values)
    children_values = sh.worksheet("Children").get_all_values()

    new_rows = []
    pg_rows = []  # mirrors new_rows for the Postgres dual-write, since it needs plain values, not column indices
    coverage_updates = {}  # kid_id -> (new_from, new_until), applied after the log is written
    for kid_id in absent_kids:
        _, child_row, ccol = _find_child_row(children_values, kid_id)
        kid_entries = [e for e in entries if e["child"] == kid_id]
        cov_from, cov_until = _best_coverage(kid_entries)
        if not cov_until:
            from_i, until_i = ccol.get("Paid from"), ccol.get("Paid until")
            cov_from = (child_row[from_i] if child_row and from_i is not None and from_i < len(child_row) else "").strip()
            cov_until = (child_row[until_i] if child_row and until_i is not None and until_i < len(child_row) else "").strip()
        if not _is_day_rate(cov_from, cov_until):
            continue
        cov_from_d, cov_until_d = _parse_dmy(cov_from), _parse_dmy(cov_until)
        if not cov_from_d or not cov_until_d or not (cov_from_d <= d <= cov_until_d):
            continue  # missed day isn't inside a currently-paid window — nothing to carry over

        already_compensated = any(
            e["child"] == kid_id and e.get("tariff") == _COMPENSATION_TARIFF
            and missed_dmy in (e.get("markedBy") or "")
            for e in entries
        )
        if already_compensated:
            continue

        extra_dmy = (cov_until_d + timedelta(days=1)).strftime("%d.%m.%Y")
        group_i = ccol.get("Group")
        group = (child_row[group_i] if child_row and group_i is not None and group_i < len(child_row) else "").strip()

        entered_date = datetime.now().strftime("%d.%m.%Y")
        marker_text = f"Система: перенос пропуска {missed_dmy}"
        width = max(col.values(), default=-1) + 1
        row = [""] * width
        for field, value in (
            ("Child", kid_id), ("Group", group), ("Tariff", _COMPENSATION_TARIFF),
            ("Paid from", extra_dmy), ("Paid until", extra_dmy), ("Amount", "0"),
            ("Entered date", entered_date),
            ("Marked by", marker_text),
        ):
            if field in col:
                row[col[field]] = value
        new_rows.append(row)
        pg_rows.append((kid_id, group, extra_dmy, entered_date, marker_text))

        kid_entries.append({"from": extra_dmy, "until": extra_dmy})
        coverage_updates[kid_id] = _best_coverage(kid_entries)

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    for kid_id, group, extra_dmy, entered_date, marker_text in pg_rows:
        pg_dual_write.insert_payment_log(kid_id, group, _COMPENSATION_TARIFF, extra_dmy, extra_dmy, "0", entered_date, marker_text)
    for kid_id, (new_from, new_until) in coverage_updates.items():
        _write_child_coverage(sh, children_values, kid_id, new_from, new_until)
        pg_dual_write.update_child_coverage(kid_id, new_from, new_until)


def delete_payment_log_entry(row_id: int) -> dict:
    """Remove one logged payment (a manager correcting a mistake) and
    recompute the owning child's cached coverage from what's left.

    Phase 4, module 3: row_id is normally a Postgres payment_log.id now
    (get_payment_log reads from there — see above). Look it up by that id
    first; the matching sheet row is found by full content, never by
    position. Falls back to the pre-Phase-4 position-based path only if
    nothing matches in Postgres, meaning the read that produced this id had
    itself fallen back to Sheets (row_id is then a real sheet position)."""
    sh = _sheet()
    ws = sh.worksheet("Payment log")
    values = ws.get_all_values()
    entries = _parse_log_values(values)

    pg_entry = None
    try:
        pg_entry = pg_dual_write.get_payment_log_entry_by_id(row_id)
    except Exception as e:
        print(f"[phase4] delete_payment_log_entry: Postgres lookup failed: {e}")

    if pg_entry is not None:
        kid_id = pg_entry["child"]
        target = next(
            (e for e in entries if e["child"] == pg_entry["child"] and e["tariff"] == pg_entry["tariff"]
             and e["from"] == pg_entry["from"] and e["until"] == pg_entry["until"]
             and e["amount"] == pg_entry["amount"] and e["enteredDate"] == pg_entry["enteredDate"]
             and e["markedBy"] == pg_entry["markedBy"]),
            None,
        )
        if target:
            ws.delete_rows(target["id"])
        pg_dual_write.delete_payment_log_by_id(row_id)
        remaining = [e for e in entries if e["child"] == kid_id and (target is None or e["id"] != target["id"])]
    else:
        if row_id < 2 or row_id > len(values):
            raise ValueError(f"Payment log row not found: {row_id}")
        target = next((e for e in entries if e["id"] == row_id), None)
        kid_id = target["child"] if target else ""
        ws.delete_rows(row_id)
        if target:
            pg_dual_write.delete_payment_log(
                target["child"], target["tariff"], target["from"], target["until"],
                target["amount"], target["enteredDate"], target["markedBy"],
            )
        if not kid_id:
            return {}
        remaining = [e for e in entries if e["child"] == kid_id and e["id"] != row_id]

    new_from, new_until = _best_coverage(remaining)
    children_values = sh.worksheet("Children").get_all_values()
    _write_child_coverage(sh, children_values, kid_id, new_from, new_until)
    pg_dual_write.update_child_coverage(kid_id, new_from, new_until)
    return {"paidFrom": new_from, "paidUntil": new_until}


def _parse_club_log_values(values: list) -> list[dict]:
    """Same idea as _parse_log_values, plus a "club" field — a kid can be
    in more than one club with a different paid-through date in each, so
    entries need to be scoped by club as well as by child."""
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
            "id": i, "child": row[child_i].strip(), "club": cell("Club"),
            "from": cell("Paid from"), "until": cell("Paid until"),
            "amount": cell("Amount"), "enteredDate": cell("Entered date"),
            "markedBy": cell("Marked by"),
        })
    return result


def get_club_payment_log(club_name: str) -> list[dict]:
    """Every logged payment for one club, across all its members — the
    frontend fetches this once per club and derives each kid's own
    paid-through date from it client-side, since nothing here is cached
    on the Children sheet (a kid's other club may have a different date).

    Phase 4, module 3: same Postgres-first-with-fallback treatment as
    get_payment_log — see delete_club_payment_log_entry for how the id this
    returns is handled either way."""
    try:
        return pg_dual_write.get_club_payment_log_entries(club_name)
    except Exception as e:
        print(f"[phase4] get_club_payment_log: Postgres read failed, falling back to Sheets: {e}")
    sh = _sheet()
    values = sh.worksheet("Club payment log").get_all_values()
    return [{k: v for k, v in e.items() if k != "club"}
            for e in _parse_club_log_values(values) if e["club"] == club_name]


def add_club_payment_log_entry(kid_id: str, club_name: str, from_date: str, until_date: str, amount: str, marked_by: str = "") -> dict:
    """Append one club payment. Returns this kid's recomputed coverage for
    *this* club only — never touches Children!Paid from/until, which is
    the garden-only cache."""
    sh = _sheet()
    ws = sh.worksheet("Club payment log")
    values = ws.get_all_values()
    headers = values[0] if values else ["Child", "Group", "Club", "Paid from", "Paid until", "Amount", "Entered date", "Marked by"]
    col = {h: i for i, h in enumerate(headers)}

    children_values = sh.worksheet("Children").get_all_values()
    _, child_row, ccol = _find_child_row(children_values, kid_id)
    group_i = ccol.get("Group")
    group = (child_row[group_i] if child_row and group_i is not None and group_i < len(child_row) else "").strip()

    new_from_dmy, new_until_dmy = _to_dmy(from_date), _to_dmy(until_date)
    entered_date = datetime.now().strftime("%d.%m.%Y")
    width = max(col.values(), default=-1) + 1
    new_row = [""] * width
    for field, value in (
        ("Child", kid_id), ("Group", group), ("Club", club_name),
        ("Paid from", new_from_dmy), ("Paid until", new_until_dmy),
        ("Amount", amount), ("Entered date", entered_date),
        ("Marked by", marked_by),
    ):
        if field in col:
            new_row[col[field]] = value
    ws.append_rows([new_row], value_input_option="USER_ENTERED")
    pg_dual_write.insert_club_payment_log(kid_id, group, club_name, new_from_dmy, new_until_dmy, amount, entered_date, marked_by)

    existing = [e for e in _parse_club_log_values(values) if e["child"] == kid_id and e["club"] == club_name]
    existing.append({"from": new_from_dmy, "until": new_until_dmy})
    new_from, new_until = _best_coverage(existing)
    return {"paidFrom": new_from, "paidUntil": new_until}


def _apply_club_day_carryover(club_name: str, date: str, statuses: dict) -> None:
    """Same idea as _apply_day_carryover, scoped to one club — a kid on a
    per-day club plan who misses a weekday inside their paid window gets a
    zero-amount 'compensation' row in Club payment log instead of losing
    it. Unlike the garden, there's no Children-sheet cache to update
    afterwards (see add_club_payment_log_entry) — the log itself is the
    only source of truth for a kid's per-club coverage."""
    try:
        d = datetime.strptime((date or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return
    if d.weekday() >= 5:
        return
    missed_dmy = d.strftime("%d.%m.%Y")
    absent_kids = [kid_id for kid_id, status in statuses.items() if status == "absent"]
    if not absent_kids:
        return

    sh = _sheet()
    ws = sh.worksheet("Club payment log")
    values = ws.get_all_values()
    headers = values[0] if values else [
        "Child", "Group", "Club", "Paid from", "Paid until", "Amount", "Entered date", "Marked by",
    ]
    col = {h: i for i, h in enumerate(headers)}
    entries = [e for e in _parse_club_log_values(values) if e["club"] == club_name]
    children_values = sh.worksheet("Children").get_all_values()

    new_rows = []
    pg_rows = []
    for kid_id in absent_kids:
        kid_entries = [e for e in entries if e["child"] == kid_id]
        cov_from, cov_until = _best_coverage(kid_entries)
        if not _is_day_rate(cov_from, cov_until):
            continue
        cov_from_d, cov_until_d = _parse_dmy(cov_from), _parse_dmy(cov_until)
        if not cov_from_d or not cov_until_d or not (cov_from_d <= d <= cov_until_d):
            continue  # missed day isn't inside a currently-paid window — nothing to carry over

        # Club payment log has no Tariff column (unlike the garden Payment
        # log), so the missed-date text embedded in "Marked by" is the
        # only idempotency key available here.
        already_compensated = any(
            e["child"] == kid_id and missed_dmy in (e.get("markedBy") or "")
            for e in entries
        )
        if already_compensated:
            continue

        extra_dmy = (cov_until_d + timedelta(days=1)).strftime("%d.%m.%Y")
        _, child_row, ccol = _find_child_row(children_values, kid_id)
        group_i = ccol.get("Group")
        group = (child_row[group_i] if child_row and group_i is not None and group_i < len(child_row) else "").strip()

        entered_date = datetime.now().strftime("%d.%m.%Y")
        marker_text = f"Система: перенос пропуска {missed_dmy}"
        width = max(col.values(), default=-1) + 1
        row = [""] * width
        for field, value in (
            ("Child", kid_id), ("Group", group), ("Club", club_name),
            ("Paid from", extra_dmy), ("Paid until", extra_dmy), ("Amount", "0"),
            ("Entered date", entered_date),
            ("Marked by", marker_text),
        ):
            if field in col:
                row[col[field]] = value
        new_rows.append(row)
        pg_rows.append((kid_id, group, extra_dmy, entered_date, marker_text))

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    for kid_id, group, extra_dmy, entered_date, marker_text in pg_rows:
        pg_dual_write.insert_club_payment_log(kid_id, group, club_name, extra_dmy, extra_dmy, "0", entered_date, marker_text)


def delete_club_payment_log_entry(row_id: int) -> dict:
    """Remove one logged club payment and recompute that kid's coverage
    for that same club from what's left.

    Same Phase 4, module 3 dual-path handling as delete_payment_log_entry —
    row_id is normally a Postgres club_payment_log.id, with a fallback to
    the old position-based path if nothing matches there."""
    sh = _sheet()
    ws = sh.worksheet("Club payment log")
    values = ws.get_all_values()
    entries = _parse_club_log_values(values)

    pg_entry = None
    try:
        pg_entry = pg_dual_write.get_club_payment_log_entry_by_id(row_id)
    except Exception as e:
        print(f"[phase4] delete_club_payment_log_entry: Postgres lookup failed: {e}")

    if pg_entry is not None:
        kid_id = pg_entry["child"]
        club_name = pg_entry["club"]
        target = next(
            (e for e in entries if e["child"] == pg_entry["child"] and e["club"] == pg_entry["club"]
             and e["from"] == pg_entry["from"] and e["until"] == pg_entry["until"]
             and e["amount"] == pg_entry["amount"] and e["enteredDate"] == pg_entry["enteredDate"]
             and e["markedBy"] == pg_entry["markedBy"]),
            None,
        )
        if target:
            ws.delete_rows(target["id"])
        pg_dual_write.delete_club_payment_log_by_id(row_id)
        remaining = [e for e in entries if e["child"] == kid_id and e["club"] == club_name
                     and (target is None or e["id"] != target["id"])]
    else:
        if row_id < 2 or row_id > len(values):
            raise ValueError(f"Club payment log row not found: {row_id}")
        target = next((e for e in entries if e["id"] == row_id), None)
        kid_id = target["child"] if target else ""
        club_name = target["club"] if target else ""
        ws.delete_rows(row_id)
        if target:
            pg_dual_write.delete_club_payment_log(
                target["child"], target["club"], target["from"], target["until"],
                target["amount"], target["enteredDate"], target["markedBy"],
            )
        if not kid_id:
            return {}
        remaining = [e for e in entries if e["child"] == kid_id and e["club"] == club_name and e["id"] != row_id]

    new_from, new_until = _best_coverage(remaining)
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


_CHILD_SHEET_TO_PG_COL = {
    "First name": "first_name", "Last name": "last_name", "Birthday": "birthday",
    "Group": "group", "Contract type": "contract_type", "Day type": "day_type",
    "Price": "price", "Paid from": "paid_from", "Paid until": "paid_until",
    "Start date": "start_date", "Meals included": "meals_included", "Nap time": "nap_time",
    "After school": "after_school", "Deposit": "deposit", "Clubs": "clubs",
    "Club payment type": "club_payment_type", "Allergies / notes": "allergies",
    "Paracetamol": "paracetamol", "Using Photos for Media": "photo_consent",
    "Parent name (1)": "parent1_name", "Parent contact (1)": "parent1_phone",
    "Parent name (2)": "parent2_name", "Parent contact (2)": "parent2_phone",
    "Address": "address", "Adaptation": "adaptation", "Status": "status",
}


def _child_row_to_pg_dict(full_name: str, row: list, col: dict) -> dict:
    """Same row + header map sheets_client already has in memory after a
    write, reshaped into the plain dict pg_dual_write.upsert_child expects."""
    d = {"full_name": full_name}
    for sheet_col, pg_col in _CHILD_SHEET_TO_PG_COL.items():
        idx = col.get(sheet_col)
        d[pg_col] = (row[idx] if idx is not None and idx < len(row) else "") or ""
    return d


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
            joined = " + ".join(club_names)
            ws.update_cell(i, clubs_i + 1, joined)
            _cache["at"] = 0  # so the next get_children()/get_all_children_clubs() sees this write
            pg_dual_write.update_child_clubs(child_id, joined)
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

    updated_row = list(row)  # row = the matched sheet row, still bound from the loop above
    updates = []
    for field, col_name in _CHILD_FIELD_MAP.items():
        col_idx = col.get(col_name)
        if col_idx is None or field not in data:
            continue
        cell_value = _cell_val(field, data[field])
        updates.append({
            "range": gspread.utils.rowcol_to_a1(target_row, col_idx + 1),
            "values": [[cell_value]],
        })
        while len(updated_row) <= col_idx:
            updated_row.append("")
        updated_row[col_idx] = cell_value

    if updates:
        ws.batch_update(updates)
    _cache["at"] = 0

    new_first_i, new_last_i = col.get("First name"), col.get("Last name")
    new_first = updated_row[new_first_i] if new_first_i is not None and new_first_i < len(updated_row) else ""
    new_last = updated_row[new_last_i] if new_last_i is not None and new_last_i < len(updated_row) else ""
    new_full_name = f"{new_first} {new_last}".strip()
    pg_dual_write.rename_child(old_id, _child_row_to_pg_dict(new_full_name, updated_row, col))


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
    full_name = f"{fn} {ln}".strip()
    if full_name:
        pg_dual_write.upsert_child(_child_row_to_pg_dict(full_name, new_row, col))
    return full_name


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
            pg_dual_write.delete_child(child_id)
            return
    raise ValueError(f"Child not found: {child_id}")


def get_staff() -> list[dict]:
    """Read from Staff sheet. Columns: Name, Position, Contract End, Phone, Password.
    Returns password too — caller must strip it before sending to the frontend
    (this also feeds /auth/login's password check directly).

    Phase 4, module 4: tries Postgres first, falls back to Sheets on any
    failure — same treatment as get_children."""
    try:
        rows = pg_dual_write.read_staff_rows()
    except Exception as e:
        print(f"[phase4] get_staff: Postgres read failed, falling back to Sheets: {e}")
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
    name = str(data.get("Name", "")).strip()
    if name:
        pg_dual_write.upsert_staff(
            name, data.get("Position", ""), data.get("Contract End", ""),
            data.get("Phone", ""), data.get("Password", ""), data.get("Rate", ""),
        )


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
    updated_row = list(row)  # row = the matched sheet row, still bound from the loop above
    updates = []
    for field in _STAFF_FIELDS:
        idx = col.get(field)
        if idx is not None and field in data:
            value = str(data[field])
            updates.append({
                "range": gspread.utils.rowcol_to_a1(target_row, idx + 1),
                "values": [[value]],
            })
            while len(updated_row) <= idx:
                updated_row.append("")
            updated_row[idx] = value
    if updates:
        ws.batch_update(updates)

    def _get(field):
        idx = col.get(field)
        return updated_row[idx] if idx is not None and idx < len(updated_row) else ""
    pg_dual_write.rename_staff(
        old_name, _get("Name"), _get("Position"), _get("Contract End"),
        _get("Phone"), _get("Password"), _get("Rate"),
    )


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
            pg_dual_write.delete_staff(name)
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
