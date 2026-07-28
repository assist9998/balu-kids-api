import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

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


# Same hour the end-of-day sweep runs (see main.py's ATTENDANCE_SWEEP_HOUR_BALI
# — kept here, single source of truth, main.py reads this one).
ATTENDANCE_SWEEP_HOUR_BALI = 22


def _bali_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _should_defer_carryover(date: str) -> bool:
    """A mark made *today*, before the day's sweep hour, might still get
    undone later the same day (a mis-tap corrected a minute later) — so
    compensating for it immediately would need to be un-done too if that
    happens. Simpler to just wait: only decide whether today's absence is
    real once the sweep runs at the end of the day, using whatever the
    final status turns out to be. A date that isn't today (a manager
    fixing last week's history) has no "later today" to wait for, so it
    still compensates immediately, same as before. And a mark made *after*
    the sweep hour, on the sweep's own day, is past the window this is
    protecting against (the sweep already ran once for that day) — treat
    it the old way too rather than silently never compensating it."""
    bali_now = _bali_now()
    return date == bali_now.strftime("%Y-%m-%d") and bali_now.hour < ATTENDANCE_SWEEP_HOUR_BALI


def upsert_attendance(date: str, statuses: dict, marked_by: str = "") -> list[str]:
    """Phase 5: writes straight to Postgres, no Sheets involved — Ольга's
    Attendance sheet is kept current by the push_to_sheets cron instead
    (Postgres -> Sheets, ~10 min), not by the app writing it directly."""
    groups = {}
    for row in pg_dual_write.read_children_rows():
        name = f"{(row.get('First name') or '').strip()} {(row.get('Last name') or '').strip()}".strip()
        if name:
            groups[name] = (row.get("Group") or "").strip()

    for name, status in statuses.items():
        label = _STATUS_LABEL.get(status, status.capitalize())
        pg_dual_write.upsert_attendance(date, name, groups.get(name, ""), label, marked_by)

    if not _should_defer_carryover(date):
        return _apply_day_carryover(date, statuses)
    return []


def run_end_of_day_carryover(date: str) -> list[str]:
    """Called once by the end-of-day sweep (main.py) — evaluates the
    *final* attendance for the whole day at once, so a kid marked absent
    then corrected back to present earlier the same day never generates a
    compensation row in the first place (see _should_defer_carryover)."""
    return _apply_day_carryover(date, get_attendance(date))


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


def upsert_club_attendance(club_name: str, date: str, statuses: dict, marked_by: str = "") -> list[str]:
    """Phase 5: writes straight to Postgres — see upsert_attendance."""
    for name, status in statuses.items():
        label = _STATUS_LABEL.get(status, status.capitalize())
        pg_dual_write.upsert_club_attendance(club_name, date, name, label, marked_by)

    if not _should_defer_carryover(date):
        return _apply_club_day_carryover(club_name, date, statuses)
    return []


def run_end_of_day_club_carryover(club_name: str, date: str) -> list[str]:
    """Club-scoped counterpart to run_end_of_day_carryover — same reasoning,
    called once per club by the end-of-day sweep."""
    return _apply_club_day_carryover(club_name, date, get_club_attendance(club_name, date))


