"""Время: в базе всё хранится в UTC, расписание считается по Москве."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def next_weekday_at(after: datetime, weekday: int, at: time, inclusive: bool = False) -> datetime:
    """Ближайший момент weekday (0=пн … 6=вс) в at по Москве, строго после after
    (или не раньше, если inclusive). Возвращает UTC."""
    local = after.astimezone(MSK)
    days = (weekday - local.weekday()) % 7
    candidate = datetime.combine(local.date() + timedelta(days=days), at, tzinfo=MSK)
    if candidate < local or (candidate == local and not inclusive):
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


def fmt_msk(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def rub(kopecks: int) -> str:
    rub_, kop = divmod(int(kopecks), 100)
    s = f"{rub_:,}".replace(",", " ")
    return f"{s} ₽" if kop == 0 else f"{s},{kop:02d} ₽"
