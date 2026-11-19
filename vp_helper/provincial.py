"""Identify Chinese provincial contests without requiring an ICPC/CCPC label."""
from __future__ import annotations

import re
import unicodedata


PROVINCES = (
    ("Anhui", "安徽"), ("Beijing", "北京"), ("Chongqing", "重庆"),
    ("Fujian", "福建"), ("Gansu", "甘肃"), ("Guangdong", "广东"),
    ("Guangxi", "广西"), ("Guizhou", "贵州"), ("Hainan", "海南"),
    ("Hebei", "河北"), ("Heilongjiang", "黑龙江"), ("Henan", "河南"),
    ("Hubei", "湖北"), ("Hunan", "湖南"), ("Inner Mongolia", "内蒙古"),
    ("Jiangsu", "江苏"), ("Jiangxi", "江西"), ("Jilin", "吉林"),
    ("Liaoning", "辽宁"), ("Ningxia", "宁夏"), ("Qinghai", "青海"),
    ("Shaanxi", "陕西"), ("Shandong", "山东"), ("Shanghai", "上海"),
    ("Shanxi", "山西"), ("Sichuan", "四川"), ("Tianjin", "天津"),
    ("Tibet", "西藏"), ("Xinjiang", "新疆"), ("Yunnan", "云南"),
    ("Zhejiang", "浙江"), ("Taiwan", "台湾"), ("Hong Kong", "香港"),
    ("Macau", "澳门"),
)


def provincial_name(title: str) -> str | None:
    text = unicodedata.normalize("NFKC", title)
    # Do not attach a main provincial archive to an invitation or practice set.
    if re.search(r"\b(?:Regional|Invitational|Warm[ -]?up|Practice|Preliminary)\b|区域|邀请|热身|预赛|赛区|站", text, re.I):
        return None
    years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", text))
    if len(years) != 1:
        return None
    chinese = bool(re.search(r"省赛|(?:大学生|高校).*程序设计(?:竞赛|大赛)", text))
    english = bool(re.search(r"\b(?:Provincial|Collegiate|University)\b.*\bProgramming\b.*\b(?:Contest|Competition|Championship)\b", text, re.I))
    if not (chinese or english):
        return None
    provinces = [name for name, local in PROVINCES
                 if local in text or re.search(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", text, re.I)]
    if len(provinces) != 1:
        return None
    series = re.search(r"(?<![A-Za-z])(ICPC|CCPC)(?![A-Za-z])", text, re.I)
    prefix = series[1].upper() + " " if series else ""
    return f"{next(iter(years))} {prefix}{provinces[0]} Pro"


def provincial_parts(name: str) -> tuple[str, str | None, str] | None:
    match = re.fullmatch(r"(20\d{2}) (?:(ICPC|CCPC) )?(.+) Pro", name)
    if match and match[3] in {province for province, _ in PROVINCES}:
        return match[1], match[2], match[3]
    return None


def ranking_group(contest, metadata: dict | None = None) -> str:
    selected = contest.extra.get("provincial_group", "official")
    if selected not in ("official", "guest", "all"):
        from .models import VPError
        raise VPError("provincial_group must be official, guest or all.")
    available = metadata.get("ranking_groups") if metadata else None
    return selected if not available or selected in available else "official"
