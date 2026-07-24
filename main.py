import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text, inspect
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from typing import Optional
from pydantic import BaseModel

from database import engine, get_db, Base, SessionLocal
import models
import sheets_client

CHILDREN_CACHE_REFRESH_SECONDS = 15

# Serializes every children_cache refresh — the periodic loop and a write
# endpoint's own refresh both end up calling sheets_client.get_children(),
# and without this lock two overlapping calls can race: whichever started
# its Sheets read first can still finish *last* and overwrite the other's
# (fresher) result with a pre-write snapshot, silently losing the write for
# up to CHILDREN_CACHE_REFRESH_SECONDS. Serializing them fixes that while
# keeping refreshes fire-and-forget from the caller's point of view.
_children_cache_refresh_lock = threading.Lock()

def _refresh_children_cache() -> None:
    """Pull Children straight from Sheets and mirror into children_cache —
    this is what picks up Ольга's direct edits (prices, paidUntil, clubs, ...)
    on the next cycle, since app reads/writes go through the cache, not Sheets."""
    with _children_cache_refresh_lock:
        children = sheets_client.get_children()
        now = datetime.now(timezone.utc).isoformat()
        db = SessionLocal()
        try:
            seen_ids = set()
            for c in children:
                seen_ids.add(c["id"])
                row = db.query(models.ChildCache).filter_by(id=c["id"]).first()
                payload = json.dumps(c)
                if row:
                    row.data, row.updated_at = payload, now
                else:
                    db.add(models.ChildCache(id=c["id"], data=payload, updated_at=now))
            for row in db.query(models.ChildCache).all():
                if row.id not in seen_ids:
                    db.delete(row)
            db.commit()
        finally:
            db.close()

def _refresh_children_cache_async() -> None:
    """Same as _refresh_children_cache(), but doesn't make the caller wait —
    the lock inside it still serializes against the periodic loop, so this
    stays correct, just not synchronous."""
    threading.Thread(target=_refresh_children_cache, daemon=True).start()

def _cached_children(db: Session) -> list[dict]:
    return [json.loads(row.data) for row in db.query(models.ChildCache).all()]

def _refresh_club_schedule_cache() -> None:
    clubs = sheets_client.get_clubs_from_sheets()
    db = SessionLocal()
    try:
        row = db.query(models.ClubScheduleCache).filter_by(id=1).first()
        payload = json.dumps(clubs)
        if row:
            row.data = payload
        else:
            db.add(models.ClubScheduleCache(id=1, data=payload))
        db.commit()
    finally:
        db.close()

def _cached_club_schedule(db: Session) -> list[dict]:
    row = db.query(models.ClubScheduleCache).filter_by(id=1).first()
    return json.loads(row.data) if row else []

def _children_cache_refresh_loop() -> None:
    while True:
        time.sleep(CHILDREN_CACHE_REFRESH_SECONDS)
        try:
            _refresh_children_cache()
        except Exception:
            pass  # Sheets hiccup — next cycle will retry; stale cache beats a crashed thread
        try:
            _refresh_club_schedule_cache()
        except Exception:
            pass

# ── End-of-day attendance sweep ────────────────────────────────────────────────
# Kids get an attendance row the moment staff actually mark them (see
# save_attendance/save_club_attendance below) — nobody's status is written
# until someone taps them. A kid nobody tapped all day still needs a record
# that they were simply never marked (as opposed to "marked absent"), so
# Ольга's sheet has the same complete daily picture it always did. This runs
# once, late in the day, and backfills anyone with no row yet as "absent",
# attributed to "Система" rather than whichever staff member happened to
# touch the app last.
# Kept in sheets_client.py (single source of truth) since _should_defer_carryover
# there needs the same threshold — this just points at it.
ATTENDANCE_SWEEP_HOUR_BALI = sheets_client.ATTENDANCE_SWEEP_HOUR_BALI  # Asia/Makassar is UTC+8, no DST
_SWEEP_CHECK_SECONDS = 600
_last_attendance_sweep_date: Optional[str] = None

