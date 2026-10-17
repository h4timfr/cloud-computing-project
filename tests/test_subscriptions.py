"""Tests for the public email subscription route (M10, part B).

Everything here runs in-process: no AWS call, no network, no boto3. The SNS client is a
recording fake, so the tests can assert the *exact* call the code makes - topic, protocol,
endpoint - and that no request can change any of them.

The privacy tests are the point of the file: the address must not reach DynamoDB, and it
must not reach the log.
"""

from __future__ import annotations

import hashlib
import json
import logging
import unittest

from floodpulse.config import (
    SUBSCRIPTION_BODY_MAX_BYTES,
    SUBSCRIPTION_MAX_EMAIL_LENGTH,
    SUBSCRIPTION_RATE_LIMIT,
    SUBSCRIPTION_RATE_PREFIX,
    SUBSCRIPTION_RATE_TTL_SECONDS,
    SUBSCRIPTION_WINDOW_SECONDS,
)
from floodpulse.handler import dispatch
from floodpulse.notify import (
    NoopNotifier,
    SnsNotifier,
    build_notifier,
    notifier_for_env,
    subscription_state_for_arn,
)
from floodpulse.storage import LocalStore
from floodpulse.subscriptions import (
    email_digest,
    email_identity,
    normalise_email,
    parse_subscription_request,
    rate_limit_key,
    redact_email,
    window_expiry,
    window_sort_key,
)
from floodpulse.validation import ValidationError

TOPIC_ARN = "arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts"
VALID = "visitor@example.com"
NOW = 1_771_064_100.0  # fixed clock

#: Inputs that must be refused before anything reaches SNS.
INVALID_ADDRESSES: tuple[tuple[str, str], ...] = (
    ("empty", ""),
    ("whitespace only", "   "),
    ("no at sign", "visitor.example.com"),
    ("two at signs", "a@b@example.com"),
    ("no local part", "@example.com"),
    ("no domain", "visitor@"),
    ("domain without a dot", "visitor@localhost"),
    ("single letter tld", "visitor@example.c"),
    ("numeric tld", "visitor@example.1"),
    ("ip literal domain", "visitor@[192.168.0.1]"),
    ("carriage return", "visitor@example.com\r\nBcc: someone@example.com"),
    ("newline", "visitor@exam\nple.com"),
    ("tab", "visitor\t@example.com"),
    ("null byte", "visitor@example.com\x00"),
    ("space inside", "visitor @example.com"),
    ("comma list", "a@example.com,b@example.com"),
    ("semicolon list", "a@example.com;b@example.com"),
    ("display name", "Visitor <visitor@example.com>"),
    ("angle brackets", "<visitor@example.com>"),
    ("comment", "visitor(comment)@example.com"),
    ("quoted local part", '"visitor name"@example.com'),
    ("backslash escape", "visitor\\@example.com"),
    ("leading dot local", ".visitor@example.com"),
    ("trailing dot local", "visitor.@example.com"),
    ("double dot local", "vis..itor@example.com"),
    ("leading hyphen domain", "visitor@-example.com"),
    ("trailing hyphen domain", "visitor@example-.com"),
    ("empty domain label", "visitor@example..com"),
    ("non-ascii domain", "visitor@xample.com".replace("x", "\u00e9")),
    ("non-ascii local", "\u00e9visitor@example.com"),
    ("short tld", "visitor@example.c"),
    ("long local part", "a" * 65 + "@example.com"),
    ("long label", "visitor@" + "a" * 64 + ".com"),
    ("too long overall", "a" * 60 + "@" + ("b" * 60 + ".") * 4 + "com"),
)


