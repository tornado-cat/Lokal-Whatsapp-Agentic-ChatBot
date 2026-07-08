from __future__ import annotations

from typing import Any

import psutil


FAKE_USER_DB: dict[str, dict[str, str]] = {
    "+905551112233": {"name": "Ahmet", "plan": "premium"},
    "905551112233": {"name": "Ahmet", "plan": "premium"},
    "+905559998877": {"name": "Mehmet", "plan": "free"},
    "905559998877": {"name": "Mehmet", "plan": "free"},
}


def get_user_profile(phone: str) -> dict[str, str]:
    profile = FAKE_USER_DB.get(phone) or FAKE_USER_DB.get(phone.lstrip("+"))
    if profile:
        return {"phone": phone, **profile}
    return {"phone": phone, "name": "Misafir", "plan": "standard"}


def check_local_system_status() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": memory.percent,
        "ram_total_gb": round(memory.total / (1024**3), 2),
        "ram_available_gb": round(memory.available / (1024**3), 2),
        "disk_percent": disk.percent,
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "disk_free_gb": round(disk.free / (1024**3), 2),
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": "Telefon numarasına göre lokal müşteri profilini getirir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "WhatsApp gönderen telefon numarası.",
                    }
                },
                "required": ["phone"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_local_system_status",
            "description": "Yerel sunucunun CPU, RAM ve disk kullanımını döndürür.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


def run_tool(tool_name: str, arguments: dict[str, Any], phone: str) -> dict[str, Any]:
    if tool_name == "get_user_profile":
        return get_user_profile(str(arguments.get("phone") or phone))
    if tool_name == "check_local_system_status":
        return check_local_system_status()
    raise ValueError(f"Unknown tool: {tool_name}")
