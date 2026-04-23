# Copyright 2021 Camptocamp SA
# @author: Simone Orsi <simone.orsi@camptocamp.com>
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import logging
import time
from itertools import chain

import werkzeug

from odoo import http, models, registry as registry_get

from ..registry import EndpointRegistry

_logger = logging.getLogger(__name__)

# TTL in seconds for the endpoint route version cache.
# During this window, routing_map() skips opening a dedicated cursor
# to query the endpoint_route_version sequence, avoiding ~0.87s
# regeneration storms when the version hasn't actually changed.
_VERSION_CHECK_TTL = 30


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    # Safe defaults so that `routing_map()` never hits an AttributeError if
    # `_routing_map` happens to be set on the class without this module's
    # initialization branch having run first (e.g. registry rebuild during
    # deploy, MRO attribute visibility). The `if not hasattr(cls, "_routing_map")`
    # branch below still overwrites `_endpoint_route_last_version` with the
    # real DB value on the first call of a fresh worker, so this default of 0
    # is only ever observed in the pathological case — where a harmless extra
    # regeneration resynchronizes the state.
    _endpoint_route_last_version = 0
    _endpoint_route_last_check = 0.0

    @classmethod
    def _endpoint_route_registry(cls, cr):
        return EndpointRegistry.registry_for(cr)

    @classmethod
    def _generate_routing_rules(cls, modules, converters):
        # Override to inject custom endpoint rules.
        return chain(
            super()._generate_routing_rules(modules, converters),
            cls._endpoint_routing_rules(),
        )

    @classmethod
    def _endpoint_routing_rules(cls):
        """Yield custom endpoint rules"""
        e_registry = cls._endpoint_route_registry(http.request.env.cr)
        for endpoint_rule in e_registry.get_rules():
            _logger.debug("LOADING %s", endpoint_rule)
            endpoint = endpoint_rule.endpoint
            for url in endpoint_rule.routing["routes"]:
                yield (url, endpoint)

    @classmethod
    def _endpoint_route_version_needs_check(cls):
        """Return True if enough time has passed since the last version check."""
        last_check = getattr(cls, "_endpoint_route_last_check", 0.0)
        return (time.monotonic() - last_check) >= _VERSION_CHECK_TTL

    @classmethod
    def _reset_endpoint_route_version_check(cls):
        """Force the next routing_map() call to re-check the DB version.

        Called after rules are created/updated so that the TTL cache
        does not hide a version bump from the current process.
        """
        cls._endpoint_route_last_check = 0.0

    @classmethod
    def routing_map(cls, key=None):
        # When the request cursor is used to instantiate the EndpointRegistry
        # in the call to routing_map, the READ REPEATABLE isolation level
        # will ensure that any value read from the DB afterwards, will be the
        # same than when the first SELECT is executed.
        #
        # This is breaking the oauth flow as the oauth token that is written
        # at the beggining of the oauth process cannot be read by the cursor
        # computing the session token, which will read an old value. Therefore
        # when the session security check is performed, the session token
        # is outdated as the new session token is computed using an up to date
        # cursor.
        #
        # By using a dedicated cursor to instantiate the EndpointRegistry, we
        # ensure no read is performed on the database using the request cursor
        # which will in turn use the updated value of the oauth token to compute
        # the session token, and the security check will not fail.
        if cls._endpoint_route_version_needs_check():
            registry = registry_get(http.request.env.cr.dbname)
            cr = registry.cursor()
            try:
                last_version = cls._get_routing_map_last_version(cr)
                if not hasattr(cls, "_routing_map"):
                    _logger.debug(
                        "routing map just initialized, "
                        "store last update for this env"
                    )
                    cls._endpoint_route_last_version = last_version
                elif cls._endpoint_route_last_version < last_version:
                    _logger.info("Endpoint registry updated, reset routing map")
                    cls._routing_map = {}
                    cls._rewrite_len = {}
                    cls._endpoint_route_last_version = last_version
            finally:
                cr.rollback()
                cr.close()
            cls._endpoint_route_last_check = time.monotonic()

        return super().routing_map(key=key)

    @classmethod
    def _get_routing_map_last_version(cls, cr):
        return cls._endpoint_route_registry(cr).last_version()

    @classmethod
    def _clear_routing_map(cls):
        res = super()._clear_routing_map()
        if hasattr(cls, "_endpoint_route_last_version"):
            cls._endpoint_route_last_version = 0
        # Force version re-check on next request after cache clear
        cls._endpoint_route_last_check = 0.0
        return res

    @classmethod
    def _auth_method_user_endpoint(cls):
        """Special method for user auth which raises Unauthorized when needed.

        If you get an HTTP request (instead of a JSON one),
        the standard `user` method raises `SessionExpiredException`
        when there's no user session.
        This leads to a redirect to `/web/login`
        which is not desiderable for technical endpoints.

        This method makes sure that no matter the type of request we get,
        a proper exception is raised.
        """
        try:
            cls._auth_method_user()
        except http.SessionExpiredException as err:
            raise werkzeug.exceptions.Unauthorized() from err
