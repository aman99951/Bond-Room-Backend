import json
from functools import lru_cache
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parent / "data" / "india_states_cities.json"


def _normalize(value):
    return " ".join(str(value or "").strip().lower().split())


@lru_cache(maxsize=1)
def _load_catalog():
    if not CATALOG_PATH.exists():
        return {"states": [], "cities_by_state": {}}
    try:
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"states": [], "cities_by_state": {}}

    states = payload.get("states")
    cities_by_state = payload.get("cities_by_state")
    if not isinstance(states, list) or not isinstance(cities_by_state, dict):
        return {"states": [], "cities_by_state": {}}
    return {
        "states": [str(item).strip() for item in states if str(item).strip()],
        "cities_by_state": {
            str(state).strip(): [
                str(city).strip()
                for city in (cities if isinstance(cities, list) else [])
                if str(city).strip()
            ]
            for state, cities in cities_by_state.items()
            if str(state).strip()
        },
    }


def get_states():
    return _load_catalog()["states"]


def get_cities_by_state():
    return _load_catalog()["cities_by_state"]


def resolve_state_name(state_name):
    lookup = _normalize(state_name)
    if not lookup:
        return None
    for state in get_states():
        if _normalize(state) == lookup:
            return state
    return None


def get_cities_for_state(state_name):
    canonical_state = resolve_state_name(state_name)
    if not canonical_state:
        return None, []
    cities = get_cities_by_state().get(canonical_state) or []
    return canonical_state, cities
