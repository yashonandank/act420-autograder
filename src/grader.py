# src/grader.py
from __future__ import annotations
from typing import Dict, Any, Tuple, List

def build_probes_from_rubric(rubric: Dict[str, Any]) -> Dict[str, str]:
    """
    Create a probes dict {probe_id: expr} from rubric criteria that need kernel evaluation.
    Supported types: columns, row_count, stat_range, unique_count, null_rate, table_shape
    figure_exists is UI/HTML-level and not probed here.
    """
    probes = {}
    for sec in rubric["sections"]:
        sid = sec["id"]
        for r in sec["criteria"]:
            cid = r["id"]
            typ = r["type"]
            args = r.get("args", {})
            pid = f"{sid}.{cid}"
            if typ == "columns":
                df = args.get("df", "df")
                probes[pid] = f"list({df}.columns)"
            elif typ == "row_count":
                df = args.get("df", "df")
                probes[pid] = f"len({df})"
            elif typ == "stat_range":
                expr = args.get("expr")
                if expr:
                    probes[pid] = expr
            elif typ == "unique_count":
                expr = args.get("expr")
                if expr:
                    probes[pid] = expr
                else:
                    df = args.get("df","df"); col = args.get("column")
                    if col: probes[pid] = f"{df}['{col}'].nunique()"
            elif typ == "null_rate":
                expr = args.get("expr")
                if expr:
                    probes[pid] = expr
                else:
                    df = args.get("df","df"); col = args.get("column")
                    if col: probes[pid] = f"{df}['{col}'].isna().mean()"
            elif typ == "table_shape":
                df = args.get("df", "df")
                probes[pid] = f"tuple({df}.shape)"
            # llm_feedback / figure_exists -> not probed here
    return probes

def evaluate(rubric: Dict[str, Any], probe_results: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns:
      {
        "sections": [
          {"id":"Q1", "max":10, "earned":8, "rows":[
             {"id":"req_cols","score":3,"max":3,"justification":"all present"},
             ...
          ]}],
        "total":{"max":..., "earned":...}
      }
    """
    out = {"sections": [], "total": {"max": 0.0, "earned": 0.0}}
    for sec in rubric["sections"]:
        sid = sec["id"]
        smax = float(sec.get("points", 0))
        rows = []
        earned = 0.0
        for r in sec["criteria"]:
            cid = r["id"]; typ = r["type"]; args = r.get("args", {}); cmax = float(r.get("max", 0))
            pid = f"{sid}.{cid}"
            score, note = _score_one(typ, args, cmax, probe_results.get(pid, None))
            rows.append({"id": cid, "score": score, "max": cmax, "justification": note})
            earned += score
        # cap at section points if specified
        if smax > 0:
            earned = min(earned, smax)
        out["sections"].append({"id": sid, "max": smax or sum(r["max"] for r in rows), "earned": earned, "rows": rows})
        out["total"]["earned"] += earned
        out["total"]["max"] += (smax or sum(r["max"] for r in rows))
    return out

def _score_one(typ: str, args: Dict[str, Any], cmax: float, probe_val: Any) -> Tuple[float, str]:
    if typ == "columns":
        required = set(args.get("required", []))
        if not isinstance(probe_val, list):
            return 0.0, f"probe failed: expected list of columns, got {repr(probe_val)}"
        got = set(probe_val)
        missing = [c for c in required if c not in got]
        if not missing:
            return cmax, "All required columns present."
        # partial credit
        have = len(required) - len(missing)
        score = round(cmax * have / max(1, len(required)), 2)
        return score, f"Missing columns: {missing}" if missing else "ok"

    if typ == "row_count":
        op = args.get("op", ">="); value = args.get("value", 0)
        if not isinstance(probe_val, (int, float)):
            return 0.0, f"probe failed: expected number, got {repr(probe_val)}"
        ok = (
            (op == ">=" and probe_val >= value) or
            (op == ">" and probe_val > value) or
            (op == "==" and probe_val == value) or
            (op == "<=" and probe_val <= value) or
            (op == "<" and probe_val < value)
        )
        return (cmax if ok else 0.0, f"Row count {probe_val} {op} {value}")

    if typ == "stat_range":
        lo = args.get("min"); hi = args.get("max")
        if isinstance(probe_val, dict) and "__error__" in probe_val:
            return 0.0, f"expr error: {probe_val['__error__']}"
        if not isinstance(probe_val, (int, float)):
            return 0.0, f"probe failed: expected number, got {repr(probe_val)}"
        ok = (lo is None or probe_val >= lo) and (hi is None or probe_val <= hi)
        return (cmax if ok else 0.0, f"value={probe_val}, expected in [{lo}, {hi}]")

    if typ == "unique_count":
        if isinstance(probe_val, dict) and "__error__" in probe_val:
            return 0.0, f"expr error: {probe_val['__error__']}"
        if not isinstance(probe_val, (int, float)):
            return 0.0, f"probe failed: expected number, got {repr(probe_val)}"
        lo = args.get("min"); hi = args.get("max")
        ok = (lo is None or probe_val >= lo) and (hi is None or probe_val <= hi)
        return (cmax if ok else 0.0, f"unique_count={probe_val}, expected in [{lo}, {hi}]")

    if typ == "null_rate":
        if isinstance(probe_val, dict) and "__error__" in probe_val:
            return 0.0, f"expr error: {probe_val['__error__']}"
        if not isinstance(probe_val, (int, float)):
            return 0.0, f"probe failed: expected number, got {repr(probe_val)}"
        lo = args.get("min"); hi = args.get("max")
        ok = (lo is None or probe_val >= lo) and (hi is None or probe_val <= hi)
        return (cmax if ok else 0.0, f"null_rate={probe_val}, expected in [{lo}, {hi}]")

    if typ == "table_shape":
        if not (isinstance(probe_val, (list, tuple)) and len(probe_val) == 2):
            return 0.0, f"probe failed: expected (rows, cols), got {repr(probe_val)}"
        want_rows = args.get("rows"); want_cols = args.get("cols")
        ok_rows = (want_rows is None or probe_val[0] == want_rows)
        ok_cols = (want_cols is None or probe_val[1] == want_cols)
        ok = ok_rows and ok_cols
        return (cmax if ok else 0.0, f"shape={probe_val}, expected rows={want_rows}, cols={want_cols}")

    if typ == "figure_exists":
        # handled later via HTML parsing (TODO). For now give 0 with note.
        return 0.0, "figure_exists check not implemented yet (will use HTML scan)."

    if typ == "llm_feedback":
        # reserved for step 3b
        return 0.0, "LLM feedback pending."
    return 0.0, f"Unknown type: {typ}"