"""
Postgres access layer for balu-kids-api. Phase 5 of the Sheets -> Postgres
migration (project_sheets_to_postgres_migration): Postgres is now the ONLY
place the app writes to — every function below that mutates data RAISES on
failure instead of swallowing it, because there is no longer a Sheets write
to fall back on. A push_to_sheets cron (Postgres -> Sheets, one direction,
~10 min) keeps the sheet readable for Ольга; nothing reads Sheets back into
Postgres anymore (that would let a manual Sheets edit silently overwrite
real data, exactly the failure mode Gorizont's migration ruled out).

Read functions still fall back to Sheets on failure (see sheets_client.py) —
that fallback is defense-in-depth for a Postgres outage, not a peer data
source, and it's fine for it to be up to one push_to_sheets cycle stale.
"""
import os

import psycopg2
import psycopg2.extras
import psycopg2.pool

_PG_DSN = os.environ.get("BALU_PG_DSN")
_pool = None

# FastAPI dispatches every route here as a plain `def`, not `async def`, so
# Starlette runs them on its threadpool — concurrent requests are genuinely
# concurrent OS threads (see the delta-save race fixed earlier for the exact
# same reason on the Sheets side). A single shared psycopg2 connection isn't
# safe for that: two threads issuing execute() at the same moment on one
# connection can interleave on the wire and corrupt the session. A pool
# hands each caller its own connection for the duration of one call, same
# fix already proven on Gorizont's write layer (pg_sync.py).


def _get_pool():
    global _pool
    if not _PG_DSN:
        return None
    if _pool is None:
        try:
            _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, _PG_DSN, connect_timeout=5)
        except Exception as e:
            print(f"[pg] pool init failed: {e}")
            return None
    return _pool


def _require_pool():
    pool = _get_pool()
    if pool is None:
        raise RuntimeError("BALU_PG_DSN not configured or pool unavailable")
    return pool


def _write(fn):
    """Every mutation goes through this — commits on success, rolls back and
    RE-RAISES on failure. Phase 5: Postgres is the only write target, so a
    failure here is a real failure and must reach the caller (ultimately
    surfacing as a 500 to the app, same as any other unhandled backend
    error) rather than being silently absorbed."""
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            result = fn(cur)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ── Children ──────────────────────────────────────────────────────────────────

_CHILD_COLS = [
    "full_name", "first_name", "last_name", "birthday", "group", "contract_type", "day_type",
    "price", "paid_from", "paid_until", "start_date", "meals_included", "nap_time", "after_school",
    "deposit", "clubs", "club_payment_type", "allergies", "paracetamol", "photo_consent",
    "parent1_name", "parent1_phone", "parent2_name", "parent2_phone", "address", "adaptation", "status",
]


def upsert_child(row: dict) -> None:
    """row keys match _CHILD_COLS; full_name is the stable key the app uses as id.
    Only ever called for a brand-new child (see add_child) — raises
    ValueError if that name is already taken instead of silently merging
    into the existing child's row (same guard as rename_child)."""
    def _do(cur):
        full_name = row.get("full_name")
        cur.execute("SELECT 1 FROM children WHERE full_name = %s", (full_name,))
        if cur.fetchone() is not None:
            raise ValueError(f"A child named '{full_name}' already exists")
        collist = ",".join(f'"{c}"' if c == "group" else c for c in _CHILD_COLS)
        placeholders = ",".join(["%s"] * len(_CHILD_COLS))
        updates = ",".join(f'"{c}"=EXCLUDED."{c}"' if c == "group" else f"{c}=EXCLUDED.{c}"
                            for c in _CHILD_COLS if c != "full_name")
        cur.execute(
            f"""INSERT INTO children ({collist}) VALUES ({placeholders})
                ON CONFLICT (full_name) DO UPDATE SET {updates}""",
            [row.get(c, "") for c in _CHILD_COLS],
        )
    _write(_do)


