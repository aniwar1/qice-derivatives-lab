"""研究期内常规期权的到期日规则和逐腿官方参考资料查询。"""
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import json
import re


def historical_expiry(contract, sessions):
    """仅适用本研究时期的常规月份。交易所调整或逐腿公告优先。"""
    m = re.fullmatch(r"([A-Z]+)(\d\d)(\d\d)", contract)
    if not m or not 1 <= int(m[3]) <= 12:
        return None
    p = m[1]
    if not "2604" <= contract[-4:] <= "2710":
        return None
    prior = date(2000 + int(m[2]), int(m[3]), 1) - timedelta(days=1)
    if p in {"AP", "CJ"}:
        end = prior.replace(day=1) - timedelta(days=1)
        n = 3
    elif p in {"CF", "FG", "MA", "OI", "PK", "SA", "SM", "SR", "UR"}:
        end, n = prior.replace(day=15), 3
    elif p in {"CU", "RU", "SP"}:
        end, n = prior, 5
    else:
        return None
    if not sessions or sessions[-1] < end.isoformat() or sessions[0] > end.replace(day=1).isoformat():
        return None
    i = bisect_right(sessions, end.isoformat())
    return date.fromisoformat(sessions[i-n]) if i >= n and sessions[i-n][:7] == end.isoformat()[:7] else None


class ExpiryIndex:
    """只选择该决策时点已经披露、且该腿实际出现在原件内的资料。"""
    def __init__(self, path: Path):
        data = json.loads(path.read_bytes())
        self.rows = defaultdict(list)
        for dump in data["sources"]:
            available = datetime.fromisoformat(dump["available_at"])
            for group in dump["series"]:
                for leg in group["legs"]:
                    self.rows[leg].append((available, group["expiry"], dump["sha256"]))
        for rows in self.rows.values():
            rows.sort()

    def lookup(self, leg: str, cutoff: datetime):
        if cutoff.utcoffset() is None:
            raise ValueError("查询时点须携带时区")
        rows = self.rows.get(leg, ())
        for at, exp, digest in reversed(rows):
            if at <= cutoff:
                return {"expiry": exp, "available_at": at.isoformat(), "sha256": digest}
        return None