def _parse_dmy(s: str):
    try:
        return datetime.strptime((s or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _next_business_day(d):
    """The garden is closed Sat/Sun, so a carried-over make-good day (see
    _apply_day_carryover) must land on the next day the kid could actually
    attend, not literally the calendar-next day — cov_until ending on a
    Friday used to carry over onto Saturday, a day nobody's ever there for.
    """
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


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
    "today" inside July read as unpaid.

    "Adjacent" allows for a weekend sitting between two entries, not just a
    literal 1-day gap — a carried-over make-good day now lands on the next
    business day (see _next_business_day), which can be up to 3 calendar
    days after the previous entry's until (Fri -> Mon). Without this, that
    entry would score as its own disconnected island instead of extending
    the same run, making paidFrom jump forward to the make-good day itself
    and losing the real start of the child's paid period. A real gap (an
    unpaid month, say) is always bigger than a single weekend, so this
    can't accidentally bridge one of those."""
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
        if start <= _next_business_day(last_until):
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


def add_payment_log_entry(kid_id: str, tariff: str, from_date: str, until_date: str, amount: str,
                           marked_by: str = "", idempotency_key: str | None = None) -> dict:
    """Append one payment to the log — the manager types the amount they
    actually received, rather than the app computing it, so a pricing bug
    can't misstate what was collected. Returns the child's recomputed
    current coverage.

    idempotency_key: if the client retries this same call (e.g. the app got
    backgrounded/killed right as the first attempt's response was coming
    back), the insert is a no-op the second time instead of appending a
    duplicate payment — coverage below is always recomputed fresh from
    whatever's actually in the table, so that's correct either way.

    Phase 5: writes straight to Postgres, no Sheets involved."""
    summary = pg_dual_write.get_child_summary(kid_id)
    group = summary["group"] if summary else ""

    new_from_dmy, new_until_dmy = _to_dmy(from_date), _to_dmy(until_date)
    entered_date = datetime.now().strftime("%d.%m.%Y")
    pg_dual_write.insert_payment_log(kid_id, group, tariff, new_from_dmy, new_until_dmy, amount, entered_date,
                                      marked_by, idempotency_key)

    existing = pg_dual_write.get_payment_log_entries(kid_id)
    new_from, new_until = _best_coverage(existing)
    pg_dual_write.update_child_coverage(kid_id, new_from, new_until)
    return {"paidFrom": new_from, "paidUntil": new_until}


_COMPENSATION_TARIFF = "compensation"


def _apply_day_carryover(date: str, statuses: dict) -> list[str]:
    """A kid on a per-day plan (see _is_day_rate) who's marked absent on a
    weekday inside their already-paid window doesn't lose that day — this
    logs a zero-amount 'compensation' row that pushes their coverage
    forward by one day, same as a real payment would.

    Returns the kid_ids that actually got a new compensation row this call
    (never ones already compensated) — main.py uses this to post a feed
    notification, so Ольга sees *why* someone's paid-until moved without
    having to go look at the payment log herself.

    Phase 5: reads/writes Postgres directly, no Sheets involved — the
    "Marked by" column ("Система: перенос пропуска <date>") still exists in
    the compensation row so Ольга can see why paid-until moved once
    push_to_sheets carries it over; that same marker text is also the
    idempotency check: toggling a day between present/absent any number of
    times must never grant more than one compensation day per missed date.
    """
    try:
        d = datetime.strptime((date or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return []
    if d.weekday() >= 5:
        return []
    missed_dmy = d.strftime("%d.%m.%Y")
    absent_kids = [kid_id for kid_id, status in statuses.items() if status == "absent"]
    if not absent_kids:
        return []

    compensated = []
    for kid_id in absent_kids:
        kid_entries = pg_dual_write.get_payment_log_entries(kid_id)
        cov_from, cov_until = _best_coverage(kid_entries)
        if not cov_until:
            summary = pg_dual_write.get_child_summary(kid_id)
            cov_from = summary["paid_from"] if summary else ""
            cov_until = summary["paid_until"] if summary else ""
        if not _is_day_rate(cov_from, cov_until):
            continue
        cov_from_d, cov_until_d = _parse_dmy(cov_from), _parse_dmy(cov_until)
        if not cov_from_d or not cov_until_d or not (cov_from_d <= d <= cov_until_d):
            continue  # missed day isn't inside a currently-paid window — nothing to carry over

        already_compensated = any(
            e.get("tariff") == _COMPENSATION_TARIFF and missed_dmy in (e.get("markedBy") or "")
            for e in kid_entries
        )
        if already_compensated:
            continue

        extra_dmy = _next_business_day(cov_until_d).strftime("%d.%m.%Y")
        summary = pg_dual_write.get_child_summary(kid_id)
        group = summary["group"] if summary else ""
        entered_date = datetime.now().strftime("%d.%m.%Y")
        marker_text = f"Система: перенос пропуска {missed_dmy}"

        pg_dual_write.insert_payment_log(kid_id, group, _COMPENSATION_TARIFF, extra_dmy, extra_dmy, "0", entered_date, marker_text)

        kid_entries.append({"from": extra_dmy, "until": extra_dmy})
        new_from, new_until = _best_coverage(kid_entries)
        pg_dual_write.update_child_coverage(kid_id, new_from, new_until)
        compensated.append(kid_id)
    return compensated


def delete_payment_log_entry(row_id: int) -> dict:
    """Remove one logged payment (a manager correcting a mistake) and
    recompute the owning child's cached coverage from what's left.

    Phase 5: row_id is a Postgres payment_log.id — the only id the frontend
    has ever been given since Phase 4 switched get_payment_log's read to
    Postgres. Straight delete by id, no more Sheets/position handling at
    all."""
    entry = pg_dual_write.get_payment_log_entry_by_id(row_id)
    if entry is None:
        raise ValueError(f"Payment log row not found: {row_id}")
    kid_id = entry["child"]
    pg_dual_write.delete_payment_log_by_id(row_id)

    remaining = pg_dual_write.get_payment_log_entries(kid_id)
    new_from, new_until = _best_coverage(remaining)
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


def add_club_payment_log_entry(kid_id: str, club_name: str, from_date: str, until_date: str, amount: str,
                                marked_by: str = "", idempotency_key: str | None = None) -> dict:
    """Append one club payment. Returns this kid's recomputed coverage for
    *this* club only — never touches Children!Paid from/until, which is
    the garden-only cache.

    idempotency_key: see add_payment_log_entry — a safely-retried duplicate
    call is a no-op instead of a second payment row.

    Phase 5: writes straight to Postgres, no Sheets involved."""
    summary = pg_dual_write.get_child_summary(kid_id)
    group = summary["group"] if summary else ""

    new_from_dmy, new_until_dmy = _to_dmy(from_date), _to_dmy(until_date)
    entered_date = datetime.now().strftime("%d.%m.%Y")
    pg_dual_write.insert_club_payment_log(kid_id, group, club_name, new_from_dmy, new_until_dmy, amount, entered_date,
                                           marked_by, idempotency_key)

    existing = [e for e in pg_dual_write.get_club_payment_log_entries(club_name) if e["child"] == kid_id]
    new_from, new_until = _best_coverage(existing)
    return {"paidFrom": new_from, "paidUntil": new_until}


def _apply_club_day_carryover(club_name: str, date: str, statuses: dict) -> list[str]:
    """Same idea as _apply_day_carryover, scoped to one club — a kid on a
    per-day club plan who misses a weekday inside their paid window gets a
    zero-amount 'compensation' row in Club payment log instead of losing
    it. Unlike the garden, there's no Children coverage cache to update
    afterwards (see add_club_payment_log_entry) — the log itself is the
    only source of truth for a kid's per-club coverage.

    Returns the kid_ids actually compensated this call, same as
    _apply_day_carryover — see there for why.

    Phase 5: reads/writes Postgres directly, no Sheets involved."""
    try:
        d = datetime.strptime((date or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return []
    if d.weekday() >= 5:
        return []
    scheduled_weekdays = _club_scheduled_weekdays(club_name)
    # A kid marked absent on a day this club doesn't even meet on (a
    # mis-tap, or historical data from before the club's real schedule was
    # entered correctly) never warrants compensation — there was no session
    # to have missed in the first place. Only enforced when the schedule is
    # actually known (see _club_scheduled_weekdays) so a Sheets hiccup
    # degrades to the old "any weekday" behavior instead of blocking
    # legitimate compensation.
    if scheduled_weekdays and d.weekday() not in scheduled_weekdays:
        return []
    missed_dmy = d.strftime("%d.%m.%Y")
    absent_kids = [kid_id for kid_id, status in statuses.items() if status == "absent"]
    if not absent_kids:
        return []

    entries = pg_dual_write.get_club_payment_log_entries(club_name)

    compensated = []
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

        extra_dmy = _next_club_day(cov_until_d, scheduled_weekdays).strftime("%d.%m.%Y")
        summary = pg_dual_write.get_child_summary(kid_id)
        group = summary["group"] if summary else ""
        entered_date = datetime.now().strftime("%d.%m.%Y")
        marker_text = f"Система: перенос пропуска {missed_dmy}"

        pg_dual_write.insert_club_payment_log(kid_id, group, club_name, extra_dmy, extra_dmy, "0", entered_date, marker_text)
        compensated.append(kid_id)
    return compensated


def delete_club_payment_log_entry(row_id: int) -> dict:
    """Remove one logged club payment and recompute that kid's coverage
    for that same club from what's left.

    Phase 5: row_id is a Postgres club_payment_log.id — straight delete by
    id, no more Sheets/position handling."""
    entry = pg_dual_write.get_club_payment_log_entry_by_id(row_id)
    if entry is None:
        raise ValueError(f"Club payment log row not found: {row_id}")
    kid_id = entry["child"]
    club_name = entry["club"]
    pg_dual_write.delete_club_payment_log_by_id(row_id)

    remaining = [e for e in pg_dual_write.get_club_payment_log_entries(club_name) if e["child"] == kid_id]
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


def split_club_names(raw: str) -> list[str]:
    """Accept both "Chess + Swimming" (what the app writes) and "Chess, Swimming"
    (what Ольга might type by hand) as the same list."""
    return [c.strip() for c in re.split(r"[+,]", raw) if c.strip()]


def get_all_children_clubs() -> dict[str, list[str]]:
    """child full name -> list of club names, from the (cached) Children data —
    this is the single source of truth for club membership. Goes through
    get_children()'s cache instead of its own fetch, so this doesn't add an
    extra read to every /clubs call."""
    return {c["id"]: split_club_names(c["clubs"]) for c in get_children()}


def get_child_clubs(child_id: str) -> list[str]:
    return get_all_children_clubs().get(child_id, [])


def _write_child_clubs(child_id: str, club_names: list[str]) -> None:
    """Phase 5: writes straight to Postgres, no Sheets involved."""
    joined = " + ".join(club_names)
    pg_dual_write.update_child_clubs(child_id, joined)
    _cache["at"] = 0  # so the next get_children()/get_all_children_clubs() sees this write


def add_child_club(child_id: str, club_name: str) -> None:
    names = get_child_clubs(child_id)
    if club_name not in names:
        names.append(club_name)
        _write_child_clubs(child_id, sorted(names))


def remove_child_club(child_id: str, club_name: str) -> None:
    names = [n for n in get_child_clubs(child_id) if n != club_name]
    _write_child_clubs(child_id, names)


def update_child(old_id: str, data: dict) -> None:
    """Phase 5: writes straight to Postgres, no Sheets involved. full_name
    is the primary key but can change on rename (data may include new
    firstName/lastName) — pg_dual_write.rename_child handles that."""
    existing = pg_dual_write.get_child_full_row(old_id)
    if existing is None:
        raise ValueError(f"Child not found: {old_id}")
    row = dict(existing)
    for field, col_name in _CHILD_FIELD_MAP.items():
        if field not in data:
            continue
        pg_col = _CHILD_SHEET_TO_PG_COL.get(col_name)
        if pg_col:
            row[pg_col] = _cell_val(field, data[field])
    row["full_name"] = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
    pg_dual_write.rename_child(old_id, row)
    _cache["at"] = 0


def add_child(data: dict) -> str:
    """Insert a new child directly into Postgres. Returns the new child's
    full_name (= its id)."""
    fn = str(data.get("firstName", "")).strip()
    ln = str(data.get("lastName", "")).strip()
    full_name = f"{fn} {ln}".strip()
    if not full_name:
        return full_name

    row = {pg_col: "" for pg_col in _CHILD_SHEET_TO_PG_COL.values()}
    row["full_name"] = full_name
    for field, col_name in _CHILD_FIELD_MAP.items():
        if field not in data:
            continue
        pg_col = _CHILD_SHEET_TO_PG_COL.get(col_name)
        if pg_col:
            row[pg_col] = _cell_val(field, data[field])

    pg_dual_write.upsert_child(row)
    _cache["at"] = 0
    return full_name


def delete_child(child_id: str) -> None:
    """Phase 5: writes straight to Postgres, no Sheets involved."""
    if pg_dual_write.delete_child(child_id) == 0:
        raise ValueError(f"Child not found: {child_id}")
    _cache["at"] = 0


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
    """Phase 5: writes straight to Postgres, no Sheets involved."""
    name = str(data.get("Name", "")).strip()
    if not name:
        raise ValueError("Staff needs a name")
    pg_dual_write.upsert_staff(
        name, data.get("Position", ""), data.get("Contract End", ""),
        data.get("Phone", ""), data.get("Password", ""), data.get("Rate", ""),
    )


def update_staff(old_name: str, data: dict) -> None:
    """Phase 5: writes straight to Postgres, no Sheets involved. Name is the
    primary key but can change on rename (data may include a new Name) —
    pg_dual_write.rename_staff handles that."""
    existing = pg_dual_write.get_staff_row(old_name)
    if existing is None:
        raise ValueError(f"Staff not found: {old_name}")
    merged = dict(existing)
    for field in _STAFF_FIELDS:
        if field in data:
            merged[field] = str(data[field])
    pg_dual_write.rename_staff(
        old_name, merged["Name"], merged["Position"], merged["Contract End"],
        merged["Phone"], merged["Password"], merged["Rate"],
    )


def delete_staff(name: str) -> None:
    """Phase 5: writes straight to Postgres, no Sheets involved."""
    if pg_dual_write.delete_staff(name) == 0:
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


_WEEKDAY_ABBR_EN = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAY_ABBR_RU = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def _parse_club_weekdays(days_str: str) -> set:
    """'Days' cell from the Clubs sheet, e.g. "Ср / Wed" or "Пн, Ср / Mon, Wed"
    (or just one side, if there's no "/" at all) -> the actual weekday
    numbers (Mon=0..Sun=6) this club meets on. Prefers the English half
    (after "/") since 3-letter abbreviations there are unambiguous; falls
    back to the Russian half if English didn't match anything (a manually
    typed cell missing the "/" separator, say)."""
    if not days_str:
        return set()
    parts = days_str.split("/")
    en_part = parts[1] if len(parts) > 1 else parts[0]
    result = {_WEEKDAY_ABBR_EN[key] for token in en_part.split(",")
              if (key := token.strip().lower()[:3]) in _WEEKDAY_ABBR_EN}
    if result:
        return result
    ru_part = parts[0]
    return {_WEEKDAY_ABBR_RU[key] for token in ru_part.split(",")
            if (key := token.strip().lower()) in _WEEKDAY_ABBR_RU}


def _next_club_day(d, scheduled_weekdays: set):
    """Next date after d whose weekday the club actually meets on — a
    make-good day for a missed club session must itself be a day the club
    runs, not just any non-weekend day (the garden's _next_business_day is
    the wrong helper here: a kid compensated onto a day their club doesn't
    even meet on would still just lose the session). Falls back to
    _next_business_day if scheduled_weekdays is empty (unknown schedule —
    a Sheets hiccup, say), so this can't loop forever with nothing to
    match, and degrades to the same behavior as before this fix existed."""
    if not scheduled_weekdays:
        return _next_business_day(d)
    nxt = d + timedelta(days=1)
    while nxt.weekday() not in scheduled_weekdays:
        nxt += timedelta(days=1)
    return nxt


def _club_scheduled_weekdays(club_name: str) -> set:
    """Which weekdays (Mon=0..Sun=6) club_name (the English name) actually
    meets on, per the live Clubs sheet — empty set if the sheet is
    unreachable or the club/its Days cell can't be found, so a lookup
    failure degrades to "don't restrict" rather than silently blocking
    something that would otherwise have been fine."""
    try:
        clubs = get_clubs_from_sheets()
    except Exception:
        return set()
    club = next((c for c in clubs if c["name_en"] == club_name), None)
    if not club:
        return set()
    return _parse_club_weekdays(club["days"])


_STAFF_ATTENDANCE_HEADER = ["Date", "Staff", "Status", "Arrival time", "Note", "Transfer", "Overtime"]


def push_staff_attendance_rows(rows: list[dict]) -> None:
    """One-way mirror only — StaffAttendance lives in this app's own SQLite
    (never in Sheets), so unlike everything else in this file there's no
    read path back from here at all, not even a fallback one. Ольга gets a
    read-only window onto it; editing always happens in the app.

    This sheet is entirely ours (nothing hand-maintained lives alongside
    it, unlike Children/Payment log) — but still writes new-data-first,
    stale-tail-cleared-second, both in one batch_update call, same as
    push_to_sheets.py. A plain clear() then a separate update() has a real
    gap between the two network calls: if this process dies or Sheets
    hiccups in between, Ольга would see an empty sheet (just the header)
    until the next push happens to succeed, instead of the sheet simply
    keeping whatever it last had."""
    sh = _sheet()
    try:
        ws = sh.worksheet("Staff attendance")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Staff attendance", rows=1000, cols=len(_STAFF_ATTENDANCE_HEADER))
        ws.update([_STAFF_ATTENDANCE_HEADER], "A1")

    rows_sorted = sorted(rows, key=lambda r: (r.get("date") or "", r.get("staff_name") or ""))
    body = [[
        r.get("date") or "", r.get("staff_name") or "", r.get("status") or "",
        r.get("arrival_time") or "", r.get("note") or "",
        (r.get("transfer") or ""), "Yes" if r.get("extra") else "",
    ] for r in rows_sorted]

    ncols = len(_STAFF_ATTENDANCE_HEADER)
    new_last_row = 1 + len(body)
    current_last_row = len(ws.get_all_values())
    updates = [{"range": f"A1:G{new_last_row}", "values": [_STAFF_ATTENDANCE_HEADER] + body}]
    if current_last_row > new_last_row:
        blank_rows = current_last_row - new_last_row
        updates.append({
            "range": f"A{new_last_row + 1}:G{current_last_row}",
            "values": [[""] * ncols for _ in range(blank_rows)],
        })
    ws.batch_update(updates)