def rename_child(old_full_name: str, row: dict) -> None:
    """full_name is the primary key but can change on rename — delete the old
    key's row first (if the name actually changed), then upsert under the
    new one, both in the same write so a failure can't leave both rows.

    Raises ValueError if the new name already belongs to a DIFFERENT child.
    Before this check existed, ON CONFLICT (full_name) DO UPDATE would
    silently merge the two children's rows under the shared name instead of
    erroring — the exact mechanism behind the Sofia/Sonya incident (two
    children renamed to the same name, one's data overwritten with no
    error). The frontend already blocks this client-side, but that check
    can miss a stale cache or a second tab — this is the real guarantee."""
    def _do(cur):
        new_full_name = row.get("full_name")
        if new_full_name and new_full_name != old_full_name:
            cur.execute("SELECT id FROM children WHERE full_name = %s", (new_full_name,))
            existing = cur.fetchone()
            if existing is not None:
                cur.execute("SELECT id FROM children WHERE full_name = %s", (old_full_name,))
                current = cur.fetchone()
                if current is None or existing[0] != current[0]:
                    raise ValueError(f"A child named '{new_full_name}' already exists")
        if old_full_name != row.get("full_name"):
            cur.execute("DELETE FROM children WHERE full_name = %s", (old_full_name,))
        collist = ",".join(f'"{c}"' if c == "group" else c for c in _CHILD_COLS)
        placeholders = ",".join(["%s"] * len(_CHILD_COLS))
        updates = ",".join(f'"{c}"=EXCLUDED."{c}"' if c == "group" else f"{c}=EXCLUDED.{c}"
                            for c in _CHILD_COLS if c != "full_name")
        cur.execute(
            f"""INSERT INTO children ({collist}) VALUES ({placeholders})
                ON CONFLICT (full_name) DO UPDATE SET {updates}""",
            [row.get(c, "") for c in _CHILD_COLS],
        )
    _write(_do)


def delete_child(full_name: str) -> int:
    """Returns the number of rows actually deleted (0 or 1) — the caller
    (sheets_client.delete_child) turns 0 into a 404-worthy ValueError, same
    as the old Sheets code raised when it couldn't find the row."""
    def _do(cur):
        cur.execute("DELETE FROM children WHERE full_name = %s", (full_name,))
        return cur.rowcount
    return _write(_do)


def update_child_clubs(full_name: str, clubs: str) -> None:
    _write(lambda cur: cur.execute(
        "UPDATE children SET clubs = %s WHERE full_name = %s", (clubs, full_name)))


def update_child_coverage(full_name: str, paid_from: str, paid_until: str) -> None:
    _write(lambda cur: cur.execute(
        "UPDATE children SET paid_from = %s, paid_until = %s WHERE full_name = %s",
        (paid_from, paid_until, full_name)))


def get_child_summary(full_name: str) -> dict | None:
    """Just the fields the payment/carryover write path needs (group label,
    cached coverage) without pulling the full get_children() derivation."""
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT "group", paid_from, paid_until FROM children WHERE full_name = %s', (full_name,))
            row = cur.fetchone()
    finally:
        pool.putconn(conn)
    if row is None:
        return None
    return {"group": row[0] or "", "paid_from": row[1] or "", "paid_until": row[2] or ""}


def get_child_full_row(full_name: str) -> dict | None:
    """All _CHILD_COLS for one child, keyed by Postgres column name (not
    Sheet header) — used by update_child to merge partial edits onto the
    child's current full record before upserting."""
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            collist = ",".join(f'"{c}"' if c == "group" else c for c in _CHILD_COLS)
            cur.execute(f"SELECT {collist} FROM children WHERE full_name = %s", (full_name,))
            row = cur.fetchone()
    finally:
        pool.putconn(conn)
    return dict(row) if row is not None else None


# ── Attendance ────────────────────────────────────────────────────────────────

def upsert_attendance(date: str, child: str, group: str, status: str, marked_by: str) -> None:
    # Resolve to the child's stable numeric id when possible and conflict on
    # (date, child_ref_id) instead of (date, child) — a same-day rename no
    # longer produces a second row under the new name (see the Sofia/Sonya
    # incident: the old text-only key let renamed kids fork into duplicate,
    # conflicting attendance rows for the same day). child is still stored
    # and kept fresh on every write so unconverted read paths keep working.
    # Falls back to the old text-keyed conflict target when the name can't
    # be resolved (child not found — shouldn't happen for a real save, but
    # safer than silently dropping the write).
    def _do(cur):
        cur.execute("SELECT id FROM children WHERE full_name = %s", (child,))
        row = cur.fetchone()
        child_ref_id = row[0] if row else None
        if child_ref_id is not None:
            cur.execute(
                """INSERT INTO attendance (date, child, "group", status, marked_by, child_ref_id)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (date, child_ref_id) DO UPDATE SET
                       child=EXCLUDED.child, "group"=EXCLUDED."group",
                       status=EXCLUDED.status, marked_by=EXCLUDED.marked_by""",
                (date, child, group, status, marked_by, child_ref_id),
            )
        else:
            cur.execute(
                """INSERT INTO attendance (date, child, "group", status, marked_by)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (date, child) DO UPDATE SET
                       status=EXCLUDED.status, marked_by=EXCLUDED.marked_by""",
                (date, child, group, status, marked_by),
            )
    _write(_do)


