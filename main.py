import os

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import Optional
from pydantic import BaseModel

from database import engine, get_db, Base
import models
import sheets_client

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth ──────────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    password: str

@app.post("/auth/login")
def login(data: LoginIn):
    if data.password == os.environ.get("DIRECTOR_PASSWORD"):
        return {"role": "director"}
    if data.password == os.environ.get("STAFF_PASSWORD"):
        return {"role": "staff"}
    raise HTTPException(status_code=401, detail="Invalid password")

# ── Children ──────────────────────────────────────────────────────────────────

@app.get("/children")
def get_children(db: Session = Depends(get_db)):
    children = sheets_client.get_children()
    avatars = {a.child_id: a.emoji for a in db.query(models.ChildAvatar).all()}
    for c in children:
        if c["id"] in avatars:
            c["emoji"] = avatars[c["id"]]
    return children

class ChildEmojiIn(BaseModel):
    emoji: str

class ChildDataIn(BaseModel):
    firstName:    str
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
    parent1Name:  str = ""
    parent1Phone: str = ""
    parent2Name:  str = ""
    parent2Phone: str = ""
    address:      str = ""

@app.post("/children")
def create_child(data: ChildDataIn):
    new_id = sheets_client.add_child(data.dict())
    return {"ok": True, "id": new_id}

@app.put("/children/{child_id}")
def update_child_data(child_id: str, data: ChildDataIn):
    sheets_client.update_child(child_id, data.dict())
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

@app.get("/clubs")
def get_clubs(db: Session = Depends(get_db)):
    clubs = db.query(models.Club).all()
    return [{"id": c.id, "name_ru": c.name_ru, "name_en": c.name_en,
             "emoji": c.emoji, "color": c.color, "ink": c.ink,
             "days_ru": c.days_ru, "days_en": c.days_en, "time": c.time,
             "price": c.price, "kids": [k.id for k in c.kids]} for c in clubs]

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

# ── Payments ──────────────────────────────────────────────────────────────────

@app.get("/payments/{month}")
def get_payments(month: str):
    return sheets_client.get_payments(month)

class PaymentRow(BaseModel):
    kid_id: str
    paid:   bool
    days:   int = 1
    amount: float = 0

class PaymentsIn(BaseModel):
    month: str
    rows:  list[PaymentRow]

@app.post("/payments")
def save_payments(data: PaymentsIn):
    sheets_client.upsert_payments(data.month, [r.dict() for r in data.rows])
    return {"ok": True}

# ── Club payments ─────────────────────────────────────────────────────────────

@app.get("/club-payments/{month}/{club_id}")
def get_club_payments(month: str, club_id: int, db: Session = Depends(get_db)):
    rows = db.query(models.ClubPayment).filter_by(month=month, club_id=club_id).all()
    return {r.kid_id: r.paid for r in rows}

class ClubPaymentsIn(BaseModel):
    month:   str
    club_id: int
    paid:    dict  # {kid_id: bool}

@app.post("/club-payments")
def save_club_payments(data: ClubPaymentsIn, db: Session = Depends(get_db)):
    db.query(models.ClubPayment).filter_by(month=data.month, club_id=data.club_id).delete()
    for kid_id, paid in data.paid.items():
        db.add(models.ClubPayment(month=data.month, club_id=data.club_id,
                                   kid_id=int(kid_id), paid=paid))
    db.commit()
    return {"ok": True}
