"""内置合同样例：首次启动时注册一个生产端和两个清洗器，/docs 可直接试换版。"""

from __future__ import annotations

# 生产端 v1：点击事件。event_id 必填；可选位置与停留毫秒数。
PRODUCER_V1 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "anyOf": [
        {
            "properties": {
                "event_id": {"type": "string"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                "y": {"type": "integer", "minimum": 0, "maximum": 4096},
                "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
            },
            "required": ["event_id", "button"],
            "additionalProperties": False,
        }
    ],
}

# 清洗器 A：与生产端 v1 完全一致（接受全部合法事件）。
CLEANER_A_V1 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "anyOf": [
        {
            "properties": {
                "event_id": {"type": "string"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                "y": {"type": "integer", "minimum": 0, "maximum": 4096},
                "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
            },
            "required": ["event_id", "button"],
            "additionalProperties": False,
        }
    ],
}

# 清洗器 B：更宽松——坐标上限更大、不要求 button、dwell_ms 可空。
# 仍然接受生产端 v1 的全部事件（超集）。
CLEANER_B_V1 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "anyOf": [
        {
            "properties": {
                "event_id": {"type": "string"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "x": {"type": "integer", "minimum": 0, "maximum": 10000},
                "y": {"type": "integer", "minimum": 0, "maximum": 10000},
                "dwell_ms": {"type": ["integer", "null"], "minimum": 0, "maximum": 120000},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        }
    ],
}

# ---- 候选换版样例（供 /docs 手工 PUT 时粘贴）---------------------------------

# 生产端候选 v2（不兼容）：新增 dwell_ms 到 90000ms 的取值。
# cleaner-a 只接受到 60000ms，必然拒收 60001~90000ms 的合法生产事件；
# cleaner-b 上限 120000 且允许 null，仍可接受（但换版要求 *所有* 清洗器接受）。
PRODUCER_V2_INCOMPATIBLE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "anyOf": [
        {
            "properties": {
                "event_id": {"type": "string"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                "y": {"type": "integer", "minimum": 0, "maximum": 4096},
                "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 90000},
            },
            "required": ["event_id", "button"],
            "additionalProperties": False,
        }
    ],
}

# 生产端候选 v2（兼容版）：仅把 x/y 上限收紧到 2048，仍在两个清洗器接受范围内。
PRODUCER_V2_COMPATIBLE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "anyOf": [
        {
            "properties": {
                "event_id": {"type": "string"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "x": {"type": "integer", "minimum": 0, "maximum": 2048},
                "y": {"type": "integer", "minimum": 0, "maximum": 2048},
                "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
            },
            "required": ["event_id", "button"],
            "additionalProperties": False,
        }
    ],
}

# 清洗器候选 v2（不兼容）：把 dwell_ms 上限收紧到 10s，
# 生产端仍会产出 10001~60000ms 的事件，换版应被拒绝并给出反例。
CLEANER_V2_INCOMPATIBLE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "anyOf": [
        {
            "properties": {
                "event_id": {"type": "string"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                "y": {"type": "integer", "minimum": 0, "maximum": 4096},
                "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
            },
            "required": ["event_id", "button"],
            "additionalProperties": False,
        }
    ],
}

SEED_ENDPOINTS = [
    ("producer-main", "producer", "埋点生产端", PRODUCER_V1),
    ("cleaner-a", "cleaner", "数仓清洗器A", CLEANER_A_V1),
    ("cleaner-b", "cleaner", "数仓清洗器B", CLEANER_B_V1),
]