def _run_attendance_sweep(date: str, db: Session) -> None:
    active = [c for c in _cached_children(db) if c.get("active", True)]

    existing = sheets_client.get_attendance(date)
    unmarked = {c["id"]: "absent" for c in active if c["id"] not in existing}
    if unmarked:
        sheets_client.upsert_attendance(date, unmarked, "Система")
    # Whole day's final picture now settled (today's real-time marks were
    # never immediately compensated, see _should_defer_carryover) — decide
    # carryover once, here, for everyone absent by the end of the day.
    sheets_client.run_end_of_day_carryover(date)

    kids_by_club = {}
    for c in active:
        for name in sheets_client.split_club_names(c["clubs"]):
            kids_by_club.setdefault(name, []).append(c["id"])
    for club in db.query(models.Club).all():
        members = kids_by_club.get(club.name_en, [])
        if not members:
            continue
        existing_club = sheets_client.get_club_attendance(club.name_en, date)
        unmarked_club = {kid_id: "absent" for kid_id in members if kid_id not in existing_club}
        if unmarked_club:
            sheets_client.upsert_club_attendance(club.name_en, date, unmarked_club, "Система")
        sheets_client.run_end_of_day_club_carryover(club.name_en, date)

def _attendance_sweep_loop() -> None:
    global _last_attendance_sweep_date
    while True:
        time.sleep(_SWEEP_CHECK_SECONDS)
        bali_now = datetime.now(timezone.utc) + timedelta(hours=8)
        bali_today = bali_now.strftime("%Y-%m-%d")
        if bali_now.hour < ATTENDANCE_SWEEP_HOUR_BALI or bali_today == _last_attendance_sweep_date:
            continue
        db = SessionLocal()
        try:
            _run_attendance_sweep(bali_today, db)
            _last_attendance_sweep_date = bali_today
        except Exception:
            pass  # Sheets hiccup — next cycle (still same Bali day) will retry
        finally:
            db.close()

# ── Staff attendance -> Sheets mirror ───────────────────────────────────────────
# StaffAttendance lives only in this app's own SQLite (see database.py) — never
# in Sheets, unlike everything else here. Ольга asked for a read-only window
# onto it so she doesn't have to ask someone to check the database. Pushed on
# a dirty flag rather than a dumb timer: every write endpoint below marks
# _staff_attendance_dirty, and this loop checks every 30s whether that's set
# AND at least STAFF_PUSH_MIN_INTERVAL has passed since the last real push —
# so a quiet stretch does nothing at all, and a burst of edits still only
# costs one push every 5 minutes, not one per edit.
_staff_attendance_dirty = False
_last_staff_push_at = 0.0
STAFF_PUSH_MIN_INTERVAL = 300  # 5 minutes
STAFF_PUSH_POLL_SECONDS = 30

def _mark_staff_attendance_dirty():
    global _staff_attendance_dirty
    _staff_attendance_dirty = True

def _push_staff_attendance(db: Session) -> None:
    rows = db.query(models.StaffAttendance).all()
    sheets_client.push_staff_attendance_rows([
        {"date": r.date, "staff_name": r.staff_name, "status": r.status,
         "arrival_time": r.arrival_time, "note": r.note, "transfer": r.transfer, "extra": r.extra}
        for r in rows
    ])

def _staff_attendance_push_loop() -> None:
    global _staff_attendance_dirty, _last_staff_push_at
    while True:
        time.sleep(STAFF_PUSH_POLL_SECONDS)
        if not _staff_attendance_dirty:
            continue
        if time.time() - _last_staff_push_at < STAFF_PUSH_MIN_INTERVAL:
            continue  # edited recently — wait out the cooldown, flag stays set
        # Reset *before* the slow Sheets call, not after — _push_staff_attendance
        # can take a real amount of time, and a write landing while it's still
        # running would otherwise get its dirty=True silently stomped back to
        # False by this same iteration's cleanup, even though that write's
        # data was never actually part of the push that just happened. Reset
        # here means that write's own dirty=True survives untouched, so the
        # next poll picks it up instead of losing it.
        _staff_attendance_dirty = False
        db = SessionLocal()
        try:
            _push_staff_attendance(db)
            _last_staff_push_at = time.time()
        except Exception:
            _staff_attendance_dirty = True  # push itself failed — make sure it's retried
        finally:
            db.close()

