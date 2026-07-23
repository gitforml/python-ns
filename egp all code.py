#!/usr/bin/env python3
"""
egp_all_code.py - Extract EVERY SAS code block from a SAS Enterprise Guide
project (.egp) into ONE .sas file, with a comment header naming the task /
query that produced each block.

Handles both places EG stores code:
  * loose members inside the ZIP (code.sas / <guid>.sas / ...)
  * code embedded as CDATA inside project.xml

Loose members are matched back to their task label via project.xml, so a block
saved as "a7f3c2e1.sas" comes out labelled "Query Builder - Sales by Region".

Query Builder tasks that were never run have no generated code stored. For
those, the query DEFINITION (tables, columns, joins, filters) is written out as
a commented block so nothing is silently missed.

Usage:
    python egp_all_code.py MyProject.egp
    python egp_all_code.py MyProject.egp -o AllCode.sas
    python egp_all_code.py C:\\projects -r -o C:\\out\\combined.sas
"""

import argparse
import glob
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

CODE_TAGS = {"code", "sascode", "generatedcode", "sourcecode", "text", "value"}
LABEL_KEYS = ("Label", "Name", "label", "name", "DisplayName", "Title")
CODE_EXT = (".sas",)

SAS_HINT = re.compile(
    r"\b(proc\s+\w+|data\s+\w|libname|%macro|%let|%include|create\s+table|select\s+)",
    re.I,
)


