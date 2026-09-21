"""
Official NSE Market Calendar & Session Validator.
Enforces Indian market trading hours (09:15 AM - 03:15 PM IST, Mon-Fri)
and gazetted National Stock Exchange (NSE) trading holidays for 2024, 2025, and 2026.
"""

from datetime import datetime, timezone, timedelta, date
from typing import Tuple, Optional
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Indian Standard Time (IST) offset: UTC + 5:30
IST_OFFSET = timedelta(hours=5, minutes=30)
IST_TZ = timezone(IST_OFFSET)

def get_ist_time() -> datetime:
    """Returns current datetime in Indian Standard Time (IST)."""
    return datetime.now(timezone.utc).astimezone(IST_TZ)


# Official NSE Trading Holidays (Format: 'YYYY-MM-DD': 'Holiday Name')
# Sources: NSE Circulars for Trading Holidays
NSE_HOLIDAYS = {
    # --- 2024 Holidays ---
    "2024-01-22": "Special Holiday (Ram Mandir Pran Pratishtha)",
    "2024-01-26": "Republic Day",
    "2024-03-08": "Mahashivratri",
    "2024-03-25": "Holi",
    "2024-03-29": "Good Friday",
    "2024-04-11": "Id-Ul-Fitr (Ramzan Id)",
    "2024-04-17": "Shri Ram Navami",
    "2024-05-01": "Maharashtra Day",
    "2024-05-20": "General Parliamentary Elections (Mumbai)",
    "2024-06-17": "Bakri Id",
    "2024-07-17": "Muharram",
    "2024-08-15": "Independence Day",
    "2024-10-02": "Mahatma Gandhi Jayanti",
    "2024-11-01": "Diwali Laxmi Pujan (Muhurat Trading evening only)",
    "2024-11-15": "Gurunanak Jayanti",
    "2024-11-20": "Maharashtra Assembly Elections",
    "2024-12-25": "Christmas",

    # --- 2025 Holidays ---
    "2025-02-26": "Mahashivratri",
    "2025-03-14": "Holi",
    "2025-03-31": "Id-Ul-Fitr (Ramzan Id)",
    "2025-04-10": "Shri Mahavir Jayanti",
    "2025-04-14": "Dr. Babasaheb Ambedkar Jayanti",
    "2025-04-18": "Good Friday",
    "2025-05-01": "Maharashtra Day",
    "2025-06-07": "Bakri Id",
    "2025-08-15": "Independence Day",
    "2025-08-27": "Ganesh Chaturthi",
    "2025-10-02": "Mahatma Gandhi Jayanti",
    "2025-10-21": "Diwali Laxmi Pujan",
    "2025-10-22": "Diwali Balipratipada",
    "2025-11-05": "Gurunanak Jayanti",
    "2025-12-25": "Christmas",

    # --- 2026 Holidays ---
    "2026-01-26": "Republic Day",
    "2026-02-16": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-20": "Id-Ul-Fitr (Ramzan Id)",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Babasaheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-27": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-24": "Gurunanak Jayanti",
    "2026-12-25": "Christmas"
}


def is_market_holiday(dt: Optional[datetime] = None) -> Tuple[bool, Optional[str]]:
    """
    Checks whether a given date is an official NSE gazetted holiday.
    Returns (is_holiday: bool, holiday_name: str or None).
    """
    if dt is None:
        dt = get_ist_time()
    
    date_str = dt.strftime("%Y-%m-%d")
    if date_str in NSE_HOLIDAYS:
        return True, NSE_HOLIDAYS[date_str]
    return False, None


def check_market_session(dt: Optional[datetime] = None) -> Tuple[bool, str, str]:
    """
    Checks if current time is within Indian market auto-trading window (09:15 AM - 03:15 PM IST, Mon-Fri).
    Enforces weekends, gazetted NSE holidays, pre-market, intraday cutoff, and post-market.

    Returns:
      (is_trading_active: bool, session_status: str, message: str)
    """
    ist_now = dt if dt is not None else get_ist_time()

    # 1. Weekend check (Saturday = 5, Sunday = 6)
    if ist_now.weekday() >= 5:
        return (
            False,
            "weekend",
            "Indian market is closed on weekends (Saturday/Sunday). Auto-trading opens Monday at 09:15 AM IST."
        )

    # 2. Gazetted NSE Holiday check
    is_holiday, holiday_name = is_market_holiday(ist_now)
    if is_holiday:
        return (
            False,
            "holiday",
            f"Indian market is closed today for {holiday_name}. Auto-trading will resume on the next trading session at 09:15 AM IST."
        )

    # 3. Pre-market check (before 09:15 AM IST)
    if (ist_now.hour < 9) or (ist_now.hour == 9 and ist_now.minute < 15):
        return (
            False,
            "pre_market",
            "Indian market is currently closed. Auto-trading opens at 09:15 AM IST."
        )

    # 4. Post 15:15 IST intraday cutoff check (Strict intraday risk management)
    if (ist_now.hour > 15) or (ist_now.hour == 15 and ist_now.minute >= 15):
        return (
            False,
            "post_market",
            "Indian market intraday trading window closed (cutoff 15:15 IST). Open positions are squared off. Next session begins tomorrow at 09:15 AM IST."
        )

    # 5. Market is Open
    return (
        True,
        "open",
        "Indian market is open for auto-trading (09:15 - 15:15 IST)."
    )
