"""
Tiny Home Assistant MCP bridge.

This server talks to Home Assistant's REST API. It is intentionally small:
configure HA_URL and HA_TOKEN, connect it as a stdio MCP server, then expose a
few generic tools for listing entities and calling services.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "data" / "home_assistant_mcp.json"
ALIASES_PATH = BASE_DIR / "data" / "home_assistant_aliases.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "ha_url": "http://homeassistant.local:8123",
    "ha_token": "",
    "dry_run": True,
    "allowed_domains": [
        "light",
        "switch",
        "fan",
        "cover",
        "climate",
        "humidifier",
        "scene",
        "script",
    ],
}

mcp = FastMCP("Home Assistant")


def _load_config() -> dict[str, Any]:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return dict(DEFAULT_CONFIG)

    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        loaded = {}

    cfg = dict(DEFAULT_CONFIG)
    cfg.update(loaded if isinstance(loaded, dict) else {})

    cfg["ha_url"] = os.getenv("HA_URL", cfg.get("ha_url", "")).rstrip("/")
    cfg["ha_token"] = os.getenv("HA_TOKEN", cfg.get("ha_token", ""))
    cfg["dry_run"] = _as_bool(os.getenv("HA_DRY_RUN", cfg.get("dry_run", True)))

    if isinstance(cfg.get("allowed_domains"), str):
        cfg["allowed_domains"] = [
            item.strip() for item in cfg["allowed_domains"].split(",") if item.strip()
        ]
    return cfg


def _load_aliases() -> dict[str, Any]:
    if not ALIASES_PATH.exists():
        ALIASES_PATH.write_text(
            json.dumps(
                {
                    "aliases": {},
                    "groups": {},
                    "blocked_entities": [],
                    "blocked_aliases": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    try:
        data = json.loads(ALIASES_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        data = {}
    return {
        "aliases": data.get("aliases", {}) if isinstance(data.get("aliases"), dict) else {},
        "groups": data.get("groups", {}) if isinstance(data.get("groups"), dict) else {},
        "blocked_entities": data.get("blocked_entities", [])
        if isinstance(data.get("blocked_entities"), list)
        else [],
        "blocked_aliases": data.get("blocked_aliases", [])
        if isinstance(data.get("blocked_aliases"), list)
        else [],
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _allowed_entity_ids() -> set[str]:
    aliases = _load_aliases()
    return {
        item.get("entity_id", "")
        for item in aliases["aliases"].values()
        if isinstance(item, dict) and item.get("entity_id")
    }


def _assert_control_allowed(entity_id: str, alias: str = "") -> dict[str, Any] | None:
    aliases = _load_aliases()
    blocked_entities = set(aliases["blocked_entities"])
    blocked_aliases = set(aliases["blocked_aliases"])
    allowed_entities = _allowed_entity_ids()

    if alias and alias in blocked_aliases:
        return {
            "ok": False,
            "message": f"Alias '{alias}' is blocked and cannot be controlled.",
        }
    if entity_id in blocked_entities:
        return {
            "ok": False,
            "message": f"Entity '{entity_id}' is blocked and cannot be controlled.",
        }
    if entity_id not in allowed_entities:
        return {
            "ok": False,
            "message": (
                f"Entity '{entity_id}' is not in the control allowlist. "
                f"Add it to {ALIASES_PATH} aliases before controlling it."
            ),
            "aliases_path": str(ALIASES_PATH),
        }
    return None


def _configured(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("ha_url") and cfg.get("ha_token"))


def _headers(cfg: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg['ha_token']}",
        "Content-Type": "application/json",
    }


def _friendly_missing_config() -> dict[str, Any]:
    return {
        "ok": False,
        "configured": False,
        "message": (
            "Home Assistant bridge is installed, but HA token is not configured yet. "
            f"Edit {CONFIG_PATH} or set HA_URL and HA_TOKEN."
        ),
        "config_path": str(CONFIG_PATH),
    }


async def _ha_get(path: str) -> Any:
    cfg = _load_config()
    if not _configured(cfg):
        return _friendly_missing_config()

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{cfg['ha_url']}{path}", headers=_headers(cfg))
        resp.raise_for_status()
        return resp.json()


async def _ha_post(path: str, payload: dict[str, Any]) -> Any:
    cfg = _load_config()
    if not _configured(cfg):
        return _friendly_missing_config()

    domain = path.strip("/").split("/")[-2]
    entity_id = str(payload.get("entity_id", ""))
    if entity_id:
        denied = _assert_control_allowed(entity_id)
        if denied:
            return denied

    allowed_domains = set(cfg.get("allowed_domains") or [])
    if domain not in allowed_domains:
        return {
            "ok": False,
            "message": f"Domain '{domain}' is not in allowed_domains.",
            "allowed_domains": sorted(allowed_domains),
        }

    if cfg.get("dry_run", True):
        return {
            "ok": True,
            "dry_run": True,
            "message": "Dry run is enabled; no Home Assistant service was called.",
            "path": path,
            "payload": payload,
        }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{cfg['ha_url']}{path}", json=payload, headers=_headers(cfg)
        )
        resp.raise_for_status()
        if not resp.text:
            return {"ok": True, "result": None}
        return {"ok": True, "result": resp.json()}


@mcp.tool()
def get_setup_status() -> dict[str, Any]:
    """Show whether this Home Assistant MCP bridge is configured."""
    cfg = _load_config()
    return {
        "configured": _configured(cfg),
        "ha_url": cfg.get("ha_url", ""),
        "has_token": bool(cfg.get("ha_token")),
        "dry_run": bool(cfg.get("dry_run", True)),
        "allowed_domains": cfg.get("allowed_domains", []),
        "config_path": str(CONFIG_PATH),
        "aliases_path": str(ALIASES_PATH),
    }


@mcp.tool()
async def check_home_assistant() -> dict[str, Any]:
    """Check whether the configured Home Assistant instance responds."""
    data = await _ha_get("/api/")
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    return {"ok": True, "response": data}


@mcp.tool()
async def list_entities(domain: str = "", search: str = "", limit: int = 80) -> dict[str, Any]:
    """List Home Assistant entities, optionally filtered by domain or text."""
    states = await _ha_get("/api/states")
    if isinstance(states, dict) and states.get("ok") is False:
        return states
    if not isinstance(states, list):
        return {"ok": False, "message": "Unexpected Home Assistant response", "raw": states}

    domain = domain.strip().lower()
    search = search.strip().lower()
    result = []
    for item in states:
        entity_id = item.get("entity_id", "")
        attributes = item.get("attributes") or {}
        friendly_name = attributes.get("friendly_name", "")
        if domain and not entity_id.startswith(f"{domain}."):
            continue
        haystack = f"{entity_id} {friendly_name}".lower()
        if search and search not in haystack:
            continue
        result.append(
            {
                "entity_id": entity_id,
                "state": item.get("state"),
                "friendly_name": friendly_name,
            }
        )
        if len(result) >= max(1, min(int(limit), 200)):
            break
    return {"ok": True, "count": len(result), "entities": result}


@mcp.tool()
async def get_entity_state(entity_id: str) -> dict[str, Any]:
    """Read one Home Assistant entity state."""
    data = await _ha_get(f"/api/states/{entity_id}")
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    return {"ok": True, "entity": data}


@mcp.tool()
def list_aliases() -> dict[str, Any]:
    """List human-friendly controllable aliases."""
    aliases = _load_aliases()
    result = []
    for alias, item in aliases["aliases"].items():
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "alias": alias,
                "entity_id": item.get("entity_id", ""),
                "notes": item.get("notes", ""),
                "blocked": alias in aliases["blocked_aliases"]
                or item.get("entity_id", "") in aliases["blocked_entities"],
            }
        )
    groups = [
        {"alias": alias, "aliases": group, "type": "group"}
        for alias, group in aliases["groups"].items()
        if isinstance(group, list)
    ]
    return {
        "ok": True,
        "count": len(result),
        "aliases": result,
        "groups": groups,
        "aliases_path": str(ALIASES_PATH),
    }


def _resolve_alias(alias: str) -> dict[str, Any]:
    aliases = _load_aliases()
    item = aliases["aliases"].get(alias)
    if not isinstance(item, dict) or not item.get("entity_id"):
        return {
            "ok": False,
            "message": f"Unknown alias '{alias}'.",
            "available_aliases": sorted(aliases["aliases"].keys()),
        }
    denied = _assert_control_allowed(item["entity_id"], alias=alias)
    if denied:
        return denied
    return {"ok": True, "entity_id": item["entity_id"], "notes": item.get("notes", "")}


def _resolve_group(alias: str) -> list[str] | None:
    aliases = _load_aliases()
    group = aliases["groups"].get(alias)
    if not isinstance(group, list):
        return None
    return [str(item).strip() for item in group if str(item).strip()]


@mcp.tool()
async def get_alias_state(alias: str) -> dict[str, Any]:
    """Read state by human-friendly alias."""
    group = _resolve_group(alias)
    if group is not None:
        results = []
        for item_alias in group:
            state = await get_alias_state(item_alias)
            results.append({"alias": item_alias, "state": state})
        failed = [
            item for item in results
            if isinstance(item.get("state"), dict) and item["state"].get("ok") is False
        ]
        return {
            "ok": not failed,
            "alias": alias,
            "group_aliases": group,
            "group_results": results,
        }

    resolved = _resolve_alias(alias)
    if not resolved.get("ok"):
        return resolved
    return await get_entity_state(resolved["entity_id"])


@mcp.tool()
async def turn_on(entity_id: str) -> dict[str, Any]:
    """Turn on one entity using its domain's turn_on service."""
    domain = entity_id.split(".", 1)[0]
    return await _ha_post(
        f"/api/services/{domain}/turn_on",
        {"entity_id": entity_id},
    )