def upsert_club_attendance(club_name: str, date: str, child: str, status: str, marked_by: str) -> None:
    # Same id-based conflict target as upsert_attendance (see its comment) —
    # a same-day rename must update the same child's club attendance row
    # instead of forking a second one under the new name.
    def _do(cur):
        cur.execute("SELECT id FROM children WHERE full_name = %s", (child,))
        row = cur.fetchone()
        child_ref_id = row[0] if row else None
        if child_ref_id is not None:
            cur.execute(
                """INSERT INTO club_attendance (club_name, date, child, status, marked_by, child_ref_id)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (club_name, date, child_ref_id) DO UPDATE SET
                       child=EXCLUDED.child, status=EXCLUDED.status, marked_by=EXCLUDED.marked_by""",
                (club_name, date, child, status, marked_by, child_ref_id),
            )
        else:
            cur.execute(
                """INSERT INTO club_attendance (club_name, date, child, status, marked_by)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (club_name, date, child) DO UPDATE SET
                       status=EXCLUDED.status, marked_by=EXCLUDED.marked_by""",
                (club_name, date, child, status, marked_by),
            )
    _write(_do)


# ── Payment log / Club payment log ─────────────────────────────────────────────
# Postgres's own serial id is now the ONLY identifier the app ever hands the
# frontend for these (see get_payment_log/get_club_payment_log) — no more
# Sheets row-position ambiguity to worry about on delete.

def insert_payment_log(child, group, tariff, paid_from, paid_until, amount, entered_date, marked_by,
                        idempotency_key=None) -> None:
    # With a key: ON CONFLICT DO NOTHING makes a safely-retried duplicate
    # call (see add_payment_log_entry) a no-op instead of a second row.
    # Without one (older caller, or None) — plain insert, unchanged.
    # child_ref_id (resolved from the child's current name) rides along so
    # get_payment_log_entries can find this row later even after a rename.
    def _do(cur):
        cur.execute("SELECT id FROM children WHERE full_name = %s", (child,))
        row = cur.fetchone()
        child_ref_id = row[0] if row else None
        if idempotency_key:
            cur.execute(
                """INSERT INTO payment_log (child, "group", tariff, paid_from, paid_until, amount, entered_date, marked_by, idempotency_key, child_ref_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (idempotency_key) DO NOTHING""",
                (child, group, tariff, paid_from, paid_until, amount, entered_date, marked_by, idempotency_key, child_ref_id))
        else:
            cur.execute(
                """INSERT INTO payment_log (child, "group", tariff, paid_from, paid_until, amount, entered_date, marked_by, child_ref_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (child, group, tariff, paid_from, paid_until, amount, entered_date, marked_by, child_ref_id))
    _write(_do)


def delete_payment_log_by_id(pg_id: int) -> None:
    _write(lambda cur: cur.execute("DELETE FROM payment_log WHERE id = %s", (pg_id,)))


def insert_club_payment_log(child, group, club_name, paid_from, paid_until, amount, entered_date, marked_by,
                             idempotency_key=None) -> None:
    def _do(cur):
        cur.execute("SELECT id FROM children WHERE full_name = %s", (child,))
        row = cur.fetchone()
        child_ref_id = row[0] if row else None
        if idempotency_key:
            cur.execute(
                """INSERT INTO club_payment_log (child, "group", club_name, paid_from, paid_until, amount, entered_date, marked_by, idempotency_key, child_ref_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (idempotency_key) DO NOTHING""",
                (child, group, club_name, paid_from, paid_until, amount, entered_date, marked_by, idempotency_key, child_ref_id))
        else:
            cur.execute(
                """INSERT INTO club_payment_log (child, "group", club_name, paid_from, paid_until, amount, entered_date, marked_by, child_ref_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (child, group, club_name, paid_from, paid_until, amount, entered_date, marked_by, child_ref_id))
    _write(_do)


def delete_club_payment_log_by_id(pg_id: int) -> None:
    _write(lambda cur: cur.execute("DELETE FROM club_payment_log WHERE id = %s", (pg_id,)))


def get_payment_log_entries(child: str) -> list[dict]:
    # Same id-based match as read_attendance_history — a rename shouldn't
    # hide payments logged under the child's old name.
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, tariff, paid_from, paid_until, amount, entered_date, marked_by
                   FROM payment_log
                   WHERE child_ref_id = (SELECT id FROM children WHERE full_name = %s)
                      OR (child = %s AND child_ref_id IS NULL)
                   ORDER BY id""",
                (child, child),
            )
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    return [
        {"id": r[0], "tariff": r[1] or "", "from": r[2] or "", "until": r[3] or "",
         "amount": r[4] or "", "enteredDate": r[5] or "", "markedBy": r[6] or ""}
        for r in rows
    ]


def get_all_payment_log_entries() -> list[dict]:
    """Every garden payment ever logged, across every child — for the
    Journal tab (Ольга: a flat, newest-first ledger like the Sheets
    Payment log, without needing Sheets to see it)."""
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, child, tariff, paid_from, paid_until, amount, entered_date, marked_by
                   FROM payment_log ORDER BY id DESC"""
            )
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    return [
        {"id": r[0], "child": r[1] or "", "tariff": r[2] or "", "from": r[3] or "", "until": r[4] or "",
         "amount": r[5] or "", "enteredDate": r[6] or "", "markedBy": r[7] or ""}
        for r in rows
    ]


def get_payment_log_entry_by_id(pg_id: int) -> dict | None:
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT child, tariff, paid_from, paid_until, amount, entered_date, marked_by
                   FROM payment_log WHERE id = %s""",
                (pg_id,),
            )
            row = cur.fetchone()
    finally:
        pool.putconn(conn)
    if row is None:
        return None
    return {"child": row[0] or "", "tariff": row[1] or "", "from": row[2] or "", "until": row[3] or "",
            "amount": row[4] or "", "enteredDate": row[5] or "", "markedBy": row[6] or ""}


def get_club_payment_log_entries(club_name: str) -> list[dict]:
    """Keeps "child" in the result (unlike get_payment_log_entries, which
    drops it) — the frontend fetches this once per club and filters by
    child.id on its own side. Because of that client-side match, "child" is
    resolved through child_ref_id to the child's CURRENT name whenever
    possible — a stale name from before a rename would otherwise never
    match any kid.id the frontend has and silently vanish from the list."""
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT cpl.id, COALESCE(c.full_name, cpl.child), cpl.paid_from, cpl.paid_until,
                          cpl.amount, cpl.entered_date, cpl.marked_by
                   FROM club_payment_log cpl LEFT JOIN children c ON c.id = cpl.child_ref_id
                   WHERE cpl.club_name = %s ORDER BY cpl.id""",
                (club_name,),
            )
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    return [
        {"id": r[0], "child": r[1] or "", "from": r[2] or "", "until": r[3] or "",
         "amount": r[4] or "", "enteredDate": r[5] or "", "markedBy": r[6] or ""}
        for r in rows
    ]


