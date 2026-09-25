"""Offline rehearsal of probe_read_census (and probe_sweep_tail) against a fake tenant.

The fake speaks exactly what the live tenant was measured saying (2026-09-24): zero
matches answer HTTP 200 + Success:false + ErrorName "NotFound" (-12), successful calls
carry ErrorName as the STRING "None", and one guessed endpoint 404s. Running the real
section code against it proves the new code does not crash and that its verdict logic
fires on the right shape — cheaper than discovering either at the cost of a live round.
"""
import importlib.util as u
import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path.cwd()))

spec = u.spec_from_file_location("cp", "scripts/conformance_probe.py")
cp = u.module_from_spec(spec)
spec.loader.exec_module(cp)

RICH = set(range(1, 200))          # contracts whose sweep row carries an amount
ALIVE = set(range(1, 251))         # contract ids that exist
CLIENTS = set(range(1, 281))


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.content = b"x"
        self.text = json.dumps(payload)

    def json(self):
        return self._p


def envelope(rows=None, entity=None, error_name="None", error=0, success=True):
    return {"Success": success, "ErrorName": error_name, "Error": error,
            "Data": rows if rows is not None else [], "Entity": entity or {}}


def handle(method, path, body):
    ids = set((body or {}).get("SearchIDs") or [])
    if path == "/Session/Post":
        return Resp(200, envelope(success=True))
    if path == "/Contract/SearchByID":
        hits = [{"ID": i, "ContractCD": f"C{i}", "Status": 1, "IsActiveFlag": True,
                 "NetAmount": 5000 if i in RICH else None, "StartDate": "2026-01-01",
                 "EndDate": "2026-03-01", "Description": f"spot {i}"} for i in sorted(ids & ALIVE)]
        if not hits:
            return Resp(200, envelope(success=False, error_name="NotFound", error=-12))
        return Resp(200, envelope(hits))
    if path == "/Client/SearchByID":
        hits = [{"ID": i, "ClientCD": str(i), "Name": f"Client {i}", "SalesTeams": "First Dallas Media",
                 "SalesReps": "Chris, Clint", "BusinessPhone1": "214-555-0000", "Type": 3}
                for i in sorted(ids & CLIENTS)]
        if not hits:
            return Resp(200, envelope(success=False, error_name="NotFound", error=-12))
        return Resp(200, envelope(hits))
    if path.startswith("/Contract/Load/"):
        return Resp(200, envelope(entity={"ID": int(path.rsplit("/", 1)[-1]), "ContractCD": "C1",
                                          "Status": 1, "SalesReps": [{"ID": 7, "Selected": True}],
                                          "Version": 2}))
    if path.startswith("/Client/Load/"):
        return Resp(200, envelope(entity={"ID": int(path.rsplit("/", 1)[-1]), "Name": "Acme",
                                          "Email": "a@b.c", "Version": 1}))
    if path == "/Contract/GetContractDetailAnalysis":
        cid = (body or {}).get("ID")
        if cid in RICH:
            return Resp(200, envelope([{"Station": "KTMF", "RevenueDate": "2026-01-15",
                                        "Amount": 1000, "ContractID": cid}]))
        return Resp(200, envelope(success=False, error_name="NotFound", error=-12))
    if path == "/Contract/GetSpotLineDetailAnalysis":
        return Resp(200, envelope(success=False, error_name="NotFound", error=-12))
    if path == "/Contract/LoadSpotline":
        return Resp(404, {"detail": "not found"})
    if path == "/User/Lookup":
        return Resp(200, envelope([{"ID": i, "Name": f"rep{i}", "SalesRepID": i, "IsSalesRep": True}
                                   for i in (6, 7, 8, 14, 15, 16, 18)]))
    if path.endswith("/Get"):
        return Resp(200, envelope([{"ID": i, "Name": f"n{i}"} for i in sorted(CLIENTS)[:100]]))
    if path in ("/Client/Search", "/Contract/Lookup"):
        return Resp(200, envelope([]))
    return Resp(200, envelope([]))


class FakeClient:
    def __init__(self, *a, **k):
        pass

    def request(self, method, path, **kw):
        return handle(method, path, kw.get("json"))

    def post(self, path, **kw):
        return handle("POST", path, kw.get("json"))

    def get(self, path, **kw):
        return handle("GET", path, None)

    def delete(self, path, **kw):
        return handle("DELETE", path, None)

    def close(self):
        pass


fake_httpx = types.SimpleNamespace(Client=FakeClient)
cp.httpx = fake_httpx


class S:
    aquira_base_url = "https://tenant.invalid/api"
    aquira_username = "u"
    aquira_password = "p"


cp._settings = lambda: S

print("#" * 30, "probe_read_census against the mock tenant")
cp.probe_read_census()
print("#" * 30, "exit ok")