@mcp.tool()
async def turn_on_alias(alias: str) -> dict[str, Any]:
    """Turn on one allowlisted entity by human-friendly alias."""
    group = _resolve_group(alias)
    if group is not None:
        results = []
        for item_alias in group:
            results.append({"alias": item_alias, "result": await turn_on_alias(item_alias)})
        failed = [
            item for item in results
            if isinstance(item.get("result"), dict) and item["result"].get("ok") is False
        ]
        return {
            "ok": not failed,
            "alias": alias,
            "group_aliases": group,
            "group_results": results,
        }

    resolved = _resolve_alias(alias)
    if not resolved.get("ok"):
        return resolved
    result = await turn_on(resolved["entity_id"])
    if isinstance(result, dict):
        result["alias"] = alias
    return result


@mcp.tool()
async def turn_off(entity_id: str) -> dict[str, Any]:
    """Turn off one entity using its domain's turn_off service."""
    domain = entity_id.split(".", 1)[0]
    return await _ha_post(
        f"/api/services/{domain}/turn_off",
        {"entity_id": entity_id},
    )


@mcp.tool()
async def turn_off_alias(alias: str) -> dict[str, Any]:
    """Turn off one allowlisted entity by human-friendly alias."""
    group = _resolve_group(alias)
    if group is not None:
        results = []
        for item_alias in group:
            results.append({"alias": item_alias, "result": await turn_off_alias(item_alias)})
        failed = [
            item for item in results
            if isinstance(item.get("result"), dict) and item["result"].get("ok") is False
        ]
        return {
            "ok": not failed,
            "alias": alias,
            "group_aliases": group,
            "group_results": results,
        }

    resolved = _resolve_alias(alias)
    if not resolved.get("ok"):
        return resolved
    result = await turn_off(resolved["entity_id"])
    if isinstance(result, dict):
        result["alias"] = alias
    return result


