from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import Optional
from pydantic import BaseModel

from database import engine, get_db, Base
import models

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Children ──────────────────────────────────────────────────────────────────

@app.get("/children")
def get_children(db: Session = Depends(get_db)):
    kids = db.query(models.Child).all()
    return [{"id": k.id, "name_ru": k.name_ru, "name_en": k.name_en,
             "emoji": k.emoji, "group": k.group_id, "contract": k.contract} for k in kids]

class ChildIn(BaseModel):
    name_ru:  str
    name_en:  str
    emoji:    str
    group_id: str
    contract: str = "longterm"

@app.post("/children")
def add_child(data: ChildIn, db: Session = Depends(get_db)):
    k = models.Child(**data.dict())
    db.add(k); db.commit(); db.refresh(k)
    return {"id": k.id}

@app.delete("/children/{kid_id}")
def delete_child(kid_id: int, db: Session = Depends(get_db)):
    db.query(models.Child).filter_by(id=kid_id).delete()
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
def get_attendance(date: str, db: Session = Depends(get_db)):
    rows = db.query(models.Attendance).filter_by(date=date).all()
    return {r.kid_id: r.status for r in rows}

class AttendanceIn(BaseModel):
    date:     str
    statuses: dict  # {kid_id: status}

@app.post("/attendance")
def save_attendance(data: AttendanceIn, db: Session = Depends(get_db)):
    db.query(models.Attendance).filter_by(date=data.date).delete()
    for kid_id, status in data.statuses.items():
        db.add(models.Attendance(date=data.date, kid_id=int(kid_id), status=status))
    db.commit()
    return {"ok": True}

# ── Payments ──────────────────────────────────────────────────────────────────

@app.get("/payments/{month}")
def get_payments(month: str, db: Session = Depends(get_db)):
    rows = db.query(models.Payment).filter_by(month=month).all()
    return {r.kid_id: {"paid": r.paid, "days": r.days} for r in rows}

class PaymentRow(BaseModel):
    kid_id: int
    paid:   bool
    days:   int = 1

class PaymentsIn(BaseModel):
    month: str
    rows:  list[PaymentRow]

@app.post("/payments")
def save_payments(data: PaymentsIn, db: Session = Depends(get_db)):
    db.query(models.Payment).filter_by(month=data.month).delete()
    for r in data.rows:
        db.add(models.Payment(month=data.month, kid_id=r.kid_id, paid=r.paid, days=r.days))
    db.commit()
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
