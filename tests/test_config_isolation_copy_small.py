"""Each test's private copy of the shipped config stays small.

The config-isolation fixture copies config/ for every test. Large files there
(catalog snapshots) would be copied thousands of times in a full run and can
fill the disk, so the copy leaves them out.
"""
import os

from puffpai import config as cfgmod

LIMIT_BYTES = 5 * 1024 * 1024


def _size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    return total


def test_the_per_test_config_copy_stays_small():
    config_dir = os.environ.get(cfgmod.CONFIG_DIR_ENV, "")
    assert config_dir and os.path.isdir(config_dir)
    assert _size(config_dir) < LIMIT_BYTES


def test_the_per_test_config_copy_has_no_catalog_snapshots():
    config_dir = os.environ.get(cfgmod.CONFIG_DIR_ENV, "")
    assert not os.path.exists(os.path.join(config_dir, "catalog_snapshots"))