@mcp.tool()
async def set_climate_alias(
    alias: str,
    hvac_mode: str = "",
    temperature: str = "",
    fan_mode: str = "",
    swing_mode: str = "",
) -> dict[str, Any]:
    """Set a climate entity by alias, then return its latest state."""
    resolved = _resolve_alias(alias)
    if not resolved.get("ok"):
        return resolved

    entity_id = resolved["entity_id"]
    if not entity_id.startswith("climate."):
        return {
            "ok": False,
            "message": f"Alias '{alias}' is '{entity_id}', not a climate entity.",
        }

    calls: list[dict[str, Any]] = []
    hvac_mode = hvac_mode.strip()
    temperature = temperature.strip()
    fan_mode = fan_mode.strip()
    swing_mode = swing_mode.strip()

    if hvac_mode:
        calls.append(
            await _ha_post(
                "/api/services/climate/set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": hvac_mode},
            )
        )

    if temperature:
        try:
            temp_value = float(temperature)
        except ValueError:
            return {"ok": False, "message": f"Invalid temperature: {temperature}"}
        payload: dict[str, Any] = {"entity_id": entity_id, "temperature": temp_value}
        if hvac_mode:
            payload["hvac_mode"] = hvac_mode
        calls.append(await _ha_post("/api/services/climate/set_temperature", payload))

    if fan_mode:
        calls.append(
            await _ha_post(
                "/api/services/climate/set_fan_mode",
                {"entity_id": entity_id, "fan_mode": fan_mode},
            )
        )

    if swing_mode:
        calls.append(
            await _ha_post(
                "/api/services/climate/set_swing_mode",
                {"entity_id": entity_id, "swing_mode": swing_mode},
            )
        )

    failed = [item for item in calls if isinstance(item, dict) and item.get("ok") is False]
    if failed:
        return {"ok": False, "alias": alias, "entity_id": entity_id, "calls": calls}

    state = await get_entity_state(entity_id)
    return {
        "ok": True,
        "alias": alias,
        "entity_id": entity_id,
        "calls": calls,
        "entity": state.get("entity") if isinstance(state, dict) else None,
    }


@mcp.tool()
async def call_service(
    domain: str,
    service: str,
    entity_id: str = "",
    service_data_json: str = "{}",
) -> dict[str, Any]:
    """Call an allowed Home Assistant service with optional JSON service data."""
    try:
        service_data = json.loads(service_data_json or "{}")
        if not isinstance(service_data, dict):
            return {"ok": False, "message": "service_data_json must decode to an object."}
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"Invalid service_data_json: {exc}"}

    payload: dict[str, Any] = dict(service_data)
    if entity_id:
        payload["entity_id"] = entity_id
    return await _ha_post(f"/api/services/{domain}/{service}", payload)


if __name__ == "__main__":
    mcp.run()