class NormaliseEmailTests(unittest.TestCase):
    def test_ordinary_addresses_are_accepted_and_trimmed(self) -> None:
        for address in (
            "visitor@example.com",
            "  visitor@example.com  ",
            "first.last+tag@sub.example.co.in",
            "a_b-c@example.co.uk",
            "x@y.io",
        ):
            with self.subTest(address=address):
                self.assertEqual(normalise_email(address), address.strip())

    def test_the_local_part_keeps_its_case(self) -> None:
        # Some mail systems treat the local part as case-sensitive, so what is sent to SNS
        # is what the visitor typed (whitespace removed). Lowercasing is for the digest only.
        self.assertEqual(normalise_email("  Visitor.Name@Example.COM "), "Visitor.Name@Example.COM")
        self.assertEqual(email_identity("Visitor.Name@Example.COM"), "visitor.name@example.com")

    def test_rejected_addresses_name_the_field(self) -> None:
        for label, address in INVALID_ADDRESSES:
            with self.subTest(case=label):
                with self.assertRaises(ValidationError) as raised:
                    normalise_email(address)
                self.assertEqual(raised.exception.field, "email")

    def test_a_non_string_is_refused(self) -> None:
        for value in (None, 42, True, ["visitor@example.com"], {"email": "visitor@example.com"}):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as raised:
                    normalise_email(value)
                self.assertEqual(raised.exception.field, "email")

    def test_the_length_limit_is_exact(self) -> None:
        local = "a" * 64
        tail = "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 55  # keeps the total under 254
        candidate = local + tail
        self.assertLessEqual(len(candidate), SUBSCRIPTION_MAX_EMAIL_LENGTH)
        self.assertEqual(normalise_email(candidate), candidate)
        with self.assertRaises(ValidationError):
            normalise_email("a" * 64 + "@" + "b" * 190 + ".com")


class RequestParsingTests(unittest.TestCase):
    def test_one_object_with_one_field(self) -> None:
        self.assertEqual(parse_subscription_request('{"email":"' + VALID + '"}'), VALID)

    def test_bodies_that_are_not_one_small_object(self) -> None:
        cases = {
            "missing body": None,
            "blank body": "   ",
            "empty string": "",
            "malformed json": '{"email": "visitor@example.com"',
            "json array": '["visitor@example.com"]',
            "json string": '"visitor@example.com"',
            "json number": "42",
            "json null": "null",
            "no email field": "{}",
            "null email": '{"email": null}',
            "extra field": '{"email": "visitor@example.com", "topic_arn": "' + TOPIC_ARN + '"}',
            "protocol field": '{"email": "visitor@example.com", "protocol": "sms"}',
            "region field": '{"email": "visitor@example.com", "region": "us-east-1"}',
            "oversized": json.dumps({"email": VALID, "pad": "x" * SUBSCRIPTION_BODY_MAX_BYTES}),
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValidationError):
                    parse_subscription_request(body)

    def test_a_client_cannot_name_a_topic_protocol_or_region(self) -> None:
        # The route accepts exactly one field, so every attempt to redirect the subscription
        # is refused as an unexpected field rather than silently ignored.
        for field in ("topic_arn", "TopicArn", "protocol", "region", "subscription_arn", "key", "x-fp-key"):
            with self.subTest(field=field):
                body = json.dumps({"email": VALID, field: "anything"})
                with self.assertRaises(ValidationError) as raised:
                    parse_subscription_request(body)
                self.assertEqual(raised.exception.field, "body")


class KeyTests(unittest.TestCase):
    def test_the_throttle_key_is_a_digest_not_an_address(self) -> None:
        key = rate_limit_key("Visitor@Example.com")
        self.assertTrue(key.startswith(SUBSCRIPTION_RATE_PREFIX))
        digest = key[len(SUBSCRIPTION_RATE_PREFIX) :]
        self.assertEqual(digest, hashlib.sha256(b"visitor@example.com").hexdigest())
        self.assertEqual(len(digest), 64)
        self.assertNotIn("visitor", key)
        self.assertNotIn("example", key)
        # case and surrounding whitespace must not create a second identity
        self.assertEqual(key, rate_limit_key("  visitor@EXAMPLE.com "))

    def test_the_window_key_is_a_bucket_start(self) -> None:
        window = SUBSCRIPTION_WINDOW_SECONDS
        bucket = int(NOW) // window * window
        expected = f"WINDOW#{bucket}"
        self.assertEqual(expected, "WINDOW#1771063200")
        self.assertEqual(window_sort_key(NOW, window), expected)
        # every instant inside the same hour lands on the same counter
        self.assertEqual(window_sort_key(bucket, window), expected)
        self.assertEqual(window_sort_key(bucket + window - 1, window), expected)
        # and the next hour starts a new one
        self.assertEqual(window_sort_key(bucket + window, window), f"WINDOW#{bucket + window}")

    def test_the_ttl_outlives_the_window(self) -> None:
        ttl = window_expiry(NOW, SUBSCRIPTION_WINDOW_SECONDS, SUBSCRIPTION_RATE_TTL_SECONDS)
        self.assertEqual(ttl, int(NOW) + SUBSCRIPTION_WINDOW_SECONDS + 3600)
        self.assertGreater(ttl, int(NOW) + SUBSCRIPTION_WINDOW_SECONDS)

    def test_the_log_stand_in_is_a_digest(self) -> None:
        marker = redact_email(VALID)
        self.assertTrue(marker.startswith("sha256:"))
        self.assertNotIn("visitor", marker)
        self.assertNotIn("example", marker)


