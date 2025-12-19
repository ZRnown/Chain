from __future__ import annotations

from typing import Optional, Tuple

from .models import FilterRange


def check_range(value: Optional[float], r: FilterRange) -> Tuple[bool, str | None]:
    if not r.is_set():
        return True, None
    if value is None:
        return False, "missing"
    if r.min is not None and value < r.min:
        return False, f"< {r.min}"
    if r.max is not None and value > r.max:
        return False, f"> {r.max}"
    return True, None


def short_num(num: Optional[float]) -> str:
    if num is None:
        return "N/A"
    if abs(num) < 1:
        return f"{num:.8f}"
    for unit in ["", "K", "M", "B"]:
        if abs(num) < 1000.0:
            if unit == "":
                return f"{num:.2f}"
            return f"{num:.2f}{unit}"
        num /= 1000.0
    return f"{num:.2f}T"

def format_time_ago(dt) -> str:
    """Format datetime as 'X小时Y分钟' or 'Y分钟'."""
    if dt is None:
        return "N/A"
    from datetime import datetime
    now = datetime.utcnow()
    
    # 验证时间合理性：不能是1970年之前或未来时间
    if dt < datetime(2020, 1, 1) or dt > now:
        # 如果是未来时间，返回"刚刚"
        if dt > now:
            return "刚刚"
        # 如果是很早的时间（可能是错误的时间戳），返回"N/A"
        return "N/A"
    
    diff = now - dt
    total_minutes = int(diff.total_seconds() / 60)
    
    # 如果时间差为负数或异常大，返回"N/A"
    if total_minutes < 0 or total_minutes > 1000000:  # 约694天
        return "N/A"
    
    if total_minutes < 60:
        return f"{total_minutes}分钟"
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if minutes == 0:
        return f"{hours}小时"
    return f"{hours}小时{minutes}分钟"

