from __future__ import annotations

from typing import Mapping


PRIORITY_DETAIL_FIELDS: tuple[str, ...] = (
    "사업위치",
    "시행주체",
    "추진근거",
    "추진경위",
    "추진계획",
)


def should_open_detail_page(selected: Mapping[str, str], min_selected_fields: int = 2) -> bool:
    threshold = max(1, int(min_selected_fields))
    selected_count = sum(1 for value in selected.values() if str(value).strip())
    if selected_count < threshold:
        return True

    return all(not str(selected.get(key, "")).strip() for key in PRIORITY_DETAIL_FIELDS)
