import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.aquira.normalize import as_str, unwrap, unwrap_deep
from app.aquira.fieldvalues import unwrap as fv_unwrap
from app.sync.planner import field_diff

print("1. as_str on spec 'untyped Value' shape ->", repr(as_str({"Value": {}, "Label": "Name", "Access": 1})))
print("   truthy?", bool(as_str({"Value": {}, "Label": "Name", "Access": 1})))
print("2. as_str nested Value->{} (fixture shape) ->", repr(as_str({"Value": {"Value": "100 Main St"}, "Access": 2})))
print("3. fieldvalues.unwrap double-wrap ->", repr(fv_unwrap({"Value": {"Value": "100 Main St"}})), "  <-- single level?")
print("4. normalize.unwrap double-wrap ->", repr(unwrap({"Value": {"Value": "100 Main St"}})))
print("5. planner.field_diff sees residual wrapper as a CHANGE:")
print("  ", field_diff({"name": {"Value": "ACME"}}, {"name": "ACME"}))
print("6. '{}'.lower() beats an 'or' fallback, as in normalize_client:387:")
print("  ", repr(as_str({"Value": {}}) or "FALLBACK-NAME"))
