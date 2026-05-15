from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import FeishuFieldExtraction, FeishuFormAnswerDraft, FeishuFormFillExtraction, PRESET_FEISHU_FORM_NAME


PRESET_FEISHU_FORM_FIELD_IDS = {
    "name": "fldRiWDs6J",
    "attendance_time": "fldMwK92Ml",
    "attendance_count": "fldROl771Y",
}


_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_CN_NUM_MAP = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_NAME_STOPWORDS = {
    "登记下",
    "登记",
    "报名",
    "填下",
    "填写",
    "帮我填",
    "帮我登记",
    "参会",
    "参加",
    "出席",
    "麻烦",
    "请",
}


@dataclass
class ParsedMeetingTime:
    raw_value: str
    display_value: str
    timestamp_ms: int
    has_explicit_clock: bool
    note: str | None = None


def _now_in_shanghai() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _parse_cn_number(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if len(text) == 2 and text[0] == "十" and text[1] in _CN_NUM_MAP:
        return 10 + _CN_NUM_MAP[text[1]]
    if len(text) == 2 and text[1] == "十" and text[0] in _CN_NUM_MAP:
        return _CN_NUM_MAP[text[0]] * 10
    if len(text) == 3 and text[1] == "十" and text[0] in _CN_NUM_MAP and text[2] in _CN_NUM_MAP:
        return _CN_NUM_MAP[text[0]] * 10 + _CN_NUM_MAP[text[2]]
    if len(text) == 1 and text in _CN_NUM_MAP:
        return _CN_NUM_MAP[text]
    return None


def _extract_name(query: str) -> tuple[str | None, str | None]:
    patterns = [
        r"(?:我叫|我是|姓名(?:是|为)?|名字(?:是|叫)?)[：:\s]*([A-Za-z\u4e00-\u9fff·]{2,40})",
        r"(?:登记(?:下)?|报名(?:下)?|填写(?:下)?|填下|记录(?:下)?)[，,\s]*([A-Za-z\u4e00-\u9fff·]{2,20})[，,。；;\s]",
        r"^[\"'“”]?\s*([A-Za-z\u4e00-\u9fff·]{2,4})[，,。；;\s]",
    ]
    for pattern in patterns:
        match = re.search(pattern, query)
        if match:
            value = re.split(r"[，,。；;、\n ]", match.group(1).strip())[0]
            if value and value not in _NAME_STOPWORDS and not re.search(r"(参会|参加|时间|人数)", value):
                return value, match.group(0).strip()
    return None, None


def _is_plausible_name(value: str | None) -> bool:
    if not value:
        return False
    value = value.strip()
    if value in _NAME_STOPWORDS:
        return False
    if re.search(r"(参会|参加|时间|人数|登记|报名|填写)", value):
        return False
    return bool(re.fullmatch(r"[A-Za-z\u4e00-\u9fff·]{2,20}", value))


def _extract_count(query: str) -> tuple[str | None, str | None]:
    patterns = [
        r"(?:参会人数|人数|一共|总共|共计)[：:\s]*([0-9]{1,4}|[零一二两三四五六七八九十]{1,3})\s*(?:个)?(?:人|位|名)?",
        r"([0-9]{1,4}|[零一二两三四五六七八九十]{1,3})\s*(?:个)?(?:人|位|名)(?:参会|参加)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, query)
        if not match:
            continue
        raw = match.group(1)
        value = _parse_cn_number(raw)
        if value is not None:
            return str(value), match.group(0).strip()
    return None, None


def _extract_time_of_day(text: str) -> tuple[int, int, bool]:
    meridiem_match = re.search(r"(上午|中午|下午|晚上)?\s*(\d{1,2})(?:[:点时](\d{1,2}))?", text)
    if not meridiem_match:
        return 0, 0, False
    meridiem = meridiem_match.group(1)
    hour = int(meridiem_match.group(2))
    minute = int(meridiem_match.group(3) or 0)
    if meridiem in {"下午", "晚上"} and hour < 12:
        hour += 12
    if meridiem == "中午" and hour < 11:
        hour += 12
    return hour, minute, True


def _parse_explicit_timestamp(query: str) -> ParsedMeetingTime | None:
    match = re.search(r"\b(1[0-9]{12}|[0-9]{13})\b", query)
    if match:
        raw = match.group(1)
        dt = datetime.fromtimestamp(int(raw) / 1000, tz=ZoneInfo("Asia/Shanghai"))
        return ParsedMeetingTime(
            raw_value=raw,
            display_value=dt.strftime("%Y-%m-%d %H:%M"),
            timestamp_ms=int(raw),
            has_explicit_clock=True,
            note="用户已直接提供毫秒级时间戳，已同步换算为可读时间。",
        )

    match = re.search(r"\b([0-9]{10})\b", query)
    if match:
        raw = match.group(1)
        dt = datetime.fromtimestamp(int(raw), tz=ZoneInfo("Asia/Shanghai"))
        return ParsedMeetingTime(
            raw_value=raw,
            display_value=dt.strftime("%Y-%m-%d %H:%M"),
            timestamp_ms=int(raw) * 1000,
            has_explicit_clock=True,
            note="用户提供的是秒级时间戳，提交时会自动转成毫秒级。",
        )
    return None


def _parse_relative_day(query: str, now: datetime) -> ParsedMeetingTime | None:
    match = re.search(r"(今天|明天|后天)(.*)", query)
    if not match:
        return None
    word = match.group(1)
    offset = {"今天": 0, "明天": 1, "后天": 2}[word]
    target = now + timedelta(days=offset)
    hour, minute, has_clock = _extract_time_of_day(match.group(0))
    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    display = target.strftime("%Y-%m-%d %H:%M" if has_clock else "%Y-%m-%d")
    note = None if has_clock else "用户只提供了日期，提交时将按当天 00:00 转成毫秒时间戳。"
    return ParsedMeetingTime(match.group(0).strip(), display, int(target.timestamp() * 1000), has_clock, note)


def _parse_weekday(query: str, now: datetime) -> ParsedMeetingTime | None:
    match = re.search(r"(下下周|下周|本周|这周)?(?:周|星期|礼拜)([一二三四五六日天])(.*)", query)
    if not match:
        return None
    prefix = match.group(1) or "本周"
    target_weekday = _WEEKDAY_MAP[match.group(2)]
    current_weekday = now.weekday()
    delta = (target_weekday - current_weekday) % 7
    if prefix == "下周":
        delta += 7 if delta == 0 else 7
    elif prefix == "下下周":
        delta += 14 if delta == 0 else 14
    elif prefix in {"本周", "这周"} and delta == 0 and "下" not in prefix:
        delta = 0
    target = now + timedelta(days=delta)
    hour, minute, has_clock = _extract_time_of_day(match.group(0))
    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    display = target.strftime("%Y-%m-%d %H:%M" if has_clock else "%Y-%m-%d")
    note = None if has_clock else "用户只提供了星期信息，提交时将按当天 00:00 转成毫秒时间戳。"
    return ParsedMeetingTime(match.group(0).strip(), display, int(target.timestamp() * 1000), has_clock, note)


def _parse_calendar_date(query: str, now: datetime) -> ParsedMeetingTime | None:
    patterns = [
        r"((?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})(?:日|号)?(?:\s*(上午|中午|下午|晚上)?\s*(\d{1,2})(?:[:点时](\d{1,2}))?)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, query)
        if not match:
            continue
        raw = match.group(1).strip()
        year = int(match.group(2) or now.year)
        month = int(match.group(3))
        day = int(match.group(4))
        meridiem = match.group(5)
        hour_raw = match.group(6)
        minute_raw = match.group(7)
        hour = int(hour_raw) if hour_raw else 0
        minute = int(minute_raw) if minute_raw else 0
        has_clock = hour_raw is not None
        if meridiem in {"下午", "晚上"} and hour < 12:
            hour += 12
        if meridiem == "中午" and hour < 11:
            hour += 12
        target = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
        display = target.strftime("%Y-%m-%d %H:%M" if has_clock else "%Y-%m-%d")
        note = None if match.group(2) else f"用户未提供年份，已按当前年份 {now.year} 补全。"
        if not has_clock:
            note = (
                f"{note} 提交时将按当天 00:00 转成毫秒时间戳。"
                if note
                else "用户只提供了日期，提交时将按当天 00:00 转成毫秒时间戳。"
            )
        return ParsedMeetingTime(raw, display, int(target.timestamp() * 1000), has_clock, note)
    return None


def parse_meeting_time(query: str, *, now: datetime | None = None) -> ParsedMeetingTime | None:
    current = now or _now_in_shanghai()
    for parser in (_parse_explicit_timestamp, lambda text: _parse_calendar_date(text, current), lambda text: _parse_relative_day(text, current), lambda text: _parse_weekday(text, current)):
        parsed = parser(query)
        if parsed is not None:
            return parsed
    return None


def parse_meeting_time_value(value: str | None, *, now: datetime | None = None) -> ParsedMeetingTime | None:
    if not value:
        return None
    return parse_meeting_time(value, now=now)


def _extract_count_value(query: str) -> tuple[str | None, str | None]:
    return _extract_count(query)


def _normalize_count_value(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    direct_match = re.search(r"([0-9]{1,4}|[零一二两三四五六七八九十]{1,3})", value)
    candidate = direct_match.group(1) if direct_match else value
    parsed = _parse_cn_number(candidate)
    if parsed is not None:
        return str(parsed)
    return candidate if candidate.isdigit() else None


def parse_form_fill_query(
    query: str,
    *,
    llm_extraction: FeishuFormFillExtraction | None = None,
    now: datetime | None = None,
) -> tuple[str, list[FeishuFormAnswerDraft], list[str]]:
    current = now or _now_in_shanghai()
    notes: list[str] = [
        f"问卷已固定为预置飞书表单：{PRESET_FEISHU_FORM_NAME}。",
        "如果用户在确认后又补充了新的信息，请重新走解析确认（prepare）阶段，而不是直接提交。",
    ]

    llm_name = llm_extraction.name if llm_extraction else FeishuFieldExtraction()
    llm_time = llm_extraction.attendance_time if llm_extraction else FeishuFieldExtraction()
    llm_count = llm_extraction.attendance_count if llm_extraction else FeishuFieldExtraction()

    rule_name_value, rule_name_excerpt = _extract_name(query)
    if _is_plausible_name(llm_name.value):
        name_value = llm_name.value.strip()
        name_excerpt = llm_name.raw_value or rule_name_excerpt
    else:
        name_value, name_excerpt = rule_name_value, rule_name_excerpt

    rule_count_value, rule_count_excerpt = _extract_count_value(query)
    llm_count_value = _normalize_count_value(llm_count.value)
    if llm_count_value is not None:
        count_value = llm_count_value
        count_excerpt = llm_count.raw_value or rule_count_excerpt
    else:
        count_value, count_excerpt = rule_count_value, rule_count_excerpt

    parsed_time = parse_meeting_time_value(llm_time.value, now=current) if llm_time.value else None
    if parsed_time is None:
        parsed_time = parse_meeting_time(query, now=current)

    if llm_extraction and llm_extraction.notes:
        notes.extend(llm_extraction.notes)
    if llm_extraction:
        notes.append("本次结果优先采用 LLM 结构化抽取，再由规则层做校验、归一化和兜底。")

    draft_answers = [
        FeishuFormAnswerDraft(
            index=1,
            field_key="name",
            field_label="姓名",
            question_type="string",
            required=True,
            proposed_value=name_value,
            raw_value=name_excerpt,
            normalized_values=[name_value] if name_value else [],
            confidence=(llm_name.confidence if _is_plausible_name(llm_name.value) else ("high" if name_value else "low")),
            source_excerpt=name_excerpt,
        ),
        FeishuFormAnswerDraft(
            index=2,
            field_key="attendance_time",
            field_label="参会时间",
            question_type="timestamp_ms",
            required=True,
            proposed_value=parsed_time.display_value if parsed_time else None,
            raw_value=parsed_time.raw_value if parsed_time else None,
            normalized_values=[str(parsed_time.timestamp_ms)] if parsed_time else [],
            confidence=(llm_time.confidence if llm_time.value and parsed_time else ("medium" if parsed_time else "low")),
            source_excerpt=llm_time.raw_value if llm_time.raw_value else (parsed_time.raw_value if parsed_time else None),
        ),
        FeishuFormAnswerDraft(
            index=3,
            field_key="attendance_count",
            field_label="参会人数",
            question_type="number",
            required=True,
            proposed_value=count_value,
            raw_value=count_excerpt,
            normalized_values=[count_value] if count_value else [],
            confidence=(llm_count.confidence if llm_count_value is not None else ("high" if count_value else "low")),
            source_excerpt=count_excerpt,
        ),
    ]

    missing = [item.field_label for item in draft_answers if not item.proposed_value]
    if missing:
        notes.append("以下字段暂未可靠解析，请用户确认或补充：" + "、".join(missing) + "。")
    if parsed_time and parsed_time.note:
        notes.append(f"参会时间已归一化为 {parsed_time.display_value}。{parsed_time.note}")
    elif parsed_time:
        notes.append(
            f"参会时间已归一化为 {parsed_time.display_value}，提交时会使用毫秒时间戳 {parsed_time.timestamp_ms}。"
        )
    else:
        notes.append("未能从用户输入中稳定解析参会时间。")

    return PRESET_FEISHU_FORM_NAME, draft_answers, notes


def display_time_to_timestamp_ms(display_value: str) -> int:
    display_value = display_value.strip().replace("/", "-")
    dt_format = "%Y-%m-%d %H:%M" if len(display_value) > 10 else "%Y-%m-%d"
    dt = datetime.strptime(display_value, dt_format).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(dt.timestamp() * 1000)