def get_club_payment_log_entry_by_id(pg_id: int) -> dict | None:
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT child, club_name, paid_from, paid_until, amount, entered_date, marked_by
                   FROM club_payment_log WHERE id = %s""",
                (pg_id,),
            )
            row = cur.fetchone()
    finally:
        pool.putconn(conn)
    if row is None:
        return None
    return {"child": row[0] or "", "club": row[1] or "", "from": row[2] or "", "until": row[3] or "",
            "amount": row[4] or "", "enteredDate": row[5] or "", "markedBy": row[6] or ""}


# ── Staff ─────────────────────────────────────────────────────────────────────

def upsert_staff(name, position, contract_end, phone, password, rate) -> None:
    _write(lambda cur: cur.execute(
        """INSERT INTO staff (name, position, contract_end, phone, password, rate)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (name) DO UPDATE SET
               position=EXCLUDED.position, contract_end=EXCLUDED.contract_end,
               phone=EXCLUDED.phone, password=EXCLUDED.password, rate=EXCLUDED.rate""",
        (name, position, contract_end, phone, password, rate)))


def rename_staff(old_name: str, name, position, contract_end, phone, password, rate) -> None:
    def _do(cur):
        if old_name != name:
            cur.execute("DELETE FROM staff WHERE name = %s", (old_name,))
        cur.execute(
            """INSERT INTO staff (name, position, contract_end, phone, password, rate)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (name) DO UPDATE SET
                   position=EXCLUDED.position, contract_end=EXCLUDED.contract_end,
                   phone=EXCLUDED.phone, password=EXCLUDED.password, rate=EXCLUDED.rate""",
            (name, position, contract_end, phone, password, rate),
        )
    _write(_do)


def delete_staff(name: str) -> int:
    """Returns the number of rows actually deleted (0 or 1) — see delete_child."""
    def _do(cur):
        cur.execute("DELETE FROM staff WHERE name = %s", (name,))
        return cur.rowcount
    return _write(_do)


def get_staff_row(name: str) -> dict | None:
    """Sheet-label-keyed (Name/Position/...), matching _STAFF_FIELDS — used
    by update_staff to merge a partial edit onto the current full record."""
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT name AS "Name", position AS "Position", contract_end AS "Contract End",
                       phone AS "Phone", password AS "Password", rate AS "Rate"
                FROM staff WHERE name = %s
            """, (name,))
            row = cur.fetchone()
    finally:
        pool.putconn(conn)
    return dict(row) if row is not None else None


