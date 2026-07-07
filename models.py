from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from database import Base

class Group(Base):
    __tablename__ = "groups"
    id      = Column(String, primary_key=True)
    name_ru = Column(String)
    name_en = Column(String)
    color   = Column(String)
    ink     = Column(String)
    emoji   = Column(String)

class Club(Base):
    __tablename__ = "clubs"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    name_ru = Column(String)
    name_en = Column(String)
    emoji   = Column(String)
    color   = Column(String)
    ink     = Column(String)
    days_ru = Column(String)
    days_en = Column(String)
    time    = Column(String)
    price   = Column(Integer, nullable=True)
    # Membership itself lives in the Children sheet's "Clubs" column (see sheets_client),
    # not here — that's the single source of truth for who's in which club.

class ClubAttendance(Base):
    __tablename__ = "club_attendance"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    club_id  = Column(Integer, ForeignKey("clubs.id"))
    date     = Column(String)   # "YYYY-MM-DD"
    child_id = Column(String)   # "First Last"
    status   = Column(String, default="present")

class ClubPayment(Base):
    __tablename__ = "club_payments"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    month   = Column(String)
    club_id = Column(Integer, ForeignKey("clubs.id"))
    kid_id  = Column(String)    # "First Last"
    paid    = Column(Boolean, default=False)

class ChildAvatar(Base):
    __tablename__ = "child_avatars"
    child_id = Column(String, primary_key=True)
    emoji    = Column(String)

class FeedItem(Base):
    __tablename__ = "feed_items"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    type       = Column(String, default="alert")
    emoji      = Column(String, default="📋")
    ru         = Column(String, default="")
    en         = Column(String, default="")
    unread     = Column(Boolean, default=True)
    created_at = Column(String, default="")

class StaffAttendance(Base):
    __tablename__ = "staff_attendance"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    date         = Column(String)   # "YYYY-MM-DD"
    staff_name   = Column(String)   # "First Last" from Sheets Staff tab
    status       = Column(String, default="present")  # present/absent/late/sick/day-off/unpaid/extra
    arrival_time = Column(String, nullable=True)  # "09:15" — only set for 'late'
    note         = Column(String, nullable=True)
