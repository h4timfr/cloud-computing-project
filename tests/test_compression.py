"""M17: a large JSON answer travels gzipped when the caller says it can take one.

Measured against the live API: GET /zones is 49,583 bytes of JSON and 5,554 bytes gzipped, and the
dashboard asks for it every five seconds. The stage cannot be configured to do this (the HTTP API
stage object has MinimumCompressionSize; the AWS CLI's apigatewayv2 update-stage rejects the
parameter), so the handler does it, and these tests are the reason that choice is safe: a caller that
does not ask for gzip receives exactly the bytes it received before, byte for byte.
"""

from __future__ import annotations

import base64
import gzip
import json
import unittest
from unittest import mock

import floodpulse.handler as handler_module
from floodpulse.handler import GZIP_FLOOR_BYTES, _maybe_compress, lambda_handler
from floodpulse.storage import LocalStore


def event_with(headers: dict[str, str] | None = None, path: str = "/zones") -> dict:
    return {
        "version": "2.0",
        "rawPath": path,
        "headers": headers or {},
        "requestContext": {"http": {"method": "GET"}},
    }


def response_with(body: str) -> dict:
    return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": body}


class CompressorTests(unittest.TestCase):
    """The rule itself, without a store, a route or a request context."""

    def setUp(self) -> None:
        self.large = json.dumps({"zones": [{"zone_id": f"zone-{index:04d}", "risk_score": 0.5}
                                          for index in range(400)]}, separators=(",", ":"))

    def test_the_floor_is_the_documented_one(self) -> None:
        self.assertEqual(GZIP_FLOOR_BYTES, 1024, "the documented compression floor")

    def test_a_caller_that_does_not_ask_gets_the_same_bytes(self) -> None:
        for headers in ({}, {"accept-encoding": "identity"}, {"accept-encoding": "br"},
                        {"accept-encoding": "gzip;q=0"}, {"accept-encoding": ""}):
            with self.subTest(headers=headers):
                response = response_with(self.large)
                self.assertEqual(_maybe_compress(response, event_with(headers)), response,
                                 "an unasked-for response must be untouched")

    def test_a_caller_that_asks_gets_gzip_that_round_trips(self) -> None:
        for header in ("gzip", "gzip, deflate, br", "GZIP", "deflate, gzip;q=0.8", "*"):
            with self.subTest(accept_encoding=header):
                compressed = _maybe_compress(response_with(self.large), event_with({"accept-encoding": header}))
                self.assertEqual(compressed["statusCode"], 200)
                self.assertEqual(compressed["headers"]["content-encoding"], "gzip")
                self.assertEqual(compressed["headers"]["content-type"], "application/json",
                                 "the existing headers must survive")
                self.assertTrue(compressed["isBase64Encoded"])
                decoded = gzip.decompress(base64.b64decode(compressed["body"])).decode("utf-8")
                self.assertEqual(decoded, self.large, "the decompressed body must be identical")
                self.assertLess(len(compressed["body"]), len(self.large) / 4,
                                "the compressed body must be substantially smaller")

    def test_a_small_body_is_left_alone_even_when_gzip_is_offered(self) -> None:
        small = json.dumps({"error": "not_found", "detail": "no such route"})
        self.assertLess(len(small), GZIP_FLOOR_BYTES)
        response = response_with(small)
        self.assertEqual(_maybe_compress(response, event_with({"accept-encoding": "gzip"})), response)

    def test_an_incompressible_body_is_left_alone(self) -> None:
        random_like = json.dumps({"blob": "".join(chr(33 + (index * 37) % 90) for index in range(4000))})
        with mock.patch.object(handler_module.gzip, "compress", lambda raw, level: b"x" * len(raw)):
            response = response_with(random_like)
            self.assertEqual(_maybe_compress(response, event_with({"accept-encoding": "gzip"})), response,
                             "a body that does not shrink must not gain a wrapper")

    def test_an_event_without_headers_is_a_no_op(self) -> None:
        response = response_with(self.large)
        self.assertEqual(_maybe_compress(response, None), response)
        self.assertEqual(_maybe_compress(response, {}), response)


class HandlerCompressionTests(unittest.TestCase):
    """End to end through the Lambda entry point, on a real route and a real (empty) store.

    ``time.time`` is frozen because the board carries a ``generated_at``: two calls a millisecond
    apart are two different documents, and the point of these tests is that compression changes the
    *encoding* of one document and nothing else about it.
    """

    FROZEN_NOW = 1792400000.0

    def setUp(self) -> None:
        self.original_store = handler_module.get_store()
        handler_module.set_store(LocalStore())
        self.clock = mock.patch.object(handler_module.time, "time", lambda: self.FROZEN_NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def tearDown(self) -> None:
        handler_module.set_store(self.original_store)

    def test_a_caller_that_does_not_ask_for_gzip_is_served_exactly_as_before(self) -> None:
        plain = lambda_handler(event_with(), None)
        identity = lambda_handler(event_with({"accept-encoding": "identity"}), None)
        self.assertNotIn("content-encoding", plain["headers"])
        self.assertNotIn("isBase64Encoded", plain)
        self.assertEqual(identity, plain, "an unasked-for board must be byte-identical")

    def test_the_live_style_board_is_gzipped_when_asked_and_decompresses_identically(self) -> None:
        plain = lambda_handler(event_with(), None)
        asked = lambda_handler(event_with({"accept-encoding": "gzip, deflate, br"}), None)
        self.assertGreaterEqual(len(plain["body"]), GZIP_FLOOR_BYTES,
                                "the board is the large end of the range, which is why this exists")
        self.assertEqual(asked["headers"]["content-encoding"], "gzip")
        self.assertTrue(asked["isBase64Encoded"])
        self.assertEqual(asked["headers"]["content-type"], plain["headers"]["content-type"])
        self.assertEqual(gzip.decompress(base64.b64decode(asked["body"])).decode("utf-8"), plain["body"],
                         "the compressed board must decompress to exactly the uncompressed board")
        self.assertLess(len(asked["body"]), len(plain["body"]) / 2,
                        "and it must actually be smaller on the wire, base64 overhead included")


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()