# ── Reads ─────────────────────────────────────────────────────────────────────
# All raise on failure — sheets_client.py catches and falls back to reading
# Sheets directly (up to one push_to_sheets cycle stale in the worst case).

def read_attendance(date: str) -> dict:
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT child, status FROM attendance WHERE date = %s", (date,))
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    result = {}
    for child, status in rows:
        if not child:
            continue
        result[child] = "present" if (status or "").strip().lower() == "present" else "absent"
    return result


def read_club_attendance(club_name: str, date: str) -> dict:
    """Club-scoped counterpart to read_attendance — same club_attendance
    table every club writes into (upsert_club_attendance), so this works
    for any club_name without needing a hardcoded list anywhere."""
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT child, status FROM club_attendance WHERE club_name = %s AND date = %s",
                (club_name, date),
            )
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    result = {}
    for child, status in rows:
        if not child:
            continue
        result[child] = "present" if (status or "").strip().lower() == "present" else "absent"
    return result


def read_attendance_history(child: str) -> dict:
    # Match by the child's stable id (resolved from their current name) so a
    # past rename doesn't hide history recorded under an old name — falls
    # back to a plain text match for any row that predates the id link.
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT date, status FROM attendance
                   WHERE child_ref_id = (SELECT id FROM children WHERE full_name = %s)
                      OR (child = %s AND child_ref_id IS NULL)""",
                (child, child),
            )
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    return {date: ("present" if (status or "").strip().lower() == "present" else "absent") for date, status in rows}


def read_club_attendance_history(club_name: str, child: str) -> dict:
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT date, status FROM club_attendance
                   WHERE club_name = %s AND (
                       child_ref_id = (SELECT id FROM children WHERE full_name = %s)
                       OR (child = %s AND child_ref_id IS NULL)
                   )""",
                (club_name, child, child),
            )
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    return {date: ("present" if (status or "").strip().lower() == "present" else "absent") for date, status in rows}


# These return rows shaped exactly like sheets_client._rows_as_dicts() output
# for the corresponding Sheet — a dict keyed by the Sheet's own header names
# (via SQL column aliases) — so get_children()/get_staff()'s existing
# derivation code (_group_id, _contract, _yn, _yn3, compute_rate, ...) runs
# completely unchanged on top of either source.

def read_children_rows() -> list[dict]:
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    first_name AS "First name", last_name AS "Last name", birthday AS "Birthday",
                    "group" AS "Group", contract_type AS "Contract type", day_type AS "Day type",
                    price AS "Price", paid_from AS "Paid from", paid_until AS "Paid until",
                    start_date AS "Start date", meals_included AS "Meals included",
                    nap_time AS "Nap time", after_school AS "After school", deposit AS "Deposit",
                    clubs AS "Clubs", club_payment_type AS "Club payment type",
                    allergies AS "Allergies / notes", paracetamol AS "Paracetamol",
                    photo_consent AS "Using Photos for Media", parent1_name AS "Parent name (1)",
                    parent1_phone AS "Parent contact (1)", parent2_name AS "Parent name (2)",
                    parent2_phone AS "Parent contact (2)", address AS "Address",
                    adaptation AS "Adaptation", status AS "Status", id AS "PgId"
                FROM children ORDER BY full_name
            """)
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    return [dict(r) for r in rows]


def read_attendance_rows_for_rate() -> list[dict]:
    """Just the columns compute_rate reads, for every row in the table (it
    filters by child itself) — shaped like _rows_as_dicts()'s Attendance
    output. Includes ChildRefId so compute_rate can match a renamed child's
    rate against rows still sitting under their old name text."""
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT child AS "Child", date AS "Date", status AS "Status",
                                  child_ref_id AS "ChildRefId" FROM attendance""")
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    return [dict(r) for r in rows]


def read_staff_rows() -> list[dict]:
    pool = _require_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT name AS "Name", position AS "Position", contract_end AS "Contract End",
                       phone AS "Phone", password AS "Password", rate AS "Rate"
                FROM staff ORDER BY name
            """)
            rows = cur.fetchall()
    finally:
        pool.putconn(conn)
    return [dict(r) for r in rows]