class RecordingSnsClient:
    """Records the exact SNS calls the notifier makes."""

    def __init__(self, subscription_arn: str = "pending confirmation", fail: str | None = None) -> None:
        self.subscription_arn = subscription_arn
        self.fail = fail
        self.subscribe_calls: list[dict[str, object]] = []
        self.publish_calls: list[dict[str, object]] = []

    def subscribe(self, **kwargs):
        self.subscribe_calls.append(kwargs)
        if self.fail:
            raise RuntimeError(self.fail)
        return {"SubscriptionArn": self.subscription_arn}

    def publish(self, **kwargs):
        self.publish_calls.append(kwargs)
        return {"MessageId": "msg-1"}


class SnsCallShapeTests(unittest.TestCase):
    """The SNS call is fixed by code, not by input."""

    def test_subscribe_uses_the_configured_topic_email_protocol_and_validated_endpoint(self) -> None:
        client = RecordingSnsClient()
        notifier = SnsNotifier(TOPIC_ARN, client=client)
        outcome = notifier.subscribe_email(VALID)
        self.assertTrue(outcome["requested"])
        (call,) = client.subscribe_calls
        self.assertEqual(
            set(call),
            {"TopicArn", "Protocol", "Endpoint", "ReturnSubscriptionArn"},
        )
        self.assertEqual(call["TopicArn"], TOPIC_ARN)
        self.assertEqual(call["Protocol"], "email")
        self.assertEqual(call["Endpoint"], VALID)
        self.assertIs(call["ReturnSubscriptionArn"], True)

    def test_the_arn_from_sns_is_not_returned_to_the_caller(self) -> None:
        notifier = SnsNotifier(TOPIC_ARN, client=RecordingSnsClient(subscription_arn=TOPIC_ARN + ":abc"))
        outcome = notifier.subscribe_email(VALID)
        # The outcome says what SNS told us and nothing more. "sns_accepted" is not "confirmed":
        # a brand-new endpoint gets an ARN too when ReturnSubscriptionArn is asked for.
        self.assertEqual(outcome, {"requested": True, "state": "sns_accepted"})
        self.assertNotIn("arn", json.dumps(outcome))

    def test_pending_and_confirmed_are_classified(self) -> None:
        self.assertEqual(subscription_state_for_arn("pending confirmation"), "pending_confirmation")
        # An ARN is not proof of a confirmed endpoint: with ReturnSubscriptionArn=True a new
        # subscription returns one too, so the state says only what SNS actually told us.
        self.assertEqual(subscription_state_for_arn(TOPIC_ARN + ":abc"), "sns_accepted")
        self.assertNotEqual(subscription_state_for_arn(TOPIC_ARN + ":abc"), "confirmed")
        self.assertEqual(subscription_state_for_arn(""), "pending_confirmation")
        self.assertEqual(subscription_state_for_arn(None), "pending_confirmation")

    def test_a_failing_subscribe_is_reported_with_the_arn_redacted(self) -> None:
        failing = RecordingSnsClient(fail=f"AccessDenied for {TOPIC_ARN}")
        outcome = SnsNotifier(TOPIC_ARN, client=failing).subscribe_email(VALID)
        self.assertFalse(outcome["requested"])
        self.assertEqual(outcome["reason"], "subscribe_failed")
        self.assertNotIn(TOPIC_ARN, json.dumps(outcome))
        self.assertIn("<alert-topic>", outcome["detail"])

    def test_the_no_op_notifier_never_subscribes(self) -> None:
        self.assertEqual(
            NoopNotifier().subscribe_email(VALID),
            {"requested": False, "reason": "not_configured"},
        )

    def test_the_notifier_comes_from_the_environment_only(self) -> None:
        self.assertIsInstance(notifier_for_env({}), NoopNotifier)
        notifier = notifier_for_env({"FLOODPULSE_SNS_TOPIC_ARN": TOPIC_ARN})
        self.assertIsInstance(notifier, SnsNotifier)
        self.assertTrue(notifier.enabled)
        self.assertIsInstance(build_notifier(""), NoopNotifier)


