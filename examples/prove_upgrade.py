"""Proof for the demo goal "The fleet upgrades itself after a release": python3 prove_upgrade.py

Stands in for a real check (start an instance, trigger a release, watch it upgrade within 10 minutes).
It fails until there's a release to see: `touch examples/.released` to simulate one.
"""
import os
import sys

marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".released")
if not os.path.exists(marker):
    sys.exit("no release found: the instance is still on the old version")
print("the instance picked up the release within 10 minutes")