def _migrate():
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    with engine.connect() as conn:
        # Drop old tables that changed schema (no real data yet)
        if "club_members" not in tables:
            conn.execute(text("DROP TABLE IF EXISTS club_payments"))
            conn.execute(text("DROP TABLE IF EXISTS club_kids"))
            conn.commit()
        # club_payments (month/paid checkbox) replaced by the Club payment
        # log sheet — drop unconditionally now that nothing reads it.
        if "club_payments" in tables:
            conn.execute(text("DROP TABLE IF EXISTS club_payments"))
            conn.commit()
        # transfer (bus escort duty, extra-paid) added to an existing table —
        # create_all() below only creates missing tables, never ALTERs an
        # existing one, so this needs its own explicit migration.
        if "staff_attendance" in tables:
            existing_cols = {c["name"] for c in inspector.get_columns("staff_attendance")}
            if "transfer" not in existing_cols:
                conn.execute(text("ALTER TABLE staff_attendance ADD COLUMN transfer FLOAT DEFAULT 0"))
                conn.commit()
            if "extra" not in existing_cols:
                conn.execute(text("ALTER TABLE staff_attendance ADD COLUMN extra BOOLEAN DEFAULT 0"))
                conn.commit()
            # Without this, two concurrent writes for the same (date, staff)
            # (e.g. the durable queue retrying a request whose first attempt
            # actually landed, right as a second edit comes in) could each
            # see "no row yet" and both insert, leaving two rows for the same
            # day/person — needed for the ON CONFLICT upserts below to have
            # something to target in the first place.
            existing_indexes = {ix["name"] for ix in inspector.get_indexes("staff_attendance")}
            if "staff_attendance_date_name_uq" not in existing_indexes:
                conn.execute(text(
                    "CREATE UNIQUE INDEX staff_attendance_date_name_uq ON staff_attendance (date, staff_name)"
                ))
                conn.commit()
    Base.metadata.create_all(bind=engine)

def _seed_clubs(db: Session):
    if db.query(models.Club).count() > 0:
        return
    clubs = [
        models.Club(name_ru="Шахматы", name_en="Chess", emoji="♟️",
                    color="#CDE8FB", ink="#1f5f86",
                    days_ru="Пн, Ср", days_en="Mon, Wed", time="15:00"),
        models.Club(name_ru="Плавание", name_en="Swimming", emoji="🏊",
                    color="#D4F0DF", ink="#1f7a55",
                    days_ru="Вт, Пт", days_en="Tue, Fri", time="16:00"),
    ]
    db.add_all(clubs)
    db.commit()

_migrate()
_db = SessionLocal()
try:
    _seed_clubs(_db)
finally:
    _db.close()

try:
    _refresh_children_cache()  # populate before the first request lands
except Exception:
    pass  # Sheets unreachable at boot — background loop will retry
try:
    _refresh_club_schedule_cache()
except Exception:
    pass
threading.Thread(target=_children_cache_refresh_loop, daemon=True).start()
threading.Thread(target=_attendance_sweep_loop, daemon=True).start()
threading.Thread(target=_staff_attendance_push_loop, daemon=True).start()

app = FastAPI()

# ── Auth ──────────────────────────────────────────────────────────────────────
# In-memory session store: token -> {role, name}. Lost on backend restart —
# same tradeoff as StaffAttendance/FeedItem already have on this box (no
# persistent volume), acceptable since it just forces a re-login, which
# already happens once a day anyway (see loadStoredRole's daily reset).
_SESSIONS: dict[str, dict] = {}

