"""Feature-owned OBS items, applied over obs-websocket on every OBS connect.

A feature declares the scenes, scene items (with the input they show) and
filters it needs. :func:`apply_items` makes OBS match the feature's switch:

* on: create whatever is missing, then enable / show every declared item;
* off: disable / hide the declared items that exist, create nothing.

Only missing things are created, with the declared settings. An existing
input, scene or filter keeps the host's settings, transform and order; the
switch only decides whether it is enabled or visible. Scenes are never
deleted or hidden (a switch that is off leaves them as they are).

Every item is applied on its own: one failing request is logged and the
rest still run. ``apply_items`` never raises (except cancellation).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Protocol

from .obs import CODE_RESOURCE_NOT_FOUND, ObsRequestFailed

log = logging.getLogger(__name__)


class ObsRequester(Protocol):
    async def request(self, request_type: str, data: dict | None = None) -> dict | None: ...


@dataclass(frozen=True)
class Scene:
    name: str


@dataclass(frozen=True)
class SceneItem:
    """``source`` shown in ``scene``. ``kind`` creates the input if it is missing
    (``None``: the source must already exist, e.g. a scene or the host's camera)."""

    scene: str
    source: str
    kind: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    # Applied only when the scene item is created (obs-websocket SceneItemTransform).
    transform: dict[str, Any] | None = None


@dataclass(frozen=True)
class Filter:
    source: str
    name: str
    kind: str
    settings: dict[str, Any] = field(default_factory=dict)


Item = Scene | SceneItem | Filter


@dataclass
class ApplyResult:
    created: list[str] = field(default_factory=list)
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _label(item: Item) -> str:
    if isinstance(item, Scene):
        return f"scene {item.name!r}"
    if isinstance(item, SceneItem):
        return f"{item.source!r} in {item.scene!r}"
    return f"filter {item.name!r} on {item.source!r}"


def _missing(exc: Exception) -> bool:
    return isinstance(exc, ObsRequestFailed) and exc.code == CODE_RESOURCE_NOT_FOUND


async def _scene_names(obs: ObsRequester) -> set[str]:
    data = await obs.request("GetSceneList") or {}
    return {s.get("sceneName") for s in data.get("scenes", []) if isinstance(s, dict)}


async def _input_names(obs: ObsRequester) -> set[str]:
    data = await obs.request("GetInputList") or {}
    return {i.get("inputName") for i in data.get("inputs", []) if isinstance(i, dict)}


async def _scene_item_id(obs: ObsRequester, scene: str, source: str) -> int | None:
    try:
        data = await obs.request("GetSceneItemId", {"sceneName": scene, "sourceName": source})
    except ObsRequestFailed as exc:
        if _missing(exc):
            return None
        raise
    return (data or {}).get("sceneItemId")


async def _filter_exists(obs: ObsRequester, source: str, name: str) -> bool:
    try:
        await obs.request("GetSourceFilter", {"sourceName": source, "filterName": name})
    except ObsRequestFailed as exc:
        if _missing(exc):
            return False
        raise
    return True


async def _apply_scene(obs: ObsRequester, item: Scene, on: bool, res: ApplyResult) -> None:
    if not on:
        return
    if item.name not in await _scene_names(obs):
        await obs.request("CreateScene", {"sceneName": item.name})
        res.created.append(_label(item))


async def _apply_scene_item(
    obs: ObsRequester, item: SceneItem, on: bool, res: ApplyResult
) -> None:
    item_id = await _scene_item_id(obs, item.scene, item.source)
    if item_id is None:
        if not on:
            return
        if item.kind is not None and item.source not in await _input_names(obs):
            data = await obs.request(
                "CreateInput",
                {
                    "sceneName": item.scene,
                    "inputName": item.source,
                    "inputKind": item.kind,
                    "inputSettings": dict(item.settings),
                    "sceneItemEnabled": True,
                },
            )
        else:
            data = await obs.request(
                "CreateSceneItem",
                {"sceneName": item.scene, "sourceName": item.source, "sceneItemEnabled": True},
            )
        item_id = (data or {}).get("sceneItemId")
        res.created.append(_label(item))
        if item.transform and item_id is not None:
            await obs.request(
                "SetSceneItemTransform",
                {"sceneName": item.scene, "sceneItemId": item_id,
                 "sceneItemTransform": dict(item.transform)},
            )
    if item_id is None:
        raise RuntimeError(f"no scene item id for {_label(item)}")
    await obs.request(
        "SetSceneItemEnabled",
        {"sceneName": item.scene, "sceneItemId": item_id, "sceneItemEnabled": on},
    )
    (res.enabled if on else res.disabled).append(_label(item))


async def _apply_filter(obs: ObsRequester, item: Filter, on: bool, res: ApplyResult) -> None:
    if not await _filter_exists(obs, item.source, item.name):
        if not on:
            return
        await obs.request(
            "CreateSourceFilter",
            {"sourceName": item.source, "filterName": item.name, "filterKind": item.kind,
             "filterSettings": dict(item.settings)},
        )
        res.created.append(_label(item))
    await obs.request(
        "SetSourceFilterEnabled",
        {"sourceName": item.source, "filterName": item.name, "filterEnabled": on},
    )
    (res.enabled if on else res.disabled).append(_label(item))


async def apply_items(
    obs: ObsRequester, items: list[Item] | tuple[Item, ...], on: bool, *, owner: str = ""
) -> ApplyResult:
    """Make ``items`` match the switch ``on`` (see the module docstring)."""
    res = ApplyResult()
    for item in items:
        step: Awaitable[None]
        if isinstance(item, Scene):
            step = _apply_scene(obs, item, on, res)
        elif isinstance(item, SceneItem):
            step = _apply_scene_item(obs, item, on, res)
        else:
            step = _apply_filter(obs, item, on, res)
        try:
            await step
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            res.failed.append(_label(item))
            log.warning("%s: cannot apply %s: %r", owner or "obs items", _label(item), exc)
    if res.created:
        log.info("%s: created %s", owner or "obs items", ", ".join(res.created))
    return res
