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
    # the deployed page must carry the security policy itself, not just locally: a policy that
    # only exists in the repository protects nobody
    "content security policy": '<meta http-equiv="Content-Security-Policy"',
    "no inline script is allowed": "script-src 'self'",
    "no plugins, no form posts": "object-src 'none'; base-uri 'none'; form-action 'none'",
    "the API origin is still reachable": "connect-src 'self' https://",
    "referrer policy": '<meta name="referrer" content="strict-origin-when-cross-origin">',
    "the API base is page data, not a script": 'data-api="https://',
    "external links cannot reach back through window.opener": 'rel="noopener noreferrer"',
    # M9: the fifth section, the status surface and the guidance the About page carries
    "the About section is deployed": 'id="view-about"',
    "the status surface is a disclosure": 'class="health-summary"',
    "the status is announced as a state, not a number": 'id="health-word"',
    "the flood safety guidance is deployed": "Flood safety guidance",
    "the guidance defers to the authorities": "does <b>not</b> issue official warnings",
    "the meteorological department is linked": "mausam.imd.gov.in",
    "the disaster management authority is linked": "ndma.gov.in",
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
    # script executed from the markup would defeat the policy above
    "an inline script": "<script>",
    "a javascript: URL": "javascript:",
    # M9: the page never claims a delivery it cannot know about, and never loads an icon from a CDN
    "a claim that an email was sent": "email sent",
    "an icon loaded from a CDN": "cdn.",
    "an icon font": "material-icons",
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
        # M9: the parts a reader meets on every page
        "the icon set is deployed": "ICON_PATHS",
        "an icon is inline SVG hidden from assistive tech": 'aria-hidden="true" focusable="false"',
        "a placeholder is built, not written by hand": "function cityCardsSkeleton",
        "a placeholder does not pretend to be a card": "skel-card",
        "every empty state comes from one builder": "function emptyState",
        "the delivery panel states what the API does not expose": "Not reported to this page",
        "the publishing wording is the honest one": "published to the notification topic",
        "the status counts reporting cities in words": "reporting'",
    },
    "app.js": {
        "the chart answers the arrow keys": "handleHistoryKey",
        "the chart's label names the point": "historyAriaLabel",
        "a missing API base is reported": "no API base configured",
        "incidents know which zones are silent": "staleZoneIds",
        # M9: the router knows five sections, the retry is a retry, the chart has text
        "the router knows the About section": "renderAbout",
        "the system status is rendered from the payload": "renderSystemHealth",
        "the retry re-requests instead of reloading": 'data-action="retry"',
        "the empty fleet is a named state": "key: 'no-cities'",
        "the chart has a text equivalent on the page": "history-summary",
    },
    "styles.css": {
        "change note": "change-note",
        "the scrollable table has a focus ring": ".table-scroll:focus-visible",
        # M9: the layout rules that keep a phone from scrolling sideways
        "the phone breakpoint is deployed": "@media (max-width: 400px)",
        "payload text is told how to wrap": "overflow-wrap: anywhere",
        "the skeleton is styled": "skel-sweep",
        "a placeholder has its own shape": ".skel-card",
        "the touch target rule is deployed": "min-height: 44px",
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