# ---------------------------------------------------------------- utilities
def decode(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def tag_of(elem):
    return elem.tag.split("}")[-1].lower()


def label_of(elem):
    for k in LABEL_KEYS:
        v = elem.attrib.get(k)
        if v and v.strip():
            return v.strip()
    for child in elem:
        if tag_of(child) in ("label", "name", "displayname", "title"):
            if child.text and child.text.strip():
                return child.text.strip()
    return ""


def ancestor_label(elem, parent_map, depth=6):
    node = elem
    for _ in range(depth):
        lab = label_of(node)
        if lab:
            return lab
        node = parent_map.get(node)
        if node is None:
            break
    return ""


def task_type(elem, parent_map, depth=6):
    """Guess whether this block is a Query Builder, program, or other task."""
    node = elem
    for _ in range(depth):
        blob = " ".join([tag_of(node)] + [str(v) for v in node.attrib.values()])
        low = blob.lower()
        if "query" in low:
            return "Query Builder"
        if "sasprogram" in low or "program" in low:
            return "SAS Program"
        if "task" in low:
            return "Task"
        node = parent_map.get(node)
        if node is None:
            break
    return "Code"


def norm(code):
    return re.sub(r"\s+", " ", code).strip().lower()


# ------------------------------------------------------- query definitions
def describe_query(elem, parent_map):
    """Best-effort readable summary of a query builder definition."""
    lines, node = [], elem
    for _ in range(4):
        if node is None:
            break
        parent = parent_map.get(node)
        node = parent if parent is not None else node
    scope = node if node is not None else elem

    tables, columns, joins, filters = [], [], [], []
    for e in scope.iter():
        t, lab = tag_of(e), label_of(e)
        if not lab:
            lab = (e.text or "").strip()
        if not lab:
            continue
        if "table" in t or "dataset" in t or "inputdata" in t:
            tables.append(lab)
        elif "column" in t or "field" in t or "selectitem" in t:
            columns.append(lab)
        elif "join" in t:
            joins.append(lab)
        elif "filter" in t or "where" in t or "condition" in t:
            filters.append(lab)

    def block(title, items):
        items = list(dict.fromkeys(items))[:40]
        if items:
            lines.append(f"   {title}: " + ", ".join(items))

    block("Input tables", tables)
    block("Columns", columns)
    block("Joins", joins)
    block("Filters", filters)
    return lines


# ------------------------------------------------------------- xml parsing
def parse_project_xml(text):
    """Return (code_blocks, member_labels).

    code_blocks : list of dicts with label/type/code
    member_labels: {stem_lowercase: label} for matching loose .sas members
    """
    blocks, member_labels = [], {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Malformed XML - fall back to a raw CDATA sweep
        for i, m in enumerate(re.finditer(r"<!\[CDATA\[(.*?)\]\]>", text, re.S), 1):
            body = m.group(1).strip()
            if SAS_HINT.search(body):
                blocks.append({"label": f"Unnamed block {i}",
                               "type": "Code", "code": body})
        return blocks, member_labels

    parent_map = {c: p for p in root.iter() for c in p}

    for elem in root.iter():
        # --- embedded code -------------------------------------------
        if tag_of(elem) in CODE_TAGS and elem.text:
            body = elem.text.strip()
            if len(body) >= 15 and SAS_HINT.search(body):
                blocks.append({
                    "label": ancestor_label(elem, parent_map) or "Unnamed task",
                    "type": task_type(elem, parent_map),
                    "code": body,
                })

        # --- map file stems to labels ---------------------------------
        candidates = list(elem.attrib.values()) + [elem.text or ""]
        for val in candidates:
            val = (val or "").strip()
            if not val or len(val) > 200:
                continue
            stem = os.path.splitext(os.path.basename(val))[0].lower()
            if len(stem) >= 6 and re.fullmatch(r"[\w\-]+", stem):
                lab = ancestor_label(elem, parent_map)
                if lab and stem not in member_labels:
                    member_labels[stem] = lab

    # --- query builder tasks with no generated code -------------------
    for elem in root.iter():
        blob = (tag_of(elem) + " " + " ".join(str(v) for v in elem.attrib.values())).lower()
        if "query" not in blob:
            continue
        lab = label_of(elem)
        if not lab:
            continue
        has_code = any(tag_of(c) in CODE_TAGS and c.text and len(c.text.strip()) > 15
                       for c in elem.iter())
        if has_code:
            continue
        desc = describe_query(elem, parent_map)
        if desc:
            blocks.append({"label": lab, "type": "Query Builder (definition only)",
                           "code": None, "definition": desc})

    return blocks, member_labels


# ------------------------------------------------------------------ driver
def collect(egp_path):
    """Return an ordered list of code blocks for one .egp."""
    out = []
    try:
        zf = zipfile.ZipFile(egp_path)
    except zipfile.BadZipFile:
        print(f"  ! {os.path.basename(egp_path)}: not a ZIP "
              "(EG 4.x or password-protected project)")
        return out
    except Exception as e:
        print(f"  ! {os.path.basename(egp_path)}: {e}")
        return out

    with zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        xmls = [n for n in names
                if os.path.basename(n).lower() in ("project.xml", "egp.xml", "content.xml")]

        member_labels = {}
        for xm in xmls:
            blocks, labels = parse_project_xml(decode(zf.read(xm)))
            member_labels.update(labels)
            for b in blocks:
                b["source"] = f"{os.path.basename(xm)}"
                out.append(b)

        for n in names:
            if not n.lower().endswith(CODE_EXT):
                continue
            body = decode(zf.read(n)).strip()
            if len(body) < 15 or not SAS_HINT.search(body):
                continue
            stem = os.path.splitext(os.path.basename(n))[0].lower()
            label = member_labels.get(stem) or member_labels.get(
                os.path.splitext(n)[0].lower()) or stem
            out.append({"label": label, "type": "Task", "code": body, "source": n})

    # de-duplicate identical code (EG often stores the same block twice)
    seen, unique = set(), []
    for b in out:
        if b.get("code"):
            k = norm(b["code"])
            if k in seen:
                continue
            seen.add(k)
        unique.append(b)
    return unique


def banner(idx, block, egp_name):
    label = block.get("label", "Unnamed")
    line = "/*" + "*" * 74 + "\n"
    line += f" * [{idx:03d}] {label}\n"
    line += f" * Type    : {block.get('type', 'Code')}\n"
    line += f" * Project : {egp_name}\n"
    line += f" * Source  : {block.get('source', '')}\n"
    for extra in block.get("definition", []):
        line += f" *{extra}\n"
    line += " " + "*" * 74 + "*/\n"
    return line


def main():
    ap = argparse.ArgumentParser(
        description="Extract all SAS code from .egp project(s) into one file")
    ap.add_argument("targets", nargs="+", help=".egp file(s), glob, or folder")
    ap.add_argument("-o", "--out", default="egp_all_code.sas", help="output .sas file")
    ap.add_argument("-r", "--recursive", action="store_true")
    args = ap.parse_args()

    files = []
    for t in args.targets:
        if os.path.isdir(t):
            files += glob.glob(os.path.join(t, "**/*.egp" if args.recursive else "*.egp"),
                               recursive=args.recursive)
        elif any(c in t for c in "*?["):
            files += glob.glob(t, recursive=args.recursive)
        else:
            files.append(t)
    files = sorted({os.path.abspath(f) for f in files if f.lower().endswith(".egp")})

    if not files:
        print("No .egp files matched.")
        sys.exit(1)

    idx, written, skipped = 0, 0, 0
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(f"/* Extracted from {len(files)} EG project(s) on "
                 f"{datetime.now():%Y-%m-%d %H:%M} by egp_all_code.py */\n")
        for f in files:
            name = os.path.basename(f)
            blocks = collect(f)
            print(f"  {name}: {len(blocks)} block(s)")
            fh.write(f"\n\n/* ===== PROJECT: {name} "
                     f"{'=' * max(0, 50 - len(name))} */\n")
            for b in blocks:
                idx += 1
                fh.write("\n" + banner(idx, b, name) + "\n")
                if b.get("code"):
                    fh.write(b["code"].rstrip() + "\n")
                    written += 1
                else:
                    fh.write("/* No generated code stored - run this task in EG "
                             "and re-save the project to capture it. */\n")
                    skipped += 1

    print(f"\n{written} code block(s) written, {skipped} definition-only")
    print(f"-> {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