class SubscriptionRouteTests(unittest.TestCase):
    """``POST /subscriptions`` end to end through ``dispatch``."""

    def setUp(self) -> None:
        self.store = LocalStore()
        self.client = RecordingSnsClient()
        self.notifier = SnsNotifier(TOPIC_ARN, client=self.client)

    def post(self, body=None, method: str = "POST", path: str = "/subscriptions", notifier=None):
        return dispatch(
            method,
            path,
            body,
            self.store,
            NOW,
            {},
            {},
            self.notifier if notifier is None else notifier,
        )

    def test_an_accepted_request_answers_202_with_the_confirmation_instruction(self) -> None:
        status, payload = self.post(json.dumps({"email": VALID}))
        self.assertEqual(status, 202)
        self.assertEqual(
            payload,
            {
                "status": "confirmation_required",
                "message": "Check your email to confirm the subscription.",
            },
        )
        # no claim of delivery, no address, no ARN
        text = json.dumps(payload).lower()
        for forbidden in ("subscribed", "sent", "arn", VALID, "topic"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(len(self.client.subscribe_calls), 1)

    def test_the_response_is_identical_for_new_pending_and_confirmed_addresses(self) -> None:
        first = self.post(json.dumps({"email": VALID}))
        self.client.subscription_arn = TOPIC_ARN + ":abc"
        second = self.post(json.dumps({"email": "another@example.com"}))
        self.assertEqual(first, second)

    def test_repeated_requests_are_throttled_and_the_address_is_never_answered(self) -> None:
        bodies = []
        for _ in range(SUBSCRIPTION_RATE_LIMIT):
            status, payload = self.post(json.dumps({"email": VALID}))
            self.assertEqual(status, 202)
            bodies.append(payload)
        self.assertTrue(all(body == bodies[0] for body in bodies))
        self.assertEqual(len(self.client.subscribe_calls), SUBSCRIPTION_RATE_LIMIT)

        status, payload = self.post(json.dumps({"email": VALID}))
        self.assertEqual(status, 429)
        self.assertEqual(payload["status"], "rate_limited")
        self.assertEqual(payload["message"], "Too many subscription requests. Please try again later.")
        self.assertNotIn(VALID, json.dumps(payload))
        # the refused attempt must not reach SNS
        self.assertEqual(len(self.client.subscribe_calls), SUBSCRIPTION_RATE_LIMIT)

    def test_the_throttle_identity_ignores_case_and_whitespace(self) -> None:
        for address in (VALID, VALID.upper(), f"  {VALID}  "):
            self.post(json.dumps({"email": address}))
        self.assertEqual(len(self.client.subscribe_calls), SUBSCRIPTION_RATE_LIMIT)
        self.assertEqual(self.post(json.dumps({"email": VALID}))[0], 429)

    def test_a_different_address_is_not_throttled_by_another(self) -> None:
        for _ in range(SUBSCRIPTION_RATE_LIMIT):
            self.post(json.dumps({"email": VALID}))
        status, _ = self.post(json.dumps({"email": "someone-else@example.com"}))
        self.assertEqual(status, 202)

    def test_invalid_input_is_a_friendly_400_that_does_not_echo_the_input(self) -> None:
        for label, body in (
            ("no at sign", json.dumps({"email": "visitor.example.com"})),
            ("crlf", json.dumps({"email": "a@example.com\r\nBcc: b@example.com"})),
            ("array", json.dumps(["visitor@example.com"])),
            ("extra field", json.dumps({"email": VALID, "topic_arn": TOPIC_ARN})),
            ("malformed", "{not json"),
            ("empty body", ""),
        ):
            with self.subTest(case=label):
                status, payload = self.post(body)
                self.assertEqual(status, 400)
                self.assertIn(payload["error"], ("invalid_email", "invalid_body"))
                self.assertIn(payload["message"][-1], ".")
                text = json.dumps(payload)
                self.assertNotIn("Traceback", text)
                self.assertNotIn(TOPIC_ARN, text)
                self.assertNotIn("arn:aws", text)
                for word in ("Exception", "AccessDenied", "botocore"):
                    self.assertNotIn(word, text)
        # nothing invalid ever reached SNS
        self.assertEqual(self.client.subscribe_calls, [])

    def test_an_invalid_attempt_does_not_consume_the_throttle_budget(self) -> None:
        for _ in range(10):
            self.post(json.dumps({"email": "not an address"}))
        self.assertEqual(self.post(json.dumps({"email": VALID}))[0], 202)

    def test_a_failing_sns_call_is_a_friendly_503(self) -> None:
        failing = SnsNotifier(TOPIC_ARN, client=RecordingSnsClient(fail=f"AccessDenied {TOPIC_ARN}"))
        status, payload = self.post(json.dumps({"email": VALID}), notifier=failing)
        self.assertEqual(status, 503)
        self.assertEqual(payload["message"], "We couldn't create the subscription request right now. Please try again.")
        text = json.dumps(payload)
        self.assertNotIn(TOPIC_ARN, text)
        self.assertNotIn("AccessDenied", text)

    def test_the_failed_attempt_still_counts_against_the_budget(self) -> None:
        failing = SnsNotifier(TOPIC_ARN, client=RecordingSnsClient(fail="boom"))
        for _ in range(SUBSCRIPTION_RATE_LIMIT):
            self.assertEqual(self.post(json.dumps({"email": VALID}), notifier=failing)[0], 503)
        self.assertEqual(self.post(json.dumps({"email": VALID}), notifier=failing)[0], 429)

    def test_a_missing_topic_configuration_is_reported_as_unavailable(self) -> None:
        status, payload = self.post(json.dumps({"email": VALID}), notifier=NoopNotifier())
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "subscription_unavailable")
        self.assertIn("try again", payload["message"])
        self.assertEqual(self.client.subscribe_calls, [])

    def test_only_post_is_allowed(self) -> None:
        status, payload = self.post(None, method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(payload["allowed"], ["POST"])

    def test_the_route_is_public_and_ignores_the_ingest_key(self) -> None:
        # no x-fp-key header at all: this route is for anonymous visitors
        self.assertEqual(self.post(json.dumps({"email": VALID}), notifier=self.notifier)[0], 202)

    def test_an_oversized_body_is_refused_before_sns(self) -> None:
        status, _ = self.post(json.dumps({"email": VALID, "pad": "x" * SUBSCRIPTION_BODY_MAX_BYTES}))
        self.assertEqual(status, 400)
        self.assertEqual(self.client.subscribe_calls, [])


class PrivacyTests(unittest.TestCase):
    """The address must not be stored and must not be logged."""

    def setUp(self) -> None:
        self.store = LocalStore()
        self.client = RecordingSnsClient()
        self.notifier = SnsNotifier(TOPIC_ARN, client=self.client)

    def test_no_stored_item_contains_the_address(self) -> None:
        for _ in range(SUBSCRIPTION_RATE_LIMIT):
            dispatch(
                "POST",
                "/subscriptions",
                json.dumps({"email": VALID}),
                self.store,
                NOW,
                {},
                {},
                self.notifier,
            )
        items = list(self.store.items())
        self.assertEqual(len(items), 1)
        dumped = json.dumps(items, default=str).lower()
        for fragment in (VALID, "visitor", "example.com", "subrate", "window"):
            if fragment in ("subrate", "window"):
                continue
            self.assertNotIn(fragment, dumped)
        (item,) = items
        self.assertTrue(str(item["pk"]).startswith(SUBSCRIPTION_RATE_PREFIX))
        self.assertEqual(item["attempts"], SUBSCRIPTION_RATE_LIMIT)
        self.assertEqual(item["ttl"], int(NOW) + SUBSCRIPTION_RATE_TTL_SECONDS)
        self.assertEqual(item["pk"], rate_limit_key(VALID))

    def test_nothing_is_logged_but_the_digest(self) -> None:
        with self.assertLogs("floodpulse.handler", level=logging.DEBUG) as captured:
            for _ in range(SUBSCRIPTION_RATE_LIMIT + 1):
                dispatch(
                    "POST",
                    "/subscriptions",
                    json.dumps({"email": VALID}),
                    self.store,
                    NOW,
                    {},
                    {},
                    self.notifier,
                )
            failing = SnsNotifier(TOPIC_ARN, client=RecordingSnsClient(fail=f"AccessDenied {TOPIC_ARN}"))
            dispatch(
                "POST",
                "/subscriptions",
                json.dumps({"email": "other@example.com"}),
                self.store,
                NOW,
                {},
                {},
                failing,
            )
        joined = "\n".join(captured.output)
        self.assertNotIn(VALID, joined)
        self.assertNotIn("other@example.com", joined)
        self.assertNotIn(TOPIC_ARN, joined)
        self.assertIn(redact_email(VALID), joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
