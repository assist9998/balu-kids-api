"""
Phase 3 of the Sheets -> Postgres migration (project_sheets_to_postgres_migration):
every function in sheets_client.py that writes to Google Sheets also calls one of
these to write the same change to Postgres. Sheets stays the only source the app
actually reads from during this phase — a failure here must never surface to the
caller or block the real (Sheets) write, so every public function below swallows
its own errors. The Phase 2 shadow_sync cron (Sheets -> Postgres, every 5 min)
keeps running in parallel and self-heals any drift a bug here might cause.
"""
import os

import psycopg2

_PG_DSN = os.environ.get("BALU_PG_DSN")
_conn = None


def _connection():
    global _conn
    if not _PG_DSN:
        return None
    if _conn is None or _conn.closed:
        try:
            _conn = psycopg2.connect(_PG_DSN, connect_timeout=5)
        except Exception as e:
            print(f"[pg_dual_write] connect failed: {e}")
            return None
    return _conn


def _run(label, fn):
    conn = _connection()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            fn(cur)
        conn.commit()
    except Exception as e:
        print(f"[pg_dual_write] {label} failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# ── Children ──────────────────────────────────────────────────────────────────

_CHILD_COLS = [
    "full_name", "first_name", "last_name", "birthday", "group", "contract_type", "day_type",
    "price", "paid_from", "paid_until", "start_date", "meals_included", "nap_time", "after_school",
    "deposit", "clubs", "club_payment_type", "allergies", "paracetamol", "photo_consent",
    "parent1_name", "parent1_phone", "parent2_name", "parent2_phone", "address", "adaptation", "status",
]


def upsert_child(row: dict) -> None:
    """row keys match _CHILD_COLS; full_name is the stable key the live app already uses."""
    def _do(cur):
        collist = ",".join(f'"{c}"' if c == "group" else c for c in _CHILD_COLS)
        placeholders = ",".join(["%s"] * len(_CHILD_COLS))
        updates = ",".join(f'"{c}"=EXCLUDED."{c}"' if c == "group" else f"{c}=EXCLUDED.{c}"
                            for c in _CHILD_COLS if c != "full_name")
        cur.execute(
            f"""INSERT INTO children ({collist}) VALUES ({placeholders})
                ON CONFLICT (full_name) DO UPDATE SET {updates}""",
            [row.get(c, "") for c in _CHILD_COLS],
        )
    _run("upsert_child", _do)


def rename_child(old_full_name: str, row: dict) -> None:
    """full_name is the primary key but can change on rename — delete the old
    key's row (if the name actually changed) then upsert under the new one."""
    def _do(cur):
        if old_full_name != row.get("full_name"):
            cur.execute("DELETE FROM children WHERE full_name = %s", (old_full_name,))
    _run("rename_child(delete old)", _do)
    upsert_child(row)


def delete_child(full_name: str) -> None:
    _run("delete_child", lambda cur: cur.execute("DELETE FROM children WHERE full_name = %s", (full_name,)))


def update_child_clubs(full_name: str, clubs: str) -> None:
    _run("update_child_clubs", lambda cur: cur.execute(
        "UPDATE children SET clubs = %s WHERE full_name = %s", (clubs, full_name)))


def update_child_coverage(full_name: str, paid_from: str, paid_until: str) -> None:
    _run("update_child_coverage", lambda cur: cur.execute(
        "UPDATE children SET paid_from = %s, paid_until = %s WHERE full_name = %s",
        (paid_from, paid_until, full_name)))


# ── Attendance ────────────────────────────────────────────────────────────────

def upsert_attendance(date: str, child: str, group: str, status: str, marked_by: str) -> None:
    def _do(cur):
        cur.execute(
            """INSERT INTO attendance (date, child, "group", status, marked_by)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (date, child) DO UPDATE SET
                   status=EXCLUDED.status, marked_by=EXCLUDED.marked_by""",
            (date, child, group, status, marked_by),
        )
    _run("upsert_attendance", _do)


def upsert_club_attendance(club_name: str, date: str, child: str, status: str, marked_by: str) -> None:
    def _do(cur):
        cur.execute(
            """INSERT INTO club_attendance (club_name, date, child, status, marked_by)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (club_name, date, child) DO UPDATE SET
                   status=EXCLUDED.status, marked_by=EXCLUDED.marked_by""",
            (club_name, date, child, status, marked_by),
        )
    _run("upsert_club_attendance", _do)


# ── Payment log / Club payment log ─────────────────────────────────────────────
# Neither has a stable id in Sheets (only an ephemeral row position) — inserts are
# unambiguous (append), deletes are matched by full row content instead of a
# position-based id, same lesson learned migrating Gorizont (never trust physical
# row position as a stable identifier).

def insert_payment_log(child, group, tariff, paid_from, paid_until, amount, entered_date, marked_by) -> None:
    _run("insert_payment_log", lambda cur: cur.execute(
        """INSERT INTO payment_log (child, "group", tariff, paid_from, paid_until, amount, entered_date, marked_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (child, group, tariff, paid_from, paid_until, amount, entered_date, marked_by)))


def delete_payment_log(child, tariff, paid_from, paid_until, amount, entered_date, marked_by) -> None:
    def _do(cur):
        cur.execute(
            """DELETE FROM payment_log WHERE id = (
                   SELECT id FROM payment_log
                   WHERE child=%s AND tariff=%s AND paid_from=%s AND paid_until=%s
                     AND amount=%s AND entered_date=%s AND marked_by=%s
                   ORDER BY id LIMIT 1)""",
            (child, tariff, paid_from, paid_until, amount, entered_date, marked_by),
        )
    _run("delete_payment_log", _do)


def insert_club_payment_log(child, group, club_name, paid_from, paid_until, amount, entered_date, marked_by) -> None:
    _run("insert_club_payment_log", lambda cur: cur.execute(
        """INSERT INTO club_payment_log (child, "group", club_name, paid_from, paid_until, amount, entered_date, marked_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (child, group, club_name, paid_from, paid_until, amount, entered_date, marked_by)))


def delete_club_payment_log(child, club_name, paid_from, paid_until, amount, entered_date, marked_by) -> None:
    def _do(cur):
        cur.execute(
            """DELETE FROM club_payment_log WHERE id = (
                   SELECT id FROM club_payment_log
                   WHERE child=%s AND club_name=%s AND paid_from=%s AND paid_until=%s
                     AND amount=%s AND entered_date=%s AND marked_by=%s
                   ORDER BY id LIMIT 1)""",
            (child, club_name, paid_from, paid_until, amount, entered_date, marked_by),
        )
    _run("delete_club_payment_log", _do)


# ── Staff ─────────────────────────────────────────────────────────────────────

def upsert_staff(name, position, contract_end, phone, password, rate) -> None:
    _run("upsert_staff", lambda cur: cur.execute(
        """INSERT INTO staff (name, position, contract_end, phone, password, rate)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (name) DO UPDATE SET
               position=EXCLUDED.position, contract_end=EXCLUDED.contract_end,
               phone=EXCLUDED.phone, password=EXCLUDED.password, rate=EXCLUDED.rate""",
        (name, position, contract_end, phone, password, rate)))


def rename_staff(old_name: str, name, position, contract_end, phone, password, rate) -> None:
    if old_name != name:
        _run("rename_staff(delete old)", lambda cur: cur.execute(
            "DELETE FROM staff WHERE name = %s", (old_name,)))
    upsert_staff(name, position, contract_end, phone, password, rate)


def delete_staff(name: str) -> None:
    _run("delete_staff", lambda cur: cur.execute("DELETE FROM staff WHERE name = %s", (name,)))
