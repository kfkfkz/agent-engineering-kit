"""Known-good atomic reservation with the same simultaneous-arrival harness."""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor


def reserve_parallel(keys):
    if not keys:
        return {"accepted": 0, "committed": []}
    ledger = {}
    barrier = threading.Barrier(len(keys))
    lock = threading.Lock()

    def reserve(key):
        barrier.wait(timeout=2)
        with lock:
            if key in ledger:
                return False
            ledger[key] = True
            return True

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        accepted = sum(pool.map(reserve, keys))
    return {"accepted": accepted, "committed": sorted(ledger)}


if __name__ == "__main__":
    print(json.dumps(reserve_parallel(json.loads(sys.argv[1])["keys"])))
