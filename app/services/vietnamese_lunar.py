"""Vietnamese lunar calendar helpers (AI-156).

``lunar-vn`` implements the Vietnamese lunar calendar (UTC+7) and supports
solar dates from 1900-01-01 through 2100-12-31. Keeping the dependency behind
this module means the rest of the app only sees ``date`` values and the swap to
another library (or a pre-computed table) stays local.
"""

from __future__ import annotations

from datetime import date

from lunar_vn import LunarDate, lunar_to_solar


def lunar_date_to_solar(year: int, month: int, day: int) -> date:
    """Return the solar date for a Vietnamese lunar date."""
    return lunar_to_solar(LunarDate(day=day, month=month, year=year, leap=False))


def tet_date(year: int) -> date:
    """Return the solar date of Tết Nguyên Đán (lunar 1/1)."""
    return lunar_date_to_solar(year, 1, 1)


def hung_kings_date(year: int) -> date:
    """Return the solar date of Giỗ Tổ Hùng Vương (lunar 3/10)."""
    return lunar_date_to_solar(year, 3, 10)
