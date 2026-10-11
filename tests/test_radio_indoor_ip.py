"""radio_player._indoor_ip: only plain public addresses may be streamed from.

It delegates to the shared classifier in puffpai.security.netaddr, so the
ranges Python's own is_private has moved between releases (CGNAT, the
documentation and benchmark nets) and wrapped forms are all refused.
"""

import ipaddress

import pytest

from puffpai import radio_player


@pytest.mark.parametrize("addr", [
    '100.64.0.1', '100.100.100.100', '100.127.255.254',   # CGNAT / Tailscale
    '127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.2', '169.254.169.254',
    '0.0.0.0', '224.0.0.1', '240.0.0.1',
    '192.0.2.1', '198.51.100.7', '203.0.113.9', '198.18.0.1',
    '::1', 'fe80::1', 'fc00::1', 'fd12:3456::1', '2001:db8::1',
    '::ffff:10.0.0.1', '::ffff:127.0.0.1', '2002:7f00:1::',
])
def test_not_public_is_indoor(addr):
    assert radio_player._indoor_ip(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize("addr", ['8.8.8.8', '1.1.1.1', '93.184.216.34',
                                  '2606:4700:4700::1111'])
def test_public_is_not_indoor(addr):
    assert radio_player._indoor_ip(ipaddress.ip_address(addr)) is False


def test_cgnat_stream_url_is_refused():
    # CGNAT space must be treated as indoor.
    assert radio_player._indoor_ip(ipaddress.ip_address('100.64.0.1'))
