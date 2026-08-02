import math
import unittest

from aiohttp import web

from sophos.api.routes.permissions import (
    _parse_float,
    _parse_int,
    _validate_scope,
    routes,
)


class PermissionApiValidationTests(unittest.TestCase):
    def test_valid_scopes(self) -> None:
        _validate_scope("global", 0)
        _validate_scope("conversation", 123)

    def test_invalid_scope_combinations_are_rejected(self) -> None:
        for scope_type, scope_id in (
            ("global", 1),
            ("conversation", 0),
            ("group", 1),
            ("private", 1),
            ("other", 1),
        ):
            with self.subTest(scope_type=scope_type, scope_id=scope_id), self.assertRaises(web.HTTPBadRequest):
                _validate_scope(scope_type, scope_id)

    def test_numeric_parsers_reject_invalid_values(self) -> None:
        self.assertEqual(_parse_int("123", "id"), 123)
        self.assertEqual(_parse_float("1.5", "rate"), 1.5)
        with self.assertRaises(web.HTTPBadRequest):
            _parse_int("abc", "id")
        with self.assertRaises(web.HTTPBadRequest):
            _parse_float("abc", "rate")
        self.assertTrue(math.isnan(_parse_float("nan", "rate")))

    def test_expected_routes_are_registered(self) -> None:
        registered = {(route.method, route.path) for route in routes}
        self.assertIn(("GET", "/api/permissions/users"), registered)
        self.assertIn(("GET", "/api/permissions/conversations"), registered)
        self.assertIn(("GET", "/api/permissions/scopes"), registered)
        self.assertIn(("PUT", "/api/permissions/scopes/{scope_type}/{scope_id}/tools"), registered)
        self.assertIn(("POST", "/api/permissions/grants"), registered)
        self.assertIn(("POST", "/api/permissions/policies"), registered)


if __name__ == "__main__":
    unittest.main()
