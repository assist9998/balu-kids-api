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
            "dayType": (row.get("Day type") or "").strip(),
            "price": (row.get("Price") or "").strip(),
            "paidUntil": (row.get("Paid until") or "").strip(),
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


_MONTH_DAYS = {"jun": 30, "jul": 31, "aug": 31, "sep": 30}


def _update_paid_until(sh, month: str, deltas: list) -> None:
    """Roll the "Paid until" date forward in Children, but only by however
    many days are genuinely *new* in this save (see upsert_payments, which
    computes each kid's delta against what was already on record before
    calling this) — re-saving an unchanged "Paid" checkbox must not add
    days again, or every idle re-save would push the date further out.

    Tourists only attend days they've prepaid, so a late top-up starts
    counting from today (max(today, old_date) + new_days) — unpaid time
    just meant the kid wasn't there.

    Long-term kids buy a whole calendar month in one go — the number of
    days added is however many days are in the month tab being paid for
    (30/31), not a flat 30, so paying tab-by-tab lines "Paid until" up
    with real month boundaries instead of drifting. A late payment still
    settles a debt rather than buying fresh days: it always adds onto the
    *old* date, even if that's still in the past — that surfaces as
    still-overdue if more than one cycle is owed."""
    ws = sh.worksheet("Children")
    values = ws.get_all_values()
    if not values:
        return
    headers = values[0]
    col = {h: i for i, h in enumerate(headers)}
    first_i, last_i = col.get("First name"), col.get("Last name")
    contract_i, paiduntil_i = col.get("Contract type"), col.get("Paid until")
    if first_i is None or last_i is None or paiduntil_i is None:
        return

    row_by_name = {}
    for i, row in enumerate(values[1:], start=2):
        first = row[first_i] if first_i < len(row) else ""
        last = row[last_i] if last_i < len(row) else ""
        name = f"{first} {last}".strip()
        if name:
            row_by_name[name] = i

    today = datetime.now().date()
    updates = []
    for d in deltas:
        row_i = row_by_name.get(d["kid_id"])
        if row_i is None:
            continue
        row_vals = values[row_i - 1]
        contract = (row_vals[contract_i] if contract_i is not None and contract_i < len(row_vals) else "").strip().lower()
        is_tourist = contract == "tourist"
        n_days = d["new_days"] if is_tourist else (_MONTH_DAYS.get(month, 30) if d["newly_paid"] else 0)
        if n_days <= 0:
            continue

        cur_raw = (row_vals[paiduntil_i] if paiduntil_i < len(row_vals) else "").strip()
        try:
            cur_date = datetime.strptime(cur_raw, "%Y-%m-%d").date()
        except ValueError:
            cur_date = today
        base = max(today, cur_date) if is_tourist else cur_date
        new_date = base + timedelta(days=n_days)

        updates.append({
            "range": gspread.utils.rowcol_to_a1(row_i, paiduntil_i + 1),
            "values": [[new_date.strftime("%Y-%m-%d")]],
        })

    if updates:
        ws.batch_update(updates)


def get_payments(month: str) -> dict:
    """Read garden payment status straight from Ольга's Payments sheet, so
    that anything she edits or deletes there is what the app shows — the
    sheet is the source of truth, not a local copy."""
    sh = _sheet()
    try:
        ws = sh.worksheet("Payments")
    except gspread.WorksheetNotFound:
        return {}
    result = {}
    for row in _rows_as_dicts(ws.get_all_values()):
        if (row.get("Month") or "").strip() != month:
            continue
        name = (row.get("Child") or "").strip()
        if not name:
            continue
        paid = (row.get("Paid") or "").strip().lower() == "yes"
        try:
            days = int((row.get("Days") or "1").strip())
        except ValueError:
            days = 1
        result[name] = {"paid": paid, "days": days}
    return result


def upsert_payments(month: str, rows: list) -> None:
    """Mirror garden payments into Ольга's Payments sheet
    (Month/Child/Group/Amount/Days/Paid/Payment date). Club columns are
    left alone — clubs aren't wired up to this yet."""
    sh = _sheet()
    ws = sh.worksheet("Payments")
    values = ws.get_all_values()
    headers = values[0] if values else ["Month", "Child", "Group", "Amount", "Days", "Paid", "Payment date"]
    col = {h: i for i, h in enumerate(headers)}
    month_i, child_i, group_i, amount_i, days_i, paid_i, pdate_i = (
        col.get("Month", 0), col.get("Child", 1), col.get("Group"),
        col.get("Amount"), col.get("Days"), col.get("Paid"), col.get("Payment date"),
    )

    groups, contracts = {}, {}
    try:
        for row in _rows_as_dicts(sh.worksheet("Children").get_all_values()):
            name = f"{(row.get('First name') or '').strip()} {(row.get('Last name') or '').strip()}".strip()
            if name:
                groups[name] = (row.get("Group") or "").strip()
                contracts[name] = _contract(row.get("Contract type"))
    except gspread.WorksheetNotFound:
        pass

    existing_row_for = {}
    for i, row in enumerate(values[1:], start=2):
        m = row[month_i] if month_i < len(row) else ""
        c = row[child_i] if child_i < len(row) else ""
        if m == month and c:
            existing_row_for[c] = i

    today = datetime.now().strftime("%Y-%m-%d")
    updates, appends, deltas = [], [], []
    for r in rows:
        name, paid, amount, days = r["kid_id"], r["paid"], r["amount"], r.get("days", 1)
        is_tourist = contracts.get(name) == "tourist"
        days_cell = days if is_tourist else ""  # "Days" only means anything for tourists
        paid_label = "Yes" if paid else "No"
        old_days, was_paid = 0, False
        if name in existing_row_for:
            row_i = existing_row_for[name]
            old_row = values[row_i - 1]
            if days_i is not None and days_i < len(old_row):
                try:
                    old_days = int(old_row[days_i])
                except ValueError:
                    old_days = 0
            if paid_i is not None and paid_i < len(old_row):
                was_paid = old_row[paid_i].strip().lower() == "yes"
            if amount_i is not None:
                updates.append({"range": gspread.utils.rowcol_to_a1(row_i, amount_i + 1), "values": [[amount]]})
            if days_i is not None:
                updates.append({"range": gspread.utils.rowcol_to_a1(row_i, days_i + 1), "values": [[days_cell]]})
            if paid_i is not None:
                updates.append({"range": gspread.utils.rowcol_to_a1(row_i, paid_i + 1), "values": [[paid_label]]})
            if paid_i is not None and pdate_i is not None and paid:
                updates.append({"range": gspread.utils.rowcol_to_a1(row_i, pdate_i + 1), "values": [[today]]})
        else:
            managed_width = max(month_i, child_i, group_i or 0, amount_i or 0, days_i or 0, paid_i or 0, pdate_i or 0) + 1
            new_row = [""] * managed_width
            new_row[month_i] = month
            new_row[child_i] = name
            if group_i is not None:
                new_row[group_i] = groups.get(name, "")
            if amount_i is not None:
                new_row[amount_i] = amount
            if days_i is not None:
                new_row[days_i] = days_cell
            if paid_i is not None:
                new_row[paid_i] = paid_label
            if pdate_i is not None and paid:
                new_row[pdate_i] = today
            appends.append(new_row)

        if paid:
            deltas.append({"kid_id": name, "new_days": max(0, days - old_days), "newly_paid": not was_paid})

    if updates:
        ws.batch_update(updates)
    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED")

    _update_paid_until(sh, month, deltas)
