"""Tiny read helpers shared by outlook/analysis without importing reports (which imports many modules)."""
import store


def watchlist():
    wl = store.read_json("watchlist.json", {})
    return sorted(({"symbol": k, **v} for k, v in wl.items()), key=lambda x: -x.get("score", 0))
