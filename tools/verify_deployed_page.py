"""Confirm the deployed page carries the elements and attributes the harnesses check locally."""

from __future__ import annotations

import hashlib
import pathlib
import re
import urllib.request

SITE = "http://floodpulse-dashboard-725934295966.s3-website.eu-north-1.amazonaws.com"
WEB = pathlib.Path(__file__).resolve().parents[1] / "web"

EXPECTED = {
    # the connection state is announced by one off-screen sentence, not by the ticking line
    "quiet live region for connection state": 'id="conn-announce" class="sr-only" role="status" aria-live="polite"',
    "announced strip summary": 'id="band-strip-label" class="band-strip-label" role="status" aria-live="polite"',
    "change note": 'id="change-note"',
    "chart exposed as an image": 'role="img"',
    # the chart is an inline SVG now, not a canvas: what matters is that it is still focusable and
    # still exposed as an image to assistive tech
    "chart can be focused": 'role="img" tabindex="0"',
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
    # M10 (premium pass): the hero, the three-step explanation, the alert preview and the form
    "the hero is a card that ships with its own answer": 'id="hero" class="card hero hero-live"',
    "the hero answer is a polite live region": 'id="ov-headline"',
    "the hero says what the product does": "What FloodPulse does",
    "the hero links to the method": "Learn how risk is calculated",
    "the alert preview is on the Overview": 'id="alert-preview-card"',
    "the three steps are deployed": 'class="works-steps"',
    "the subscription card is deployed": 'id="subscribe-card"',
    "the subscription field has a real label": 'for="subscribe-email"',
    "the subscription form does not navigate": '<form id="subscribe-form" class="subscribe-form" novalidate>',
    "the subscription outcome is a polite live region": 'id="subscribe-result" class="subscribe-result" role="status" aria-live="polite" hidden',
    # the notes were rewritten in the plain voice and the AWS branding left the page: the three claims
    # the check exists for are unchanged - a confirmation arrives, no marketing, every email carries a
    # way out
    "the confirmation note is deployed": "receive a confirmation email",
    "the privacy note is deployed": "No marketing email",
    "the unsubscribe instruction is deployed": "You can unsubscribe from any alert email",
    # the model left the header and lives in the folded technical panel instead
    "the model is in the folded technical panel": '<b id="summary-model">heuristic-v1</b>',
    # M13: twelve coastal cities, and the three things that make finding one of them possible
    "the search box is labelled for what it searches": 'id="cities-search-label"',
    "the toolbar is a search landmark": 'class="cities-toolbar" id="cities-toolbar" role="search"',
    "the result count is a polite status region": 'id="cities-count" role="status" aria-live="polite"',
    "the empty state has its own element": 'id="cities-empty"',
    "the way to the rest of the fleet leads to the Cities page": 'id="city-cards-more" href="#/cities"',
    "the sort control offers the three orders": '<option value="fresh">Freshness</option>',
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
    # M10: the model name belongs in the folded panel, not in the primary header, and a public
    # page never names the notification topic, an ARN or an account
    "the model name is back in the primary header": 'meta-item">model <b id="summary-model"',
    "an AWS resource name is on the page": "arn:aws",
    "the notification topic is named": "floodpulse-alerts",
    "the account id is on the page": "725934295966",
    # and no view ever claims a subscription that SNS has not confirmed
    "a subscription claim SNS has not confirmed": "You are subscribed",
    "a subscription claim SNS has not confirmed (2)": "subscribed successfully",
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
        # M11.2: the result sentence names the filters, and never the raw enum
        "the result sentence names the filters in force": 'cityFilterLabels',
        "an empty result says so in words": "' match these filters.'",
        "the leader's reading age is exposed": "leaderAgeSeconds",
        "a silent zone is identified": "staleZoneIds",
        "a silent zone is marked on an incident": "not reporting</span>",
        "the incident rows carry table roles": 'role="row"',
        # M9: the parts a reader meets on every page
        "the icon set is deployed": "ICON_PATHS",
        "an icon is inline SVG hidden from assistive tech": 'aria-hidden="true" focusable="false"',
        "a placeholder is built, not written by hand": "function cityCardsSkeleton",
        "a placeholder does not pretend to be a card": "skel-card",
        "every empty state comes from one builder": "function emptyState",
        "the delivery panel states what the API does not expose": "Not reported to this page",
        # the wording moved from e-mail plumbing to what the reader can expect: how often mails may go out,
    # and that a confirmation has to be accepted before any of them arrive
    "the publishing wording is the honest one": "at most one email per city every",
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
        # M10: the subscription vocabulary exists once, in this module
        "the shared subscription copy is deployed": "function subscriptionResult",
        "the accepted state describes a confirmation request": "Confirm it to activate FloodPulse alerts",
        "the outcome of a status code is mapped in one place": "function subscriptionKindForStatus",
        "the calm alert state is composed, not static": "No active flood alerts",
        "the city plate is decorative": 'class="city-thumb band-',
        "the meter marks the documented band edges": "SCORE_BAND_EDGES",
        "the mail icon is deployed": "mail:",
        # M10.1: the prepared nightscapes, chosen by an explicit table and never by payload text
        "the zone -> image table is deployed": "CITY_IMAGES",
        "the lookup is an own-property test": "hasOwnProperty.call(CITY_IMAGES",
        "a card image is decorative and reserves its box": 'alt="" width="800" height="450"',
        "every card image below the first row is lazy": 'loading="lazy"',
        "the image is decoded off the main thread": 'decoding="async"',
        # M13: the coastal fleet
        "the chips are built beside the other renderers": 'function filterChips(kind, options, active)',
        "a chip says whether it is pressed": 'aria-pressed=',
        "the fleet's scope is stated, not implied": 'function fleetScopeNote(payload)',
        "the drawn plate names the coast of its city": 'city-thumb-coast',
        "a city card names its state and coast group": 'data-f="city_state"',
        "an alert row names the city and the state": 'function incidentZoneLabel(incident)',
        "a city card says how to open it": 'function openCityLabel(noun)',
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
        # M10: one write to one public route, and the hero is patched rather than rebuilt
        "the form posts to the subscription route": "API_BASE + '/subscriptions'",
        "the hero is patched field by field": "setText('hero-monitor'",
        "the band edges have one definition": "FP.SCORE_BAND_EDGES",
        "the alert preview reads the same incidents as the alerts page": "renderAlertPreview",
        # M13: the coastal fleet
        "the Overview shows a named limit, not the fleet": 'OVERVIEW_CITY_LIMIT = 5',
        "the Overview counts the fleet from the payload": 'payload.zone_count',
        "the empty state offers the way back": 'clear-filters',
        "the risk filter offers the API's own floors": 'RISK_FILTERS',
        "a filter change never refetches": 'renderCitiesPage();',
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
        # M11.2: the control bar is a grid with a tier per width, and a metric row is four columns
        "the desktop bar is one row": "grid-template-areas: 'search coast risk sort';",
        "the tablet bar is two rows": "grid-template-areas: 'search sort' 'coast risk';",
        "the phone bar is one column": "grid-template-areas: 'search' 'coast' 'risk' 'sort';",
        # M14: pinned to the measured floors. That the floors fit the narrowest desktop card is
        # asserted as arithmetic in tools/dashboard_check.mjs, where a layout change is made; this
        # file only checks that what S3 serves is what the repository holds.
        "a metric row declares four columns that fit a 768px card":
        "grid-template-columns: minmax(84px, 1fr) auto minmax(64px, auto) minmax(44px, 1.1fr)",
        "the phone metric row is two lines": "grid-template-areas: 'name source' 'value track';",
        # M10: the spotlight sentence is set as prose, and the model's own words are set apart
        "the spotlight sentence is styled": ".hero-reason",
        "the raw model line is styled": ".raw-model",
        "the worked example is styled": ".breakdown-list",
        # M10: the premium layer, the form and the two new decorations
        "the premium layer is deployed": "M10 - premium pass",
        "the backdrop is centred and scrimmed": ".bg-layer::after",
        "the subscription card is styled": ".subscribe-result",
        "the city meter is styled": ".city-meter-fill",
        "the header counters are chips": ".meta-item",
        "the three steps are styled": ".works-step",
        "the subscriber's field is styled": ".subscribe-input",
        # M10.1: the card band's shape, crop and grid
        "the card band has a fixed 16:9 ratio": "aspect-ratio: 16 / 9",
        "the image covers its band": "object-fit: cover",
        "the wide desktop gets one row of five": "repeat(5, minmax(0, 1fr))",
        "the phone gets one column": "grid-template-columns: minmax(0, 1fr)",
        "the band gradient is deployed": ".city-thumb::after",
        # M10.2: the backdrop is a ramp in front of the photograph, not a mask over it
        "the new backdrop wash is deployed": "radial-gradient(128% 96% at 50% 2%",
        "the edge vignette is deployed": ".bg-layer::after",
        "the ordinary card surface is deployed": "--surface-card",
        "the header surface is deployed": "--topbar-surface",
        "the hero keeps its cool edge": "--hero-edge",
        # M13: the coastal fleet
        "a chip reuses the button style and keeps its target size": '.filter-chip',
        "the drawn plate's coast label is styled": '.city-thumb-coast',
        "the empty state is styled": '.cities-empty',
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
        # M10: the page never renders what AWS said, only what it means
        "the page renders an AWS error class": "AuthorizationError",
        "the page renders a subscription ARN": "SubscriptionArn",
        "the page claims the address is subscribed": "You are subscribed",
        # M10.1: the seven PNGs named *.svg are not an icon system and must stay unreferenced
        "an icon is loaded from one of the PNG files named *.svg": "-icon.svg",
        "the chevron is loaded from a file": "chevron-right.svg",
        "a card image comes from outside the same directory": 'src="http',
    },
    "styles.css": {
        # M10.2: the two rules that made the photograph invisible must not come back to the
        # deployed stylesheet, whatever the local file happens to say
        "the backdrop is blended in a mode that discards its darks": "mix-blend-mode: screen",
        "the body repaints itself on every scroll": "background-attachment: fixed",
    },
}
for asset, forbidden in FORBIDDEN_ASSETS.items():
    body = urllib.request.urlopen(f"{SITE}/{asset}", timeout=20).read().decode("utf-8")
    # The stylesheet's comments quote the rules they replaced - "mix-blend-mode: screen", "not
    # background-attachment: fixed" - on purpose, so the search runs against the declarations only.
    # A comment is not a rule; a rule is still caught, which is the point of the entry. The static
    # harness strips comments for the same reason.
    searched = re.sub(r"/\*[\s\S]*?\*/", " ", body) if asset.endswith(".css") else body
    present = [label for label, needle in forbidden.items() if needle in searched]
    failed += len(present)
    quotted = [label for label, needle in forbidden.items()
               if needle in body and needle not in searched]
    print(
        f"  {'FAIL' if present else 'ok  '} {asset} carries no engineering copy in a public view"
        + (f" — found: {', '.join(present)}" if present else "")
        + (f" (mentioned only in a comment, not as a rule: {', '.join(quotted)})" if quotted else "")
    )

