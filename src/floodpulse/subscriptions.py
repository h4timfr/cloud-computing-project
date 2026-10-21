"""Public email alert subscriptions (M10, part B).

A visitor can ask to be emailed when a city reaches WARNING or CRITICAL. The
subscription is owned by SNS from end to end:

* FloodPulse calls ``sns.subscribe(TopicArn=<env>, Protocol="email", Endpoint=<address>,
  ReturnSubscriptionArn=True)``.
* **AWS sends the confirmation message.** FloodPulse never composes, sends or fakes one,
  and never claims an address is subscribed: until the visitor clicks *Confirm
  subscription* in AWS's email, the subscription is pending and no alert reaches them.
* The topic ARN is read from the environment only (``FLOODPULSE_SNS_TOPIC_ARN``). A client
  cannot name the topic, the protocol or the region, and none of the three is echoed back.

Privacy: the address is *not* stored. The only thing FloodPulse keeps is a throttling
counter whose key is a SHA-256 digest of the normalised address, with a TTL, plus - since
M17 - a second counter keyed by a SHA-256 digest of the caller's source address, so that
one caller cannot spend a stranger's hourly budget. Both counters hold a digest, a window
bucket and a number; neither can be read back as an address. SNS holds the endpoint; there
is no marketing list, no analytics and no export.

This module is pure: no AWS calls, no clock reads. ``parse_subscription_request`` decides
what is acceptable, and the handler decides what to do about it.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .config import (
    SUBSCRIPTION_BODY_MAX_BYTES,
    SUBSCRIPTION_MAX_EMAIL_LENGTH,
    SUBSCRIPTION_MAX_LABEL_LENGTH,
    SUBSCRIPTION_MAX_LOCAL_LENGTH,
    SUBSCRIPTION_RATE_PREFIX,
    SUBSCRIPTION_RATE_WINDOW_PREFIX,
    SUBSCRIPTION_SOURCE_RATE_PREFIX,
)
from .validation import ValidationError

#: The single field this route accepts.
ALLOWED_FIELDS = frozenset({"email"})

#: Control characters (including CR and LF), which are rejected anywhere in the address.
#: A carriage return inside an address is how header injection starts, and SNS is a mail
#: system at the other end of this call.
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: Characters that mean "this is not one plain address": a list separator, a display name,
#: a comment, an angle-bracket route, a quoted local part or a backslash escape.
ADDRESS_TRICKS = re.compile(r"""[\s,;:<>()\[\]"\\]""")

#: One dot-separated domain label.
DOMAIN_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

#: What the local part may contain for this MVP. Deliberately narrower than RFC 5321: the
#: dot-atom form covers ordinary addresses, and everything exotic (quoted local parts,
#: comments, IP-literal domains) is refused rather than guessed at.
LOCAL_PART = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]+$")


