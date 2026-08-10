# DSP Switcher Audio Console

Control the [dsp-switcher](https://github.com/saikhurana98/dsp-switcher) audio
gateway — and the Biamp Tesira behind it — from Home Assistant.

Every zone becomes a media player with volume, mute and source selection. The
master fader and per-source trims become numbers, source enable pills become
switches, and each streaming source reports `streaming` / `idle` / `offline`
with its current track. A "Now Playing" entity carries the album art and
transport controls. Five services expose the routing matrix for automations.

Local polling over HTTP with a long-lived API token. No cloud.

**Setup:** mint a token in the console (Normalize → ACCESS TOKENS → GENERATE),
then add the integration with your gateway's base URL and that token.