# ----------------------------------------------------------------------- the images themselves ---
# A needle proves the mapping is deployed; it does not prove the bucket serves the prepared bytes.
# This is the one place the two can be compared directly: fetch the six objects the page loads and
# compare each response with the file in web/ by length and MD5, and with the content type the
# upload claimed. A 404, a stale copy or a mislabelled type would each show up here as itself.
IMAGES = ["bg-storm.webp",
          "chennai-nightscape.webp",
          "mumbai-nightscape.webp",
          "bengaluru-nightscape.webp",
          "hyderabad-nightscape.webp",
          "kochi-nightscape.webp",
          # M11: the photographs that came with the expanded fleet
          "delhi.webp",
          "kolkata.webp",
          "guwahati.webp",
          "patna.webp",
          "bhubaneshwar.webp",
          "visakhapatnam.webp",
          "pune.webp",
          "ahmedabad.webp",
          "jaipur.webp",
          "lucknow.webp",]
print("\n  the six images the page loads, against web/")
for name in IMAGES:
    local = (WEB / name).read_bytes()
    request = urllib.request.Request(f"{SITE}/{name}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except Exception as error:  # a missing object is a result, not a crash
        failed += 1
        print(f"  FAIL {name}: {error}")
        continue
    digest = hashlib.md5(body).hexdigest()
    problems = []
    if status != 200:
        problems.append(f"status {status}")
    if content_type != "image/webp":
        problems.append(f"content type {content_type!r}")
    if body != local or digest != hashlib.md5(local).hexdigest():
        problems.append(f"{len(body)} B / {digest} vs web/{name} {len(local)} B")
    failed += len(problems)
    detail = f"{len(body)} B, md5 {digest}, image/webp"
    print(f"  {'FAIL' if problems else 'ok  '} {name}: {detail}"
          + (f" — {', '.join(problems)}" if problems else " — byte-identical to web/"))


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
