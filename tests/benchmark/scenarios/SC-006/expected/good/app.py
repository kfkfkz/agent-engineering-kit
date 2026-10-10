"""Known-good recovery queries independent commit facts before any new effect."""
import json
import sys
import urllib.parse
import urllib.request


def run(request):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    query = urllib.parse.urlencode({"key": request["key"]})
    with opener.open(request["service"] + "/status?" + query, timeout=2) as response:
        previous = json.load(response)
    if previous["receipt"] is not None:
        return {"receipt": previous["receipt"], "reused": True}
    body = json.dumps({"key": request["key"]}).encode("utf-8")
    call = urllib.request.Request(request["service"] + "/commit", data=body,
                                  headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(call, timeout=2) as response:
        committed = json.load(response)
    if request.get("crash_after_commit"):
        return {"error": "INTERRUPTED"}
    return {"receipt": committed["receipt"], "reused": False}


if __name__ == "__main__":
    print(json.dumps(run(json.loads(sys.argv[1]))))
