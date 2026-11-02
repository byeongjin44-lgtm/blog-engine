"""Runs a site's own calculator code, so numbers in posts come from code, never from the AI.

HomeRivet's calculators live in its site repo (TypeScript). The draft workflow checks that repo out
and this module calls its CLI (`scripts/calc.mts`) with Node:

    [calculator]
    command = ["node", "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", "vendor/homerivettools/scripts/calc.mts"]

Each result row has `key`, `label`, `value`, `unit`, `digits`, `primary`, `number` (the exact text
the post must show) and `display` (number + unit).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

from engine.config import ROOT, Site

# (argv) -> stdout. Replaced in tests.
Runner = Callable[[list[str]], str]


class CalculatorError(RuntimeError):
    pass


def _subprocess_runner(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise CalculatorError(f"calculator command not found: {argv[0]} ({e})") from None
    if done.returncode != 0:
        detail = (done.stdout.strip() or done.stderr.strip())[:500]
        raise CalculatorError(f"calculator failed: {detail}")
    return done.stdout


runner: Runner = _subprocess_runner


def _call(site: Site, arg: str):
    if not site.calculator or not site.calculator.get("command"):
        raise CalculatorError(f"site {site.id} has no [calculator] command")
    out = runner([*site.calculator["command"], arg])
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise CalculatorError(f"calculator returned invalid JSON: {out[:300]}") from None
    if isinstance(data, dict) and "error" in data:
        raise CalculatorError(data["error"])
    return data


def run(site: Site, calculations: list[dict]) -> list[dict]:
    """[{calculator, input, results}] for each requested run. Raises CalculatorError on bad input."""
    if not calculations:
        return []
    for c in calculations:
        if site.tool(c.get("calculator")) is None:
            raise CalculatorError(f"unknown calculator '{c.get('calculator')}'")
    return _call(site, json.dumps([{"calculator": c["calculator"], "input": dict(c.get("input", {}))} for c in calculations]))


def fields(site: Site) -> list[dict]:
    """[{calculator, fields}] — input fields with defaults and options."""
    return _call(site, "--list")


NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def known_numbers(site: Site, results: list[dict], field_list: list[dict] | None = None) -> set[float]:
    """Numbers a post may show as decimals: calculator results and inputs, field defaults and option
    labels, and the site's `extra_numbers` (published constants such as R-values)."""
    known: set[float] = set()

    def add_text(text) -> None:
        for m in NUMBER_RE.findall(str(text)):
            known.add(float(m.replace(",", "")))

    for run_ in results:
        for v in run_.get("input", {}).values():
            add_text(v)
        for r in run_.get("results", []):
            known.add(float(r["value"]))
            add_text(r.get("number", ""))
    for calc in field_list or []:
        for f in calc.get("fields", []):
            add_text(f.get("default", ""))
            for o in f.get("options", []):
                add_text(o.get("value", ""))
                add_text(o.get("label", ""))
    for n in (site.calculator or {}).get("extra_numbers", []):
        known.add(float(n))
    return known


def effective_inputs(run_: dict, field_list: list[dict]) -> list[tuple[str, str]]:
    """(label, value with unit) for every input the calculator actually used: the topic's values,
    defaults for the rest, skipping fields hidden by another field's choice."""
    calc = next((c for c in field_list if c["calculator"] == run_["calculator"]), None)
    if calc is None:
        return [(k, str(v)) for k, v in run_.get("input", {}).items()]
    values = {f["key"]: run_.get("input", {}).get(f["key"], f["default"]) for f in calc["fields"]}
    out = []
    for f in calc["fields"]:
        cond = f.get("showWhen")
        if cond and str(values.get(cond["key"])) != cond["equals"]:
            continue
        v = values[f["key"]]
        if f.get("kind") == "select":
            v = next((o["label"] for o in f.get("options", []) if o["value"] == str(v)), v)
        out.append((f["label"], f"{v} {f['unit']}" if f.get("unit") else str(v)))
    return out


def describe(results: list[dict], field_list: list[dict] | None = None) -> str:
    """Plain-English list of runs, their inputs and results, for prompts and the review message."""
    lines = []
    for i, run_ in enumerate(results, 1):
        inputs = "; ".join(f"{label}: {value}" for label, value in effective_inputs(run_, field_list or []))
        lines.append(f"{i}. {run_['calculator']} — inputs: {inputs or 'defaults'}")
        for r in run_["results"]:
            mark = " (HEADLINE — must appear exactly as written)" if r.get("primary") else ""
            lines.append(f"   - {r['label']}: {r['display']}{mark}")
    return "\n".join(lines)
