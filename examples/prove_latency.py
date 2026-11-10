"""Proof for the demo goal "Search answers in under 200 ms": python3 prove_latency.py URL

Stands in for a real check (start an instance, trigger a release, watch it upgrade): it times 20
requests and passes only if the 95th percentile is under 200 ms.
"""
import sys
import time
import urllib.request

url = sys.argv[1]
times = []
for _ in range(20):
    start = time.perf_counter()
    urllib.request.urlopen(url, timeout=5).read()
    times.append((time.perf_counter() - start) * 1000)
p95 = sorted(times)[18]
print(f"20 requests to {url}: p95 {p95:.0f} ms, slowest {max(times):.0f} ms")
sys.exit(0 if p95 < 200 else f"p95 is {p95:.0f} ms, not under 200 ms")
