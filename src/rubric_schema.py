import json
from jsonschema import validate, ValidationError
import pandas as pd

RUBRIC_SCHEMA = {
  "type":"object","required":["sections"],
  "properties":{
    "sections":{
      "type":"array",
      "items":{
        "type":"object","required":["id","points","criteria"],
        "properties":{
          "id":{"type":"string","pattern":"^Q\\d+$"},
          "title":{"type":"string"},
          "points":{"type":"number","minimum":0},
          "criteria":{"type":"array","items":{
            "type":"object","required":["id","type","max"],
            "properties":{
              "id":{"type":"string"},
              "criterion":{"type":"string"},
              "type":{"enum":["columns","row_count","stat_range","unique_count","null_rate","table_shape","figure_exists","llm_feedback"]},
              "args":{"type":"object"},
              "max":{"type":"number","minimum":0},
              "auto_only":{"type":["boolean","null"]},
              "bonus_cap":{"type":["number","null"]}
            }}}}}}}}

def load_rubric_json(file_obj):
    data = json.load(file_obj)
    validate(instance=data, schema=RUBRIC_SCHEMA)
    return data

def load_rubric_excel(file_obj):
    x = pd.ExcelFile(file_obj)
    sections = pd.read_excel(x, "Sections").fillna("")
    criteria = pd.read_excel(x, "Criteria").fillna("")
    sections["section_id"] = sections["section_id"].astype(str).str.strip()
    criteria["section_id"] = criteria["section_id"].astype(str).str.strip()

    out = {"sections": []}
    if "order" in sections.columns:
        sections = sections.sort_values("order")

    for _, s in sections.iterrows():
        sec_id = str(s["section_id"])
        pts = float(s.get("points", 0) or 0)
        title = str(s.get("title",""))
        subset = criteria[criteria["section_id"] == sec_id]
        rows = []
        for _, c in subset.iterrows():
            args = {}
            sargs = str(c.get("args_json","")).strip()
            if sargs:
                try:
                    args = json.loads(sargs)
                except Exception as e:
                    raise ValidationError(
                        f"Invalid args_json for criterion_id={c.get('criterion_id')}: {e}"
                    )
            rows.append({
                "id": str(c.get("criterion_id")),
                "criterion": str(c.get("label","")),
                "type": str(c.get("type")),
                "args": args,
                "max": float(c.get("max_points",0) or 0),
                "auto_only": bool(c.get("auto_only")) if str(c.get("auto_only","")).strip() != "" else None,
                "bonus_cap": float(c.get("bonus_cap",0)) if str(c.get("bonus_cap","")).strip() != "" else None
            })
        out["sections"].append({"id": sec_id, "title": title, "points": pts, "criteria": rows})

    validate(instance=out, schema=RUBRIC_SCHEMA)
    return out