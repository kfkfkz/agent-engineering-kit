"""A controlled simultaneous-arrival harness exposes a check-then-act race."""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor


def reserve_parallel(keys):
    if not keys:
        return {"accepted": 0, "committed": []}
    ledger = {}
    barrier = threading.Barrier(len(keys))

    def reserve(key):
        present = key in ledger
        barrier.wait(timeout=2)
        if present:
            return False
        ledger[key] = True
        return True

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        accepted = sum(pool.map(reserve, keys))
    return {"accepted": accepted, "committed": sorted(ledger)}


if __name__ == "__main__":
    print(json.dumps(reserve_parallel(json.loads(sys.argv[1])["keys"])))
