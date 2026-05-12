"""/performance + /track_record alias — public hit-rate readout in Telegram.

Stage 8-P9. Replaces the Stage 7-era stub that lived in `handlers/help.py`.

Renders the same data as the web `/track-record` page (Stage 8-P6):
summary aggregate + the last 5 evaluated outcomes with one-line verdicts.
`/performance` is the advertised primary; `/track_record` (underscore — bot
command names cannot contain hyphens per Telegram's spec) is an unadvertised
alias for users who guess the longer form. Both route to the same handler.

Owns its own `async_session()` per anti-drift rule D17. Reuses
`pipeline.track_record.get_stats_by_confidence_floor` (the cohort-stat
helper from P8) for the GLOBAL hit-rate (floor=1 sums every confidence)
AND `pipeline.track_record.get_recent_outcomes` for the last-5 list.
Two queries per command invocation — well under any rate-limit concern,
no caching needed at the bot layer (cohort stats refresh hourly via the
outcome_eval scheduler, command latency dominated by Telegram round-trip).
"""

from __future__ import annotations

import html

from telegram import Update
from telegram.ext import ContextTypes

from etfpulse.config import settings
from etfpulse.db import async_session
from etfpulse.models import SignalOutcome
from etfpulse.pipeline.track_record import (
    compute_hit_rate_pct,
    get_recent_outcomes,
    get_stats_by_confidence_floor,
)

# Last-N outcomes shown in the readout. Five lines is the sweet spot —
# enough recency to feel honest, not so many that the message scrolls.
# Aligns with the Stage 8 design doc's example.
_RECENT_LIMIT = 5