def _make_token(role: str, name: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {"role": role, "name": name}
    return token

class LoginIn(BaseModel):
    password: str

_STAFF_POSITION_ROLE = {"director": "director", "accounter": "staff", "accountant": "staff", "staff": "staff"}

@app.post("/auth/login")
def login(data: LoginIn):
    # Individual Staff-sheet passwords first — a personal password should
    # always win over the shared DIRECTOR_PASSWORD/STAFF_PASSWORD env vars,
    # otherwise an accidental collision (e.g. a teacher's password happening
    # to match STAFF_PASSWORD) silently logs them in under the wrong role.
    try:
        for s in sheets_client.get_staff():
            if s["password"] and data.password == s["password"]:
                role = _STAFF_POSITION_ROLE.get(s["position"].strip().lower(), "teacher")
                return {"role": role, "name": s["name"], "token": _make_token(role, s["name"])}
    except Exception:
        pass
    if data.password == os.environ.get("DIRECTOR_PASSWORD"):
        return {"role": "director", "token": _make_token("director")}
    if data.password == os.environ.get("STAFF_PASSWORD"):
        return {"role": "staff", "token": _make_token("staff")}
    raise HTTPException(status_code=401, detail="Invalid password")

@app.middleware("http")
async def require_auth(request: Request, call_next):
    # CORS preflight and the login endpoint itself must stay open —
    # everything else needs a valid token from a prior /auth/login.
    if request.method == "OPTIONS" or request.url.path == "/auth/login":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    if not token or token not in _SESSIONS:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    request.state.session = _SESSIONS[token]
    return await call_next(request)

def _marker(request: Request) -> str:
    """Who to write into a sheet's "Marked by" column for an action taken
    under the current session — the individual Staff-sheet login's own name
    when there is one, else just the role (shared DIRECTOR_PASSWORD/
    STAFF_PASSWORD logins never have a name attached)."""
    session = getattr(request.state, "session", {}) or {}
    return session.get("name") or session.get("role", "").capitalize()

# Registered *after* require_auth on purpose: Starlette wraps middleware in
# reverse registration order, so whichever is added last ends up outermost.
# CORS must be outermost so it can still stamp Access-Control-Allow-Origin
# onto a 401 short-circuited by require_auth — otherwise the browser reports
# a bare CORS failure instead of a readable 401, and the frontend's 401
# handling (clear token, force re-login) never gets a response to look at.
app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Staff ─────────────────────────────────────────────────────────────────────

class StaffIn(BaseModel):
    name:        str
    position:    str = ""
    contractEnd: str = ""
    phone:       str = ""
    password:    str = ""
    rate:        str = ""

# Utility logins, not real staff to track attendance/contracts/salary for —
# Ольга ("director") can't sensibly mark her own attendance, and Alexander
# ("developer") only has a Staff-sheet row so he can log in at all. Filtered
# out here so every screen that calls GET /staff (Team, Journal, Schedule,
# Monthly) stays clean automatically, without each one remembering to do it.
_NON_TRACKED_POSITIONS = {"director", "developer"}

@app.get("/staff")
def get_staff():
    staff = sheets_client.get_staff()
    return [{"name": s["name"], "position": s["position"],
             "contractEnd": s["contractEnd"], "phone": s["phone"],
             "rate": s["rate"]} for s in staff
            if s["position"].strip().lower() not in _NON_TRACKED_POSITIONS]

@app.post("/staff")
def create_staff(data: StaffIn):
    sheets_client.add_staff({
        "Name": data.name, "Position": data.position,
        "Contract End": data.contractEnd, "Phone": data.phone,
        "Password": data.password, "Rate": data.rate,
    })
    return {"ok": True}

@app.put("/staff/{old_name}")
def update_staff(old_name: str, data: StaffIn):
    payload = {
        "Name": data.name, "Position": data.position,
        "Contract End": data.contractEnd, "Phone": data.phone, "Rate": data.rate,
    }
    if data.password:  # only update password if provided
        payload["Password"] = data.password
    try:
        sheets_client.update_staff(old_name, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}

@app.delete("/staff/{name}")
def delete_staff(name: str):
    try:
        sheets_client.delete_staff(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}

@app.get("/staff-attendance/{date}")
def get_staff_attendance(date: str, db: Session = Depends(get_db)):
    rows = db.query(models.StaffAttendance).filter_by(date=date).all()
    return {r.staff_name: {"status": r.status, "arrival_time": r.arrival_time or "", "note": r.note or "",
                           "transfer": r.transfer or 0, "extra": bool(r.extra)}
            for r in rows}

def _upsert_staff_attendance_row(db: Session, date: str, staff_name: str, status: str,
                                  arrival_time: str | None, note: str | None,
                                  transfer: float, extra: bool) -> None:
    """One atomic INSERT ... ON CONFLICT (date, staff_name) DO UPDATE, instead
    of a SELECT to check "does a row exist" followed by a separate INSERT or
    UPDATE. That select-then-write shape has a real race: two requests for
    the same (date, staff_name) landing close together (a double-tap, or the
    durable queue retrying a call whose first attempt actually succeeded but
    whose response got lost) can both see "no row yet" and both try to
    INSERT, leaving two rows for what should be a single (date, staff_name)
    — the unique index added in _migrate() is what makes ON CONFLICT here
    possible at all."""
    stmt = sqlite_insert(models.StaffAttendance).values(
        date=date, staff_name=staff_name, status=status,
        arrival_time=arrival_time, note=note, transfer=transfer, extra=extra,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["date", "staff_name"],
        set_={"status": stmt.excluded.status, "arrival_time": stmt.excluded.arrival_time,
              "note": stmt.excluded.note, "transfer": stmt.excluded.transfer, "extra": stmt.excluded.extra},
    )
    db.execute(stmt)

class StaffAttendanceIn(BaseModel):
    date:    str
    records: dict  # {name: {status, arrival_time?, note?, transfer?, extra?}} — transfer: 0 | 0.5 | 1

@app.post("/staff-attendance")
def save_staff_attendance(data: StaffAttendanceIn, db: Session = Depends(get_db)):
    # Upserts only the names actually in this payload — never deletes+
    # recreates the whole day. The frontend now only ever sends what
    # actually changed (see StaffDailyScreen.save()), same reasoning as
    # attendance/club attendance already send deltas: a delete-everything-
    # for-the-date-then-insert-everyone-back approach meant whoever saved
    # last silently discarded anyone else's edits to a *different* person
    # made in between, since it was replaying that saver's own possibly-
    # stale full-roster snapshot.
    for name, rec in data.records.items():
        _upsert_staff_attendance_row(
            db, data.date, name, rec.get("status", "present"),
            rec.get("arrival_time") or None, rec.get("note") or None,
            float(rec.get("transfer", 0) or 0), bool(rec.get("extra", False)),
        )
    db.commit()
    _mark_staff_attendance_dirty()
    return {"ok": True}

class StaffAttendanceSingleIn(BaseModel):
    status:       str = "present"
    arrival_time: str = ""
    note:         str = ""
    transfer:     float = 0  # 0 = none, 0.5 = half day, 1 = full day
    extra:        bool = False

@app.put("/staff-attendance/{date}/{staff_name}")
def upsert_single_staff_attendance(date: str, staff_name: str, data: StaffAttendanceSingleIn, db: Session = Depends(get_db)):
    _upsert_staff_attendance_row(
        db, date, staff_name, data.status, data.arrival_time or None, data.note or None,
        data.transfer, data.extra,
    )
    db.commit()
    _mark_staff_attendance_dirty()
    return {"ok": True}

@app.delete("/staff-attendance/{date}/{staff_name}")
def delete_single_staff_attendance(date: str, staff_name: str, db: Session = Depends(get_db)):
    db.query(models.StaffAttendance).filter_by(date=date, staff_name=staff_name).delete()
    db.commit()
    _mark_staff_attendance_dirty()
    return {"ok": True}

@app.get("/staff-attendance-month/{month}")
def get_staff_attendance_month(month: str, db: Session = Depends(get_db)):
    rows = db.query(models.StaffAttendance).filter(
        models.StaffAttendance.date.like(f"{month}%")
    ).all()
    result: dict = {}
    for r in rows:
        if r.staff_name not in result:
            result[r.staff_name] = {}
        result[r.staff_name][r.date] = {"status": r.status, "arrival_time": r.arrival_time or "",
                                         "transfer": r.transfer or 0, "extra": bool(r.extra)}
    return result

# ── Children ──────────────────────────────────────────────────────────────────

@app.get("/children")
def get_children(db: Session = Depends(get_db)):
    children = _cached_children(db)
    avatars = {a.child_id: a.emoji for a in db.query(models.ChildAvatar).all()}
    for c in children:
        if c["id"] in avatars:
            c["emoji"] = avatars[c["id"]]
    return children

class ChildEmojiIn(BaseModel):
    emoji: str

class ChildDataIn(BaseModel):
    firstName:    str = ""
    lastName:     str = ""
    group:        str = "big"
    birthday:     str = ""
    contractType: str = "longterm"
    dayType:      str = ""
    price:        str = ""
    startDate:    str = ""
    allergies:    str = ""
    paracetamol:  str = ""
    photoConsent: str = ""
    adaptation:   bool = False
    mealsIncluded: str = ""
    napTime:      bool = False
    afterSchool:  bool = False
    deposit:      str = ""
    paidFrom:     str = ""
    paidUntil:    str = ""
    parent1Name:  str = ""
    parent1Phone: str = ""
    parent2Name:  str = ""
    parent2Phone: str = ""
    address:      str = ""
    status:       str = ""

@app.post("/children")
def create_child(data: ChildDataIn):
    new_id = sheets_client.add_child(data.dict())
    # Frontend has no local copy of a brand-new child to patch in — it reloads
    # from /children right after, so the cache must already contain it by
    # the time this response goes out, not "eventually" via the background loop.
    _refresh_children_cache()
    return {"ok": True, "id": new_id}

@app.put("/children/{child_id}")
def update_child_data(child_id: str, data: ChildDataIn):
    # exclude_unset=True — only update fields explicitly sent in the request
    sheets_client.update_child(child_id, data.dict(exclude_unset=True))
    _refresh_children_cache_async()
    return {"ok": True}

@app.delete("/children/{child_id}")
def delete_child_route(child_id: str):
    try:
        sheets_client.delete_child(child_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    _refresh_children_cache_async()
    return {"ok": True}

@app.put("/children/{child_id}/emoji")
def set_child_emoji(child_id: str, data: ChildEmojiIn, db: Session = Depends(get_db)):
    row = db.query(models.ChildAvatar).filter_by(child_id=child_id).first()
    if row:
        row.emoji = data.emoji
    else:
        db.add(models.ChildAvatar(child_id=child_id, emoji=data.emoji))
    db.commit()
    return {"ok": True}

# ── Groups ────────────────────────────────────────────────────────────────────

@app.get("/groups")
def get_groups(db: Session = Depends(get_db)):
    return db.query(models.Group).all()

# ── Clubs ─────────────────────────────────────────────────────────────────────

def _club_dict(c, kids):
    return {"id": c.id, "name_ru": c.name_ru, "name_en": c.name_en,
            "emoji": c.emoji, "color": c.color, "ink": c.ink,
            "days_ru": c.days_ru, "days_en": c.days_en, "time": c.time,
            "price": c.price, "kids": kids}

@app.get("/clubs")
def get_clubs(db: Session = Depends(get_db)):
    clubs = db.query(models.Club).all()
    # Merge prices and schedule from the (cached) Clubs sheet (Olga edits there)
    price_map = {s["name_ru"]: s for s in _cached_club_schedule(db)}

    # Membership itself comes from the (cached) Children data's "clubs" field —
    # Sheets is still the real source, this is just the fast local mirror of it.
    kids_by_club = {}
    for c in _cached_children(db):
        for name in sheets_client.split_club_names(c["clubs"]):
            kids_by_club.setdefault(name, []).append(c["id"])

    result = []
    for c in clubs:
        sheet = price_map.get(c.name_ru, {})
        d = _club_dict(c, kids_by_club.get(c.name_en, []))
        if sheet.get("price") is not None:
            d["price"] = sheet["price"]
        if sheet.get("days"):
            days = sheet["days"]
            if "/" in days:
                parts = [p.strip() for p in days.split("/")]
                d["days_ru"] = parts[0]
                d["days_en"] = parts[1] if len(parts) > 1 else parts[0]
            else:
                d["days_ru"] = days
                d["days_en"] = days
        if sheet.get("time"):
            d["time"] = sheet["time"]
        result.append(d)
    return result

@app.post("/clubs/{club_id}/members/{child_id}")
def add_club_member(club_id: int, child_id: str, db: Session = Depends(get_db)):
    club = db.query(models.Club).filter_by(id=club_id).first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")
    sheets_client.add_child_club(child_id, club.name_en)
    _refresh_children_cache_async()
    return {"ok": True}

@app.delete("/clubs/{club_id}/members/{child_id}")
def remove_club_member(club_id: int, child_id: str, db: Session = Depends(get_db)):
    club = db.query(models.Club).filter_by(id=club_id).first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")
    sheets_client.remove_child_club(child_id, club.name_en)
    _refresh_children_cache_async()
    return {"ok": True}

def _club_name(club_id: int, db: Session) -> str:
    club = db.query(models.Club).filter_by(id=club_id).first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")
    return club.name_en

@app.get("/club-attendance/{club_id}/{date}")
def get_club_attendance(club_id: int, date: str, db: Session = Depends(get_db)):
    return sheets_client.get_club_attendance(_club_name(club_id, db), date)

class ClubAttendanceIn(BaseModel):
    date:     str
    statuses: dict  # {child_id: "present" | "absent"}

@app.post("/club-attendance/{club_id}")
def save_club_attendance(club_id: int, data: ClubAttendanceIn, request: Request, db: Session = Depends(get_db)):
    sheets_client.upsert_club_attendance(_club_name(club_id, db), data.date, data.statuses, _marker(request))
    return {"ok": True}

@app.get("/club-attendance-history/{club_id}/{kid_id}")
def club_attendance_history(club_id: int, kid_id: str, db: Session = Depends(get_db)):
    return sheets_client.get_club_attendance_history(_club_name(club_id, db), kid_id)

# ── Attendance ────────────────────────────────────────────────────────────────

@app.get("/attendance/{date}")
def get_attendance(date: str):
    return sheets_client.get_attendance(date)

class AttendanceIn(BaseModel):
    date:     str
    statuses: dict  # {kid_id: status}

@app.post("/attendance")
def save_attendance(data: AttendanceIn, request: Request):
    sheets_client.upsert_attendance(data.date, data.statuses, _marker(request))
    return {"ok": True}

@app.get("/attendance-history/{kid_id}")
def attendance_history(kid_id: str):
    return sheets_client.get_attendance_history(kid_id)

# ── Payment log ───────────────────────────────────────────────────────────────

@app.get("/payment-log/{kid_id}")
def get_payment_log(kid_id: str):
    return sheets_client.get_payment_log(kid_id)

class PaymentLogIn(BaseModel):
    kidId:     str
    tariff:    str
    dateFrom:  str
    dateUntil: str
    amount:    str
    # Client-generated, one per confirm-payment tap — lets a safely-retried
    # request (app backgrounded/killed right as the first attempt's
    # response was coming back) get recognized as "already recorded"
    # instead of appending a second row for the same payment. Optional so
    # an older cached frontend build without this still works.
    idempotencyKey: str | None = None

@app.post("/payment-log")
def add_payment_log_entry(data: PaymentLogIn, request: Request):
    coverage = sheets_client.add_payment_log_entry(
        data.kidId, data.tariff, data.dateFrom, data.dateUntil, data.amount, _marker(request),
        data.idempotencyKey)
    _refresh_children_cache_async()
    return {"ok": True, **coverage}

@app.delete("/payment-log/{row_id}")
def delete_payment_log_entry(row_id: int):
    try:
        coverage = sheets_client.delete_payment_log_entry(row_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    _refresh_children_cache_async()
    return {"ok": True, **coverage}

# ── Feed ──────────────────────────────────────────────────────────────────────

def _feed_dict(i):
    return {"id": i.id, "type": i.type, "emoji": i.emoji,
            "ru": i.ru, "en": i.en, "unread": i.unread, "created_at": i.created_at}

def _check_birthdays(db: Session):
    """Auto-create birthday feed items for children whose birthday is today."""
    today = datetime.now().date()
    today_prefix = today.strftime("%Y-%m-%dT")
    today_dm = (today.day, today.month)
    try:
        children = sheets_client.get_children()
    except Exception:
        return
    for kid in children:
        dob = (kid.get("dob") or "").strip()
        if not dob:
            continue
        parts = dob.split(".")
        if len(parts) != 3:
            continue
        try:
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if (d, m) != today_dm:
            continue
        age = today.year - y
        name = kid.get("id", "")
        first = name.split()[0] if name else name
        already = db.query(models.FeedItem).filter(
            models.FeedItem.type == "birthday",
            models.FeedItem.ru.contains(first),
            models.FeedItem.en.contains(name if name else first),
            models.FeedItem.created_at.startswith(today_prefix),
        ).first()
        if already:
            continue
        n = age % 10
        h = age % 100
        if n == 1 and h != 11:
            age_ru = f"{age} год"
        elif 2 <= n <= 4 and not 11 <= h <= 14:
            age_ru = f"{age} года"
        else:
            age_ru = f"{age} лет"
        item = models.FeedItem(
            type="birthday", emoji="🎂",
            ru=f"Сегодня день рождения у {first} — {age_ru}! 🎉",
            en=f"It's {name}'s birthday today — {age} year{'s' if age != 1 else ''} old! 🎉",
            unread=True,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(item)
    db.commit()

@app.get("/feed")
def get_feed(db: Session = Depends(get_db)):
    _check_birthdays(db)
    items = db.query(models.FeedItem).order_by(models.FeedItem.id.desc()).all()
    return [_feed_dict(i) for i in items]

class FeedItemIn(BaseModel):
    type:  str = "alert"
    emoji: str = "📋"
    ru:    str
    en:    str = ""

@app.post("/feed")
def create_feed_item(data: FeedItemIn, db: Session = Depends(get_db)):
    item = models.FeedItem(
        type=data.type, emoji=data.emoji, ru=data.ru, en=data.en,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(item); db.commit(); db.refresh(item)
    return _feed_dict(item)

@app.patch("/feed/read-all")
def mark_all_read(db: Session = Depends(get_db)):
    db.query(models.FeedItem).filter_by(unread=True).update({"unread": False})
    db.commit()
    return {"ok": True}

@app.patch("/feed/{item_id}/read")
def mark_feed_read(item_id: int, db: Session = Depends(get_db)):
    item = db.query(models.FeedItem).filter_by(id=item_id).first()
    if item:
        item.unread = False; db.commit()
    return {"ok": True}

@app.delete("/feed/{item_id}")
def delete_feed_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(models.FeedItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(item); db.commit()
    return {"ok": True}

# ── Club payment log ──────────────────────────────────────────────────────────

@app.get("/club-payment-log/{club_id}")
def get_club_payment_log(club_id: int, db: Session = Depends(get_db)):
    club = db.query(models.Club).filter_by(id=club_id).first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")
    return sheets_client.get_club_payment_log(club.name_en)

class ClubPaymentLogIn(BaseModel):
    kidId:     str
    clubId:    int
    dateFrom:  str
    dateUntil: str
    amount:    str
    idempotencyKey: str | None = None  # see PaymentLogIn.idempotencyKey

@app.post("/club-payment-log")
def add_club_payment_log_entry(data: ClubPaymentLogIn, request: Request, db: Session = Depends(get_db)):
    club = db.query(models.Club).filter_by(id=data.clubId).first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")
    coverage = sheets_client.add_club_payment_log_entry(
        data.kidId, club.name_en, data.dateFrom, data.dateUntil, data.amount, _marker(request),
        data.idempotencyKey)
    return {"ok": True, **coverage}

@app.delete("/club-payment-log/{row_id}")
def delete_club_payment_log_entry(row_id: int):
    try:
        coverage = sheets_client.delete_club_payment_log_entry(row_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, **coverage}
