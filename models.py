from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Table
from sqlalchemy.orm import relationship
from database import Base

club_kids = Table("club_kids", Base.metadata,
    Column("club_id", Integer, ForeignKey("clubs.id")),
    Column("kid_id",  Integer, ForeignKey("children.id")),
)

class Group(Base):
    __tablename__ = "groups"
    id      = Column(String, primary_key=True)
    name_ru = Column(String)
    name_en = Column(String)
    color   = Column(String)
    ink     = Column(String)
    emoji   = Column(String)

class Child(Base):
    __tablename__ = "children"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    name_ru  = Column(String)
    name_en  = Column(String)
    emoji    = Column(String)
    group_id = Column(String, ForeignKey("groups.id"))
    contract = Column(String, default="longterm")  # longterm / tourist
    group    = relationship("Group")

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
    price   = Column(Integer)
    kids    = relationship("Child", secondary=club_kids)

class Attendance(Base):
    __tablename__ = "attendance"
    id     = Column(Integer, primary_key=True, autoincrement=True)
    date   = Column(String)   # YYYY-MM-DD
    kid_id = Column(Integer, ForeignKey("children.id"))
    status = Column(String)   # present / absent

class Payment(Base):
    __tablename__ = "payments"
    id     = Column(Integer, primary_key=True, autoincrement=True)
    month  = Column(String)   # jun / jul / aug / sep
    kid_id = Column(Integer, ForeignKey("children.id"))
    paid   = Column(Boolean, default=False)
    days   = Column(Integer, default=1)   # для туристов

class ClubPayment(Base):
    __tablename__ = "club_payments"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    month   = Column(String)
    club_id = Column(Integer, ForeignKey("clubs.id"))
    kid_id  = Column(Integer, ForeignKey("children.id"))
    paid    = Column(Boolean, default=False)
