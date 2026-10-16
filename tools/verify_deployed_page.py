"""Confirm the deployed page carries the elements and attributes the harnesses check locally."""

from __future__ import annotations

import urllib.request

SITE = "http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com"

EXPECTED = {
    # the connection state is announced by one off-screen sentence, not by the ticking line
    "quiet live region for connection state": 'id="conn-announce" class="sr-only" role="status" aria-live="polite"',
    "announced strip summary": 'id="band-strip-label" class="band-strip-label" role="status" aria-live="polite"',
    "change note": 'id="change-note"',
    "chart exposed as an image": 'role="img"',
    "chart can be focused": 'id="history-canvas" height="220" role="img" tabindex="0"',
    "no-script fallback": "<noscript",
    "backdrop hidden from assistive tech": 'class="bg-layer" aria-hidden="true"',
    # the incident record is a table for assistive tech, with real column headers
    "incident table header roles": 'class="incident-head" role="rowgroup"',
    "incident column headers": 'role="columnheader"',
    "band strip": 'id="band-strip"',
    "ranking heading before its list": 'id="ranking-title"',
    "privacy/attribution footer": "Open-Meteo",
}

FORBIDDEN = {
    # a marker left over from the fix would mean the ticking line is still being announced
    "the ticking status line is a live region": 'class="conn" role="status"',
    # the lists are re-rendered whole every poll and their rows carry reading ages and
    # incident durations, so a live region over them re-announced the same rows for ever
    "the city card list is a live region": 'id="city-cards" class="city-cards" aria-live',
    "the zone grid is a live region": 'id="zones" class="zone-grid" aria-live',
    "the active alert list is a live region": 'id="alerts-active" class="alert-list" aria-live',
    "the resolved incident list is a live region": 'id="alerts-closed" class="alert-list" aria-live',
    "the incident column headers are hidden from assistive tech": 'class="incident-head" aria-hidden="true"',
    "the page disables focus outlines": "outline: none",
}

html = urllib.request.urlopen(SITE + "/index.html", timeout=20).read().decode("utf-8")
print(f"deployed index.html: {len(html)} characters from {SITE}")
failed = 0
for label, needle in EXPECTED.items():
    ok = needle in html
    failed += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
for label, needle in FORBIDDEN.items():
    present = needle in html
    failed += 1 if present else 0
    print(f"  {'FAIL' if present else 'ok  '} not present: {label}")

NEEDLES = {
    "lib.js": {
        "the leader's reading age is exposed": "leaderAgeSeconds",
        "a silent zone is identified": "staleZoneIds",
        "a silent zone is marked on an incident": "zone not reporting",
        "the incident rows carry table roles": 'role="row"',
    },
    "app.js": {
        "the chart answers the arrow keys": "handleHistoryKey",
        "the chart's label names the point": "historyAriaLabel",
        "a missing API base is reported": "no API base configured",
        "incidents know which zones are silent": "staleZoneIds",
    },
    "styles.css": {
        "change note": "change-note",
        "the scrollable table has a focus ring": ".table-scroll:focus-visible",
    },
}
for asset, wanted in NEEDLES.items():
    body = urllib.request.urlopen(f"{SITE}/{asset}", timeout=20).read().decode("utf-8")
    missing = [label for label, needle in wanted.items() if needle not in body]
    failed += len(missing)
    print(
        f"  {'ok  ' if not missing else 'FAIL'} {asset} served ({len(body)} characters)"
        + (f" — missing: {', '.join(missing)}" if missing else "")
    )

print(f"\n{'all deployed page contracts hold' if not failed else str(failed) + ' contract(s) failed'}")
raise SystemExit(1 if failed else 0)
