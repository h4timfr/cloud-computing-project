"""Confirm the deployed page carries the elements and attributes the harnesses check locally."""

from __future__ import annotations

import re
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
    # M10: the worked example the methodology page fills in at runtime has somewhere to land
    "the methodology page has a slot for the worked example": 'id="score-breakdown"',
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
        # M10: the served copy is the plain one, and the model is folded rather than removed
        "a variable name is turned into words before it is shown": "function driverLabel",
        "an unknown variable is humanised instead of printed raw": "function humaniseVariable",
        "the plain reason sentence is composed, not pasted": "function reasonSentence",
        "the city page folds the model behind a question": "how is this risk calculated?",
        "the fold still prints the weighted base": "weighted base",
        "the fold quotes the API's own output line": "Raw model output, as returned by the API",
        "the city page keeps the driver's human name": "is the main contributor",
        "the spotlight names the main risk factor": "Main risk factor",
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
        # M10: the methodology page publishes a worked example, folded, from the live reading
        "the worked example is built from the payload": "score-breakdown",
        "the spotlight reuses the shared plain sentence": "FP.plainRiskSummary",
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
        # M10: the spotlight sentence is set as prose, and the model's own words are set apart
        "the spotlight sentence is styled": ".hero-reason",
        "the raw model line is styled": ".raw-model",
        "the worked example is styled": ".breakdown-list",
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

# ------------------------------------------------------------- what the assets must NOT say ---
# A needle test proves a string is being served; it cannot prove the copy that leaked Model
# internals onto the front page was actually removed. That is what browser QA found on the
# deployed Overview ("risk_score 0.3973 = weighted base 0.3973 (soil_saturation_pct=..."), so the
# served assets are checked for the shape of that line as well.
FORBIDDEN_ASSETS = {
    "app.js": {
        # the spotlight must not print the model's own line or a raw driver list. The API string
        # itself is still read on this page - inside the folded worked example - so the needle is
        # the renderer that leaked it, not the field name: "latest.explanation" appears in the
        # methodology fold on purpose, which is why the runtime harness (which reads the rendered
        # page rather than the source) is the check that proves the leak is gone.
        "the Overview prints a dominant-driver list": "hero-drivers",
        "the Overview label names the model": "heuristic-v1 score",
        "the Overview still prints the old explanation block": "hero-explain",
        "the city list patches a raw explanation into the page": "('explanation'",
    },
    "lib.js": {
        # a variable name may appear inside the fold, but it must never be the label of one
        "a driver is labelled with its variable name": "metricMeta(driver).label",
        "a city card carries an unexplained weighted contribution": "metric-weight",
        "the score caption is model vocabulary": "heuristic score 0-1",
    },
}
for asset, forbidden in FORBIDDEN_ASSETS.items():
    body = urllib.request.urlopen(f"{SITE}/{asset}", timeout=20).read().decode("utf-8")
    present = [label for label, needle in forbidden.items() if needle in body]
    failed += len(present)
    print(
        f"  {'FAIL' if present else 'ok  '} {asset} carries no engineering copy in a public view"
        + (f" — found: {', '.join(present)}" if present else "")
    )

# --------------------------------------------------------------------- the policy itself ---
# The page carries its policy in a <meta> element, so this compares the served directive list
# rather than looking for a substring: a directive that is present but was quietly weakened is
# exactly what a substring test would miss.
REQUIRED_DIRECTIVES = {
    "default-src": "'none'",
    "script-src": "'self'",
    "style-src": "'self' 'unsafe-inline'",
    "img-src": "'self' data:",
    "font-src": "'self'",
    "object-src": "'none'",
    "base-uri": "'none'",
    "form-action": "'none'",
}
# A browser ignores these in a <meta> policy and logs a warning: frame-ancestors and sandbox need a
# real response header, and S3 static website hosting cannot send one. Documented in README.md.
META_IGNORED = ["frame-ancestors", "sandbox", "report-uri", "report-to"]

policies = [
    match.group(1) for match in re.finditer(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', html)
]
policy_ok = len(policies) == 1
if not policy_ok:
    print(f"  FAIL the page declares {len(policies)} policies")
policy = policies[0] if policies else ""
directives = {}
for part in policy.split(";"):
    name, _, value = part.strip().partition(" ")
    if name:
        directives[name] = value.strip()

for name, value in REQUIRED_DIRECTIVES.items():
    ok = directives.get(name) == value
    policy_ok = policy_ok and ok
    print(f"  {'ok  ' if ok else 'FAIL'} {name} is {value!r}"
          + ("" if ok else f" (served: {directives.get(name)!r})"))

connect = directives.get("connect-src", "")
api = re.search(r'data-api="(https://[^"]+)"', html)
for label, needle in [
    ("the page can reach its own origin", "'self'"),
    ("the page can reach its API", api.group(1) if api else "https://"),
    ("local development stays possible", "http://localhost:"),
]:
    ok = needle in connect
    policy_ok = policy_ok and ok
    print(f"  {'ok  ' if ok else 'FAIL'} connect-src: {label}")

for name in sorted(set(directives) - set(REQUIRED_DIRECTIVES)):
    if name != "connect-src":
        print(f"  note the served policy also declares {name}")
for name in META_IGNORED:
    ok = name not in directives
    policy_ok = policy_ok and ok
    print(f"  {'ok  ' if ok else 'FAIL'} not present in the policy: {name} (a meta element cannot enforce it)")
failed += 0 if policy_ok else 1

print(f"\n{'all deployed page contracts hold' if not failed else str(failed) + ' contract(s) failed'}")
raise SystemExit(1 if failed else 0)
