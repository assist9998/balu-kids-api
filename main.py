import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text, inspect
from sqlalchemy.orm import Session
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

app = FastAPI()

app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    return await call_next(request)

# ── Staff ─────────────────────────────────────────────────────────────────────

class StaffIn(BaseModel):
    name:        str
    position:    str = ""
    contractEnd: str = ""
    phone:       str = ""
    password:    str = ""
    rate:        str = ""

@app.get("/staff")
def get_staff():
    staff = sheets_client.get_staff()
    return [{"name": s["name"], "position": s["position"],
             "contractEnd": s["contractEnd"], "phone": s["phone"],
             "rate": s["rate"]} for s in staff]

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
    return {r.staff_name: {"status": r.status, "arrival_time": r.arrival_time or "", "note": r.note or ""}
            for r in rows}

class StaffAttendanceIn(BaseModel):
    date:    str
    records: dict  # {name: {status, arrival_time?, note?}}

@app.post("/staff-attendance")
def save_staff_attendance(data: StaffAttendanceIn, db: Session = Depends(get_db)):
    db.query(models.StaffAttendance).filter_by(date=data.date).delete()
    for name, rec in data.records.items():
        db.add(models.StaffAttendance(
            date=data.date, staff_name=name,
            status=rec.get("status", "present"),
            arrival_time=rec.get("arrival_time") or None,
            note=rec.get("note") or None,
        ))
    db.commit()
    return {"ok": True}

class StaffAttendanceSingleIn(BaseModel):
    status:       str = "present"
    arrival_time: str = ""
    note:         str = ""

@app.put("/staff-attendance/{date}/{staff_name}")
def upsert_single_staff_attendance(date: str, staff_name: str, data: StaffAttendanceSingleIn, db: Session = Depends(get_db)):
    row = db.query(models.StaffAttendance).filter_by(date=date, staff_name=staff_name).first()
    if row:
        row.status = data.status
        row.arrival_time = data.arrival_time or None
        row.note = data.note or None
    else:
        db.add(models.StaffAttendance(date=date, staff_name=staff_name,
            status=data.status, arrival_time=data.arrival_time or None, note=data.note or None))
    db.commit()
    return {"ok": True}

@app.delete("/staff-attendance/{date}/{staff_name}")
def delete_single_staff_attendance(date: str, staff_name: str, db: Session = Depends(get_db)):
    db.query(models.StaffAttendance).filter_by(date=date, staff_name=staff_name).delete()
    db.commit()
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
        result[r.staff_name][r.date] = {"status": r.status, "arrival_time": r.arrival_time or ""}
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
def save_club_attendance(club_id: int, data: ClubAttendanceIn, db: Session = Depends(get_db)):
    sheets_client.upsert_club_attendance(_club_name(club_id, db), data.date, data.statuses)
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
def save_attendance(data: AttendanceIn):
    sheets_client.upsert_attendance(data.date, data.statuses)
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

@app.post("/payment-log")
def add_payment_log_entry(data: PaymentLogIn):
    coverage = sheets_client.add_payment_log_entry(
        data.kidId, data.tariff, data.dateFrom, data.dateUntil, data.amount)
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

@app.post("/club-payment-log")
def add_club_payment_log_entry(data: ClubPaymentLogIn, db: Session = Depends(get_db)):
    club = db.query(models.Club).filter_by(id=data.clubId).first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")
    coverage = sheets_client.add_club_payment_log_entry(
        data.kidId, club.name_en, data.dateFrom, data.dateUntil, data.amount)
    return {"ok": True, **coverage}

@app.delete("/club-payment-log/{row_id}")
def delete_club_payment_log_entry(row_id: int):
    try:
        coverage = sheets_client.delete_club_payment_log_entry(row_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, **coverage}
