"""In-memory stand-in for resilient.SimpleClient covering the endpoints used here."""

import copy
import re


def select(name, options, custom=False):
    return {
        "name": name,
        "prefix": "properties" if custom else None,
        "values": [{"value": value, "label": label} for label, value in options.items()],
    }


def text(name, custom=True):
    return {"name": name, "prefix": "properties" if custom else None, "values": []}


MSSP_FIELDS = [
    select("severity_code", {"Low": 19355, "Medium": 19356, "High": 19357, "Critical": 19420}),
    select(
        "resolution_id",
        {
            "True-Positive Resolved": 19428,
            "Non-Issue": 19432,
            "False-Positive": 19433,
            "Action Taken": 19545,
        },
    ),
    text("name", custom=False),
    text("description", custom=False),
    text("plan_status", custom=False),
    text("customer_incident_id"),
    text("analyst"),
    text("date_reported"),
    text("external_alert_id"),
]

CUSTOMER_FIELDS = [
    select("severity_code", {"Low": 4, "Medium": 5, "High": 6, "Critical": 1116}),
    select(
        "resolution_id",
        {
            "True-Positive Resolved": 1117,
            "Non-Issue": 1121,
            "False-Positive": 1122,
            "Known Offense (Test)": 1120,
            "Action Taken": 782,
        },
    ),
    select(
        "customer_resolution",
        {"Action Taken": 890, "Not an Issue": 889, "False Positive": 897, "Known Activity": 891},
        custom=True,
    ),
    text("name", custom=False),
    text("description", custom=False),
    text("plan_status", custom=False),
    text("mssp_incident_id"),
    text("analyst_assigned"),
    text("describe_the_incident"),
    text("provide_recommendation"),
    text("date_reported"),
    text("external_alert_id"),
    text("source_incident_id"),
]


class FakeResilient:
    def __init__(self, fields, first_id=1000):
        self.fields = fields
        self.incidents = {}
        self.comments = {}
        self.tasks = {}
        self.task_notes = {}
        self.artifacts = {}
        self.patches = []
        self.posts = []
        self.fail_on = set()
        self.reject_full_incident = False
        self._next_id = first_id

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def add_incident(self, incident_id, **values):
        incident = {"id": incident_id, "plan_status": "A", "properties": {}, "vers": 1, **values}
        self.incidents[incident_id] = incident
        return incident

    def get(self, uri, **_):
        path = uri.split("?")[0]
        if path == "/types/incident/fields":
            return copy.deepcopy(self.fields)
        match = re.fullmatch(r"/incidents/(\d+)(/(comments|tasks|artifacts)(/(\d+))?)?", path)
        incident_id = int(match.group(1))
        kind, item_id = match.group(3), match.group(5)
        if kind is None:
            return copy.deepcopy(self.incidents.get(incident_id))
        if kind == "comments" and item_id:
            return next(c for c in self.comments.get(incident_id, []) if c["id"] == int(item_id))
        return copy.deepcopy(getattr(self, kind).get(incident_id, []))

    def post(self, uri, payload, **_):
        self.posts.append((uri, copy.deepcopy(payload)))
        if any(marker in uri for marker in self.fail_on):
            raise RuntimeError(f"rejected {uri}")
        if uri == "/incidents":
            if self.reject_full_incident and "severity_code" in payload:
                raise RuntimeError("400 invalid field")
            return self.add_incident(self._new_id(), **copy.deepcopy(payload))
        if uri.startswith("/incidents/query_paged"):
            return {"data": self._query(payload)}
        match = re.fullmatch(r"/(incidents|tasks)/(\d+)/(comments|tasks|artifacts)", uri)
        owner, owner_id, kind = match.group(1), int(match.group(2)), match.group(3)
        record = {"id": self._new_id(), **copy.deepcopy(payload)}
        if owner == "tasks":
            self.task_notes.setdefault(owner_id, []).append(record)
        else:
            if kind == "comments":
                record["text"] = payload["text"]["content"]
            getattr(self, kind).setdefault(owner_id, []).append(record)
        return record

    def patch(self, uri, patch, overwrite_conflict=False):
        incident_id = int(uri.rsplit("/", 1)[1])
        changes = patch.to_dict()["changes"]
        self.patches.append((incident_id, {c["field"]: c["new_value"]["object"] for c in changes}))
        incident = self.incidents[incident_id]
        custom = {f["name"] for f in self.fields if f["prefix"] == "properties"}
        for change in changes:
            name, value = change["field"], change["new_value"]["object"]
            if name.startswith("properties."):
                raise AssertionError("SOAR patch fields must not carry the properties. prefix")
            if name in custom:
                incident["properties"][name] = value
            else:
                incident[name] = value
        return {"success": True}

    def _query(self, payload):
        rows = sorted(self.incidents.values(), key=lambda item: item["id"])
        for group in payload.get("filters", []):
            for condition in group.get("conditions", []):
                field, value = condition["field_name"], condition["value"]
                if field.startswith("properties."):
                    rows = [
                        r
                        for r in rows
                        if str(r["properties"].get(field.split(".", 1)[1])) == str(value)
                    ]
                else:
                    rows = [r for r in rows if r.get(field) == value]
        if payload["sorts"][0]["type"] == "desc":
            rows.reverse()
        return copy.deepcopy(rows[payload["start"] : payload["start"] + payload["length"]])
