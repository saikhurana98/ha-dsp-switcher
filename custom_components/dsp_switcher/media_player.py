"""Media player entities: one per zone, plus the console's now-playing head.

State convention for zones: the gateway has no per-zone power, only a mute, so
a zone reports ``on`` while unmuted and ``off`` while muted. Turning a zone off
mutes it and leaves its fader untouched, so turning it back on restores the
level it had.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import pct_to_db
from .const import (
    CMD_SPOTIFY_NEXT,
    CMD_SPOTIFY_PLAYPAUSE,
    CMD_SPOTIFY_PREVIOUS,
    CMD_ZONE_LEVEL,
    CMD_ZONE_MUTE,
    CMD_ZONE_SOURCE,
)
from .coordinator import DspSwitcherConfigEntry, DspSwitcherCoordinator
from .entity import DspSwitcherEntity

# MPRIS PlaybackStatus -> Home Assistant media player state.
_PLAYBACK_STATES = {
    "Playing": MediaPlayerState.PLAYING,
    "Paused": MediaPlayerState.PAUSED,
    "Stopped": MediaPlayerState.IDLE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DspSwitcherConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one media player per zone plus the now-playing head."""
    coordinator = entry.runtime_data
    entities: list[MediaPlayerEntity] = [
        DspSwitcherZoneMediaPlayer(coordinator, int(zone["output"]), str(zone["name"]))
        for zone in coordinator.zones
        if zone.get("output") is not None
    ]
    if coordinator.now_playing is not None:
        entities.append(DspSwitcherNowPlaying(coordinator))
    async_add_entities(entities)


class DspSwitcherZoneMediaPlayer(DspSwitcherEntity, MediaPlayerEntity):
    """One matrix output: volume, mute and exclusive source selection."""

    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(
        self, coordinator: DspSwitcherCoordinator, output: int, name: str
    ) -> None:
        """Bind the entity to one zone output."""
        super().__init__(coordinator, "zone", output)
        self._output = output
        self._attr_name = name

    @property
    def _zone(self) -> dict[str, Any]:
        return self.coordinator.zone(self._output) or {}

    @property
    def available(self) -> bool:
        """Drop out when the zone disappears from the gateway's config."""
        return super().available and bool(self._zone)

    @property
    def state(self) -> MediaPlayerState:
        """Off while muted, on otherwise -- the console has no zone power."""
        return MediaPlayerState.OFF if self._zone.get("muted") else MediaPlayerState.ON

    @property
    def volume_level(self) -> float | None:
        """Return the fader position as a 0..1 fraction."""
        pct = self._zone.get("volumePct")
        return None if pct is None else float(pct) / 100

    @property
    def is_volume_muted(self) -> bool | None:
        """Return the zone's output mute."""
        muted = self._zone.get("muted")
        return None if muted is None else bool(muted)

    @property
    def source_list(self) -> list[str]:
        """Return every configured source, whether or not it is routed here."""
        return self.coordinator.source_names()

    @property
    def source(self) -> str | None:
        """Return the loudest source currently crosspointed into this zone."""
        sends = self._zone.get("sends") or []
        if not sends:
            return None
        loudest = max(sends, key=lambda send: float(send.get("levelPct") or 0))
        return str(loudest.get("name")) or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw dB level and the full send list for automations."""
        return {
            "output": self._output,
            "volume_db": self._zone.get("volumeDb"),
            "volume_pct": self._zone.get("volumePct"),
            "sends": self._zone.get("sends") or [],
        }

    async def async_set_volume_level(self, volume: float) -> None:
        """Write the zone fader, converting the 0..1 fraction to dB."""
        await self.coordinator.async_send_command(
            {
                "type": CMD_ZONE_LEVEL,
                "zone": self._output,
                "level": pct_to_db(volume * 100),
            }
        )

    async def async_mute_volume(self, mute: bool) -> None:
        """Set the zone's output mute."""
        await self.coordinator.async_send_command(
            {"type": CMD_ZONE_MUTE, "zone": self._output, "mute": mute}
        )

    async def async_turn_on(self) -> None:
        """Unmute the zone."""
        await self.async_mute_volume(False)

    async def async_turn_off(self) -> None:
        """Mute the zone."""
        await self.async_mute_volume(True)

    async def async_select_source(self, source: str) -> None:
        """Exclusively route one source into this zone."""
        input_id = self.coordinator.input_for_name(source)
        if input_id is None:
            raise ValueError(f"Unknown source {source!r}")
        await self.coordinator.async_send_command(
            {"type": CMD_ZONE_SOURCE, "zone": self._output, "source": input_id}
        )


class DspSwitcherNowPlaying(DspSwitcherEntity, MediaPlayerEntity):
    """The primary Spotify head: metadata and transport, no volume.

    Volume lives on the zone entities, because what the console calls "now
    playing" is a single upstream player fanned out across the matrix.
    """

    _attr_name = "Now Playing"
    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.PLAY_PAUSE
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
    )

    def __init__(self, coordinator: DspSwitcherCoordinator) -> None:
        """Bind the entity to the gateway's now-playing block."""
        super().__init__(coordinator, "nowplaying", "primary")

    @property
    def _now(self) -> dict[str, Any]:
        return self.coordinator.now_playing or {}

    @property
    def state(self) -> MediaPlayerState:
        """Map the MPRIS PlaybackStatus onto a media player state."""
        now = self._now
        if not now.get("available"):
            return MediaPlayerState.OFF
        return _PLAYBACK_STATES.get(str(now.get("status") or ""), MediaPlayerState.IDLE)

    @property
    def media_title(self) -> str | None:
        """Return the track title."""
        return self._now.get("title") or None

    @property
    def media_artist(self) -> str | None:
        """Return the track artist."""
        return self._now.get("artist") or None

    @property
    def media_album_name(self) -> str | None:
        """Return the album name."""
        return self._now.get("album") or None

    @property
    def media_image_url(self) -> str | None:
        """Return the album art URL advertised by the player."""
        return self._now.get("artUrl") or None

    @property
    def media_image_remotely_accessible(self) -> bool:
        """Let the browser fetch https art directly; proxy anything else.

        Spotify serves art from i.scdn.co over https, so the common case skips
        the Home Assistant image proxy entirely. A plain-http URL (or none)
        falls back to proxying so a https dashboard does not block it.
        """
        url = self._now.get("artUrl") or ""
        return str(url).startswith("https://")

    @property
    def app_name(self) -> str | None:
        """Return the casting device name reported by the gateway."""
        return self._now.get("device") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose who is casting, which the console shows in its header."""
        return {
            "controller": self._now.get("controller"),
            "device": self._now.get("device"),
            "available": bool(self._now.get("available")),
        }

    async def async_media_play_pause(self) -> None:
        """Toggle playback on the primary player."""
        await self.coordinator.async_send_command({"type": CMD_SPOTIFY_PLAYPAUSE})

    async def async_media_play(self) -> None:
        """Resume: the gateway only exposes a toggle."""
        if self.state != MediaPlayerState.PLAYING:
            await self.async_media_play_pause()

    async def async_media_pause(self) -> None:
        """Pause: the gateway only exposes a toggle."""
        if self.state == MediaPlayerState.PLAYING:
            await self.async_media_play_pause()

    async def async_media_next_track(self) -> None:
        """Skip forward."""
        await self.coordinator.async_send_command({"type": CMD_SPOTIFY_NEXT})

    async def async_media_previous_track(self) -> None:
        """Skip back."""
        await self.coordinator.async_send_command({"type": CMD_SPOTIFY_PREVIOUS})
