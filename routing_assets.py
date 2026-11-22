"""Encode literal routing lists as Xray GeoSiteList and GeoIPList assets."""

import hashlib
import ipaddress
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from logger import logger


_CODE = b"MARZBAN"
_DOMAIN_TYPES = {"keyword": 0, "domain": 2, "full": 3}


def _varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _bytes_field(number, value):
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _int_field(number, value):
    return _varint(number << 3) + _varint(value)


def _site_asset(values):
    entries = []
    for value in values:
        prefix, separator, pattern = value.partition(":")
        if not separator:
            prefix, pattern = "keyword", value
        if prefix not in _DOMAIN_TYPES or not pattern:
            return None
        # Unlike inline rules, invalid regexes in geosite entries are skipped
        # by Xray. Leave regex and other special syntax in the original list.
        entries.append(_bytes_field(2, _int_field(1, _DOMAIN_TYPES[prefix])
                                    + _bytes_field(2, pattern.encode("utf-8"))))
    return _bytes_field(1, _bytes_field(1, _CODE) + b"".join(entries))


def _ip_asset(values):
    entries = []
    for value in values:
        # Leave negation, existing assets, IPv6 scopes, and nonstandard
        # prefixes to Xray's own parser.
        if any(marker in value for marker in ("!", "%")):
            return None
        address, separator, prefix = value.partition("/")
        if separator and (not prefix.isascii() or not prefix.isdecimal()):
            return None
        try:
            net = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return None
        if net.version == 6 and ipaddress.IPv6Address(address).ipv4_mapped is not None:
            return None
        entries.append(_bytes_field(2, _bytes_field(1, net.network_address.packed)
                                    + _int_field(2, net.prefixlen)))
    return _bytes_field(1, _bytes_field(1, _CODE) + b"".join(entries))


def _publish_asset(directory, kind, content):
    filename = f"marzban-routing-{kind}-{hashlib.sha256(content).hexdigest()}.dat"
    path = directory / filename
    reference = f"ext:{filename}:{_CODE.decode()}"
    try:
        with path.open("rb") as existing:
            if existing.read(len(content) + 1) == content:
                return reference
    except FileNotFoundError:
        pass

    with NamedTemporaryFile(dir=directory, delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.close()
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
    return reference


def with_cached_matchers(routing, assets_path):
    """Return a wire-only routing copy without changing rule order or user lists."""
    directory = Path(assets_path)
    rules = []
    for source in routing.get("rules", []):
        if source.get("outboundTag") == "API":
            rules.append(source)
            continue
        rule = dict(source)
        for field, kind, encode in (
            ("domain", "site", _site_asset),
            ("domains", "site", _site_asset),
            ("ip", "ip", _ip_asset),
            ("source", "ip", _ip_asset),
            ("sourceIP", "ip", _ip_asset),
            ("localIP", "ip", _ip_asset),
        ):
            values = rule.get(field)
            if isinstance(values, list) and values and all(isinstance(value, str) for value in values):
                asset = encode(values)
                if asset is not None:
                    rule[field] = [_publish_asset(directory, kind, asset)]
        rules.append(rule)
    return {**routing, "rules": rules}


def prepare_routing(routing, assets_path):
    """Use inline conditions if the assets directory cannot be used."""
    if not assets_path or not routing.get("rules"):
        return routing
    try:
        return with_cached_matchers(routing, assets_path)
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not generate routing assets (%s); using inline matchers", type(exc).__name__)
        return routing