async def cmd_track_record(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/performance` (and the unadvertised `/track_record` alias) — render
    the public hit-rate snapshot to the Telegram chat that issued the command.

    Single read transaction. No mutations. Safe in DM and group contexts —
    the readout is identical for every caller (no per-user filtering).
    """
    if update.effective_message is None:
        return

    async with async_session() as session:
        stat = await get_stats_by_confidence_floor(session)
        recent = await get_recent_outcomes(session, limit=_RECENT_LIMIT)

    text = _format_track_record_message(stat_global=stat.by_floor[1], recent=recent)
    await update.effective_message.reply_html(text)


# Alias — `_COMMANDS` registers both names. Keeping `cmd_performance` as
# a separate symbol (rather than a string alias in the registry) makes
# the bound function show up in greps for `cmd_performance` and means the
# handler-test surface tests both names without duplicating fixtures.
cmd_performance = cmd_track_record


# ---------------------------------------------------------------------------
# Message rendering — pure function for testability.
# ---------------------------------------------------------------------------


def _format_track_record_message(
    *, stat_global: tuple[int, int], recent: list[SignalOutcome]
) -> str:
    """Compose the HTML message body. Pure — no DB, no clock, easy to test.

    `stat_global` is the floor=1 tuple (cumulative across all confidences),
    not the per-confidence breakdown. The bot command surfaces the GLOBAL
    rate; per-cohort stats live on the per-signal alert (Stage 8-P8) and
    on the web page (P6). Three different surfaces, three different cuts
    of the same underlying outcome data — that's the design intent.

    Three render paths:
      1. `recent` empty → TRUE cold boot, no outcomes at all → "no outcomes
         evaluated yet" caption. Same 72h-delay copy as the web
         HeroHitRatePanel and TrackRecord page (one truth across surfaces).
      2. `recent` non-empty AND `targeted > 0` → hit-rate summary + list.
      3. `recent` non-empty AND `targeted == 0` (all outcomes were AI-declined-
         target) → list-only with a "hit rate not yet computable" note.
         Beats showing the cold-boot caption when we DO have data, and beats
         rendering "0%" for an empty cohort.
    """
    targeted, hits = stat_global

    # Path 1 — true cold boot.
    if not recent:
        return (
            "📊 <b>Signal track record</b>\n\n"
            "<i>No outcomes evaluated yet — first outcomes land 72h after "
            "a signal fires.</i>"
        )

    parts: list[str] = ["📊 <b>Signal track record</b>", ""]

    if targeted > 0:
        # Path 2 — hit-rate summary. Helper returns float (2dp); we render
        # as integer here — precision is a presentation concern and Telegram
        # messages prefer compact whole-percent. Helper only returns None
        # when targeted == 0, which the outer guard already ruled out.
        pct = compute_hit_rate_pct(hits, targeted)
        assert pct is not None  # noqa: S101 — narrows the Optional for the round() below
        hit_rate = round(pct)
        parts.append(f"<b>Total evaluated:</b> {targeted}")
        parts.append(f"<b>Targets hit:</b> {hits} ({hit_rate}%)")
    else:
        # Path 3 — outcomes exist but none had a target.
        parts.append("<i>Hit rate not yet computable — no signals with targets evaluated yet.</i>")

    parts.append("")
    parts.append(f"<b>Last {len(recent)}:</b>")
    for outcome in recent:
        parts.append(_format_outcome_line(outcome))

    # "Full record:" footer link — only when the SPA URL is configured.
    # Uses `frontend_url` because the track-record page is a frontend route
    # (`/track-record` on the SPA), NOT a backend webhook route. Frontend and
    # backend are typically separate domains (Vercel + Coolify). When unset
    # (dev / bot-disabled-mode), skip the line cleanly rather than rendering
    # a broken link. URL is `html.escape`d defensively — operator config could
    # legitimately contain `&` (UTM params, etc.) which would otherwise break
    # Telegram's HTML parser. The path keeps the hyphen — that's a frontend
    # route segment, not a Telegram bot command, so hyphens are fine here.
    if settings.frontend_url:
        url = html.escape(f"{settings.frontend_url.rstrip('/')}/track-record")
        parts.append("")
        parts.append(f"<i>Full record: {url}</i>")

    return "\n".join(parts)


def _format_outcome_line(o: SignalOutcome) -> str:
    """One-line verdict for the Last-N list:

        ✅ #42 BTC long → target hit (+3.2%)
        ❌ #41 ETH short → stop hit (-1.8%)
        ⏳ #38 BTC long → no target set
        — #37 ETH long → neither hit (-0.4%)

    Icon priority: ✅ target wins over ❌ stop (intra-day wick where both
    triggered → user-favorable read, same convention as the frontend
    OutcomeCard verdict picker AND the TrackRecord page row). ⏳ for
    no-target signals, em-dash for "neither hit" — distinct because the
    second case had a target and missed it (informational), the first
    case had no target at all (the AI declined).

    Dynamic fields (`asset`, `direction`) are `html.escape`d defensively —
    they're internal-only today (asset from a Literal, direction from
    SignalDirection enum), but `signal_outcomes.asset` is `String(10)` with
    no DB CHECK so a future migration could leak a non-escape-safe value.
    Matches the codebase-wide policy from `delivery.format_signal_message`.
    The verdict literal is hardcoded above so it doesn't need escaping.
    """
    icon, verdict = _outcome_icon_and_verdict(o)
    pct = _percent_return_text(o)
    asset = html.escape(o.asset)
    direction = html.escape(o.direction)
    return f"{icon} #{o.signal_id} {asset} {direction} → {verdict}{f' ({pct})' if pct else ''}"


def _outcome_icon_and_verdict(o: SignalOutcome) -> tuple[str, str]:
    if o.hit_target is True:
        return ("✅", "target hit")
    if o.hit_stop is True:
        return ("❌", "stop hit")
    if o.hit_target is None:
        return ("⏳", "no target set")
    return ("—", "neither hit")


def _percent_return_text(o: SignalOutcome) -> str | None:
    """Same baseline contract as `pipeline/track_record._evaluate_one`'s
    `entry_for_metrics` AND the frontend OutcomeCard / TrackRecord row —
    `entry_price` if AI set one, else `price_at_signal`. Diverging would
    show one number on the alert and a different number in the bot,
    contradicting the verdict's hit_target math when entry ≠ price_at_signal."""
    if o.price_after_72h is None:
        return None
    baseline = o.entry_price if o.entry_price is not None else o.price_at_signal
    if baseline <= 0:
        return None
    pct = (float(o.price_after_72h - baseline) / float(baseline)) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


__all__ = ["cmd_track_record", "cmd_performance"]
