from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

KM_PER_MILE = 1.609344


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Free text shown next to finds, e.g. "Slack @sam" or "text me".
    contact: Mapped[str] = mapped_column(String(200), default="")

    # Helper side: "I'll pick things up around here."
    is_helper: Mapped[bool] = mapped_column(Boolean, default=False)
    area_query: Mapped[str | None] = mapped_column(String(255))  # what they typed
    area_label: Mapped[str | None] = mapped_column(String(255))  # geocoded, city-level, shown to others
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    radius_km: Mapped[float] = mapped_column(Float, default=32.0)
    radius_unit: Mapped[str] = mapped_column(String(2), default="mi")  # display preference
    # Only used by the "facebook" source: a Marketplace location slug or numeric id
    # (the part after /marketplace/ in a Marketplace URL).
    fb_location: Mapped[str | None] = mapped_column(String(120))

    searches: Mapped[list[Search]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="Search.created_at"
    )

    @property
    def has_area(self) -> bool:
        return self.is_helper and self.lat is not None and self.lng is not None

    @property
    def radius_display(self) -> str:
        if self.radius_unit == "km":
            return f"{round(self.radius_km):g} km"
        return f"{round(self.radius_km / KM_PER_MILE):g} mi"

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else self.email


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(String(200))
    min_price: Mapped[float | None] = mapped_column(Float)
    max_price: Mapped[float | None] = mapped_column(Float)
    # When on, every word of the query must appear in the listing title.
    strict: Mapped[bool] = mapped_column(Boolean, default=True)
    must_include: Mapped[str] = mapped_column(String(300), default="")  # comma separated
    exclude: Mapped[str] = mapped_column(String(300), default="")  # comma separated
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(back_populates="searches")
    matches: Mapped[list[Match]] = relationship(back_populates="search", cascade="all, delete-orphan")


class Listing(Base):
    """A Marketplace listing, shared across every search/helper that turned it up."""

    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Marketplace item id
    title: Mapped[str] = mapped_column(String(500))
    price_text: Mapped[str | None] = mapped_column(String(64))
    price_amount: Mapped[float | None] = mapped_column(Float)
    location_text: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(500))
    image_url: Mapped[str | None] = mapped_column(Text)
    image_file: Mapped[str | None] = mapped_column(String(255))  # cached copy in DATA_DIR/images
    is_sold: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Match(Base):
    """'Search S found listing L in helper H's area.'"""

    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("search_id", "listing_id", "helper_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"), index=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    helper_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(10), default="new")  # new | seen | saved | hidden
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    search: Mapped[Search] = relationship(back_populates="matches")
    listing: Mapped[Listing] = relationship()
    helper: Mapped[User] = relationship(foreign_keys=[helper_id])


class AreaCheck(Base):
    """When a query was last fetched in a helper's area; used to rotate fairly under a request cap."""

    __tablename__ = "area_checks"
    __table_args__ = (UniqueConstraint("query", "helper_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(200))  # normalized
    helper_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")  # schedule | manual | cli
    source: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | partial | failed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
    listings_seen: Mapped[int] = mapped_column(Integer, default=0)
    new_matches: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
