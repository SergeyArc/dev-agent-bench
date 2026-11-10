"""toxiproxy control over its HTTP API (contract §7.6)."""

from __future__ import annotations

import httpx

from . import config


def _api() -> httpx.Client:
    return httpx.Client(base_url=config.TOXI_API, timeout=5.0)


def ensure_proxy(
    name: str = config.TOXI_AMQP_PROXY,
    listen: str = f"127.0.0.1:{config.TOXI_AMQP_PORT}",
    upstream: str = f"{config.AMQP_HOST}:{config.AMQP_PORT}",
) -> None:
    """Create the proxy if missing; make it enabled, with no toxics."""
    body = {"name": name, "listen": listen, "upstream": upstream, "enabled": True}
    with _api() as api:
        current = api.get(f"/proxies/{name}")
        if current.status_code == 404:
            api.post("/proxies", json=body).raise_for_status()
            return
        current.raise_for_status()
        data = current.json()
        for toxic in data.get("toxics", []):
            api.delete(f"/proxies/{name}/toxics/{toxic['name']}").raise_for_status()
        if data["listen"] != listen or data["upstream"] != upstream or not data["enabled"]:
            api.post(f"/proxies/{name}", json=body).raise_for_status()


def add_toxic(
    proxy: str, kind: str, attributes: dict[str, int], *, stream: str = "downstream"
) -> None:
    """Add a toxic (e.g. `timeout` with `{"timeout": 0}`: data is dropped and
    the connection stays open) to one direction of a proxy."""
    body = {"name": f"{kind}_{stream}", "type": kind, "stream": stream, "attributes": attributes}
    with _api() as api:
        api.post(f"/proxies/{proxy}/toxics", json=body).raise_for_status()


def delete_proxy(name: str) -> None:
    with _api() as api:
        response = api.delete(f"/proxies/{name}")
        if response.status_code != 404:
            response.raise_for_status()


def set_enabled(enabled: bool, name: str = config.TOXI_AMQP_PROXY) -> None:
    """Disabling closes the listener and drops every established connection."""
    with _api() as api:
        api.post(f"/proxies/{name}", json={"enabled": enabled}).raise_for_status()


def disable(name: str = config.TOXI_AMQP_PROXY) -> None:
    set_enabled(False, name)


def enable(name: str = config.TOXI_AMQP_PROXY) -> None:
    set_enabled(True, name)


def is_enabled(name: str = config.TOXI_AMQP_PROXY) -> bool:
    with _api() as api:
        response = api.get(f"/proxies/{name}")
        response.raise_for_status()
        return bool(response.json()["enabled"])