def normalise_email(value: Any) -> str:
    """Validate one email address and return it with surrounding whitespace removed.

    Raises :class:`ValidationError` with ``field="email"`` for anything that is not a
    single, plausible, plain email address. The returned value keeps the local part's
    case (some mail systems treat it as significant) while every rejection decision is
    made on the lowercased form.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValidationError("must be a string", "email")

    address = value.strip()
    if not address:
        raise ValidationError("must not be empty", "email")
    if len(address) > SUBSCRIPTION_MAX_EMAIL_LENGTH:
        raise ValidationError(
            f"must be at most {SUBSCRIPTION_MAX_EMAIL_LENGTH} characters (got {len(address)})",
            "email",
        )
    if CONTROL_CHARS.search(address):
        raise ValidationError("must not contain control characters", "email")
    if ADDRESS_TRICKS.search(address):
        # spaces inside the address, a comma-separated list, a display name, a comment,
        # a quoted local part: all of them mean the caller did not send one plain address
        raise ValidationError("must be a single email address with no spaces or separators", "email")
    if not address.isascii():
        raise ValidationError("must be an ASCII email address", "email")
    if address.count("@") != 1:
        raise ValidationError("must contain exactly one '@'", "email")

    local, _, domain = address.partition("@")
    lowered_local = local.lower()
    lowered_domain = domain.lower()

    if not local or not domain:
        raise ValidationError("must have a name and a domain", "email")
    if len(local) > SUBSCRIPTION_MAX_LOCAL_LENGTH:
        raise ValidationError(
            f"the part before '@' must be at most {SUBSCRIPTION_MAX_LOCAL_LENGTH} characters",
            "email",
        )
    if not LOCAL_PART.match(lowered_local):
        raise ValidationError("the part before '@' contains characters this form does not accept", "email")
    if lowered_local.startswith(".") or lowered_local.endswith(".") or ".." in lowered_local:
        raise ValidationError("the part before '@' cannot start, end or repeat a dot", "email")

    labels = lowered_domain.split(".")
    if len(labels) < 2:
        raise ValidationError("the domain must contain a dot", "email")
    if any(not label for label in labels):
        raise ValidationError("the domain cannot contain an empty label", "email")
    for label in labels:
        if len(label) > SUBSCRIPTION_MAX_LABEL_LENGTH:
            raise ValidationError(
                f"a domain label must be at most {SUBSCRIPTION_MAX_LABEL_LENGTH} characters", "email"
            )
        if not DOMAIN_LABEL.match(label):
            raise ValidationError("the domain contains an invalid label", "email")
    # The last label must be alphabetic (".com", ".in", ".uk"): this refuses "1.2.3.4"-style
    # input and a bare trailing "-" without needing a TLD list to go stale.
    if not labels[-1].isalpha() or len(labels[-1]) < 2:
        raise ValidationError("the domain must end in a two-letter or longer name", "email")

    # The domain's case carries no meaning (RFC 5321 s2.4: domains are case-insensitive),
    # so it is folded here - the string sent to SNS, the string used for the counter and
    # the string this module returns all agree, and one mailbox gets one SNS endpoint
    # instead of one per spelling. The local part keeps its case: some mail systems treat
    # it as significant, so discarding it would be a guess.
    return f"{local}@{lowered_domain}"


def parse_subscription_request(raw_body: str | None) -> str:
    """Validate the request body and return the address to subscribe.

    The body must be a JSON **object** with exactly one field, ``email``. Arrays, scalars,
    empty bodies, oversized bodies and unknown fields are all refused before anything is
    sent to SNS.
    """
    if raw_body is None:
        raise ValidationError("request body is required", "body")
    if not isinstance(raw_body, str):
        # Same contract as the ingest route: the body is a JSON string, not a decoded object.
        raise ValidationError("request body must be a JSON string", "body")
    text = raw_body
    if not text.strip():
        raise ValidationError("request body is required", "body")
    # errors="replace": a lone surrogate is a bad payload, not a 500 (see the ingest route's
    # own size check for the same reasoning).
    size = len(text.encode("utf-8", "replace"))
    if size > SUBSCRIPTION_BODY_MAX_BYTES:
        raise ValidationError(
            f"request body is {size} bytes; limit is {SUBSCRIPTION_BODY_MAX_BYTES}", "body"
        )

    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValidationError("body is not valid JSON", "body") from exc

    if not isinstance(payload, Mapping):
        raise ValidationError("body must be a JSON object", "body")
    extra = sorted(set(payload) - ALLOWED_FIELDS)
    if extra:
        raise ValidationError(f"unexpected field(s): {', '.join(extra)}", "body")
    if "email" not in payload:
        raise ValidationError("is required", "email")

    return normalise_email(payload["email"])


def email_identity(address: str) -> str:
    """The throttling identity of an address: lowercase, whitespace already removed.

    Used for the rate-limit key only. It is never returned to a client and never stored
    in plaintext.
    """
    return str(address).strip().lower()


def email_digest(address: str) -> str:
    """SHA-256 hex digest of the throttling identity, for the rate-limit key.

    The address itself is never part of a stored key, and the digest is never returned to
    a client: it exists so two attempts by the same address land on the same counter
    without DynamoDB ever holding the address.
    """
    return hashlib.sha256(email_identity(address).encode("utf-8")).hexdigest()


def rate_limit_key(address: str) -> str:
    """The partition key of the counter for this address (``SUBRATE#<digest>``)."""
    return f"{SUBSCRIPTION_RATE_PREFIX}{email_digest(address)}"


def source_identity(source_ip: Any) -> str:
    """The throttling identity of a caller: the source address as reported by API Gateway.

    Only used to key a counter, never returned and never stored in plaintext. Takes the
    first value when a forwarded list arrives (API Gateway reports a single ``sourceIp``,
    but a test or a local run may hand over anything).
    """
    text = str(source_ip or "").split(",")[0].strip().lower()
    return text


def source_digest(source_ip: Any) -> str:
    """SHA-256 hex digest of the caller identity, for the per-connection counter."""
    return hashlib.sha256(source_identity(source_ip).encode("utf-8")).hexdigest()


def source_rate_limit_key(source_ip: Any) -> str:
    """The partition key of the counter for this caller (``SUBSRC#<digest>``)."""
    return f"{SUBSCRIPTION_SOURCE_RATE_PREFIX}{source_digest(source_ip)}"


def redact_source(source_ip: Any) -> str:
    """A log-safe stand-in for a caller address: the digest, never the address."""
    try:
        return f"sha256:{source_digest(source_ip)[:12]}"
    except Exception:  # noqa: BLE001 - logging must never raise
        return "sha256:unavailable"


def window_sort_key(now_epoch: float, window_seconds: int) -> str:
    """The sort key of the counter for the window containing ``now_epoch``.

    Fixed windows are coarse, but this endpoint is not a metered API: a visitor who is
    allowed three confirmation requests an hour cannot tell which hour boundary they fell
    on, and the alternative (a sliding log) would mean storing one item per attempt.
    """
    bucket = int(now_epoch) // int(window_seconds) * int(window_seconds)
    return f"{SUBSCRIPTION_RATE_WINDOW_PREFIX}{bucket}"


def window_expiry(now_epoch: float, window_seconds: int, ttl_seconds: int) -> int:
    """The TTL for the counter item, in epoch seconds."""
    return int(now_epoch) + int(ttl_seconds)


def redact_email(address: Any) -> str:
    """A log-safe stand-in for an address: the digest, never the address.

    Used in log lines so an operator can correlate the three throttled requests for one
    visitor without the log holding anybody's address.
    """
    try:
        return f"sha256:{email_digest(str(address))[:12]}"
    except Exception:  # noqa: BLE001 - logging must never raise
        return "sha256:unavailable"
