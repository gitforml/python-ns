#!/usr/bin/env python3
"""
egp_audit.py - Full audit of SAS Enterprise Guide project(s) (.egp).

Produces, in one run:
  flows.csv        one row per process flow (task count, code lines)
  tasks.csv        one row per code block, mapped to its flow
  paths.csv        every filesystem path (libname/filename/%include/infile/
                   file/datafile=/outfile=/ods)
  connections.csv  every external connection - DB2, Snowflake, Oracle,
                   Teradata, ODBC, Hadoop, FTP/SFTP, URL, email, etc.
  flows/<Flow>.sas per-flow SAS file, each block preceded by a comment
                   banner naming the task / query it came from
  ALL_CODE.sas     everything merged, in flow order

Passwords are masked in all CSV output; the .sas files keep the original code
untouched.

Usage:
    python egp_audit.py MyProject.egp
    python egp_audit.py MyProject.egp -o C:\\out
    python egp_audit.py C:\\projects -r -o C:\\out
"""

import argparse
import csv
import glob
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

# --------------------------------------------------------------------------
# Pattern library
# --------------------------------------------------------------------------
CODE_TAGS = {"code", "sascode", "generatedcode", "sourcecode", "text", "value"}
LABEL_KEYS = ("Label", "Name", "label", "name", "DisplayName", "Title")
FLOW_HINT = re.compile(r"(processflow|codeflow|flow|container|eg_?flow)", re.I)
SAS_HINT = re.compile(
    r"\b(proc\s+\w+|data\s+\w|libname|filename|%macro|%let|%include|create\s+table|select\s+)",
    re.I)

# --- filesystem paths -----------------------------------------------------
PATH_PATTERNS = [
    ("LIBNAME",  re.compile(r"""\blibname\s+(\w+)\s+(?:\w+\s+)?["']([^"']+)["']""", re.I)),
    ("FILENAME", re.compile(r"""\bfilename\s+(\w+)\s+(?:\w+\s+)?["']([^"']+)["']""", re.I)),
    ("INCLUDE",  re.compile(r"""%include\s+["']([^"']+)["']""", re.I)),
    ("INFILE",   re.compile(r"""\binfile\s+["']([^"']+)["']""", re.I)),
    ("FILE_OUT", re.compile(r"""^\s*file\s+["']([^"']+)["']""", re.I | re.M)),
    ("DATAFILE", re.compile(r"""\bdatafile\s*=\s*["']([^"']+)["']""", re.I)),
    ("OUTFILE",  re.compile(r"""\boutfile\s*=\s*["']([^"']+)["']""", re.I)),
    ("ODS",      re.compile(r"""\bods\s+\w+\s+(?:file|path|body)\s*=\s*["']([^"']+)["']""", re.I)),
]
BARE_PATH = re.compile(
    r"""["']((?:[A-Za-z]:[\\/]|\\\\|/(?:home|data|sas|opt|mnt|users|apps|prod)/)[^"'\n]{2,})["']""",
    re.I)

# --- database engines recognised by SAS/ACCESS ----------------------------
DB_ENGINES = (
    "db2|snow|snowflake|sasiosnf|oracle|teradata|tera|sqlsvr|sqlserver|odbc|oledb|"
    "postgres|postgresql|netezza|hadoop|hive|impala|spark|redshift|bigquery|"
    "saphana|hana|mysql|mariadb|informix|greenplm|greenplum|aster|sybase|sybaseiq|"
    "jdbc|db2unix|mongo|salesforce|yellowbrick|vertica|exasol"
)
CONNECT_TO = re.compile(
    r"\bconnect\s+to\s+(" + DB_ENGINES + r")\b\s*(?:as\s+(\w+)\s*)?\(([^;]*?)\)", re.I | re.S)
CONNECT_USING = re.compile(r"\bconnect\s+using\s+(\w+)", re.I)
LIBNAME_DB = re.compile(
    r"\blibname\s+(\w+)\s+(" + DB_ENGINES + r")\b([^;]*);", re.I | re.S)
LIBNAME_META = re.compile(r"\blibname\s+(\w+)\s+meta\b([^;]*);", re.I | re.S)
FILENAME_DEV = re.compile(
    r"\bfilename\s+(\w+)\s+(ftp|sftp|ftps|url|email|emailx?|socket|hadoop|sftpx)\b([^;]*);",
    re.I | re.S)
PROC_HTTP = re.compile(r"\bproc\s+http\b([^;]*?)\burl\s*=\s*[\"']([^\"']+)[\"']", re.I | re.S)
PROC_DB = re.compile(r"\bproc\s+(dbload|dbcopy|federated|hadoop|sqoop)\b", re.I)

# --- option scraping ------------------------------------------------------
OPT = lambda key: re.compile(key + r"\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s;)]+)", re.I)
OPT_SERVER   = OPT(r"\b(?:server|host|hostname|node|dsn|datasrc|account|url|path)")
OPT_DB       = OPT(r"\b(?:database|db|catalog|schema|warehouse|role|dbname)")
OPT_USER     = OPT(r"\b(?:user|userid|uid|username)")
OPT_PWD      = OPT(r"\b(?:password|passwd|pwd|pass)")
OPT_AUTH     = OPT(r"\b(?:authdomain|auth_domain|authenticator)")
OPT_PORT     = OPT(r"\b(?:port|service)")


def unq(v):
    v = (v or "").strip()
    return v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'" else v


def first(pat, text):
    m = pat.search(text or "")
    return unq(m.group(1)) if m else ""


def lineno(text, pos):
    return text[:pos].count("\n") + 1


# --------------------------------------------------------------------------
# ZIP / XML helpers
# --------------------------------------------------------------------------
def decode(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def tag_of(e):
    return e.tag.split("}")[-1].lower()


def label_of(e):
    for k in LABEL_KEYS:
        v = e.attrib.get(k)
        if v and v.strip():
            return v.strip()
    for c in e:
        if tag_of(c) in ("label", "name", "displayname", "title") and c.text:
            if c.text.strip():
                return c.text.strip()
    return ""


def ancestor_label(e, pmap, depth=6):
    node = e
    for _ in range(depth):
        lab = label_of(node)
        if lab:
            return lab
        node = pmap.get(node)
        if node is None:
            break
    return ""


def is_flow(e):
    blob = tag_of(e) + " " + " ".join(str(v) for v in e.attrib.values())
    return bool(FLOW_HINT.search(blob))


def flow_of(e, pmap, depth=12):
    node = pmap.get(e)
    for _ in range(depth):
        if node is None:
            break
        if is_flow(node):
            return label_of(node) or "Unnamed flow"
        node = pmap.get(node)
    return ""


def kind_of(e, pmap, depth=6):
    node = e
    for _ in range(depth):
        if node is None:
            break
        low = (tag_of(node) + " " + " ".join(str(v) for v in node.attrib.values())).lower()
        if "query" in low:
            return "Query Builder"
        if "program" in low:
            return "SAS Program"
        if "task" in low:
            return "Task"
        node = pmap.get(node)
    return "Code"


def safe(name, fallback="item"):
    s = re.sub(r"[^\w\-. ]", "_", (name or "")).strip().rstrip(".")
    return (s or fallback)[:70]


# --------------------------------------------------------------------------
# Scanners
# --------------------------------------------------------------------------
def scan_paths(code, ctx):
    rows, seen = [], set()
    for kind, pat in PATH_PATTERNS:
        for m in pat.finditer(code):
            g = m.groups()
            ref, path = (g[0], g[1]) if len(g) == 2 else ("", g[0])
            key = (kind, ref, path)
            if key in seen:
                continue
            seen.add(key)
            rows.append({**ctx, "type": kind, "ref": ref, "path": path,
                         "line": lineno(code, m.start())})
    for m in BARE_PATH.finditer(code):
        p = m.group(1)
        if any(r["path"] == p for r in rows):
            continue
        rows.append({**ctx, "type": "OTHER", "ref": "", "path": p,
                     "line": lineno(code, m.start())})
    return rows


def scan_connections(code, ctx):
    rows = []

    def add(category, tech, target, database="", user="", port="",
            auth="", pwd=False, pos=0, detail=""):
        rows.append({**ctx, "category": category, "technology": tech,
                     "server_or_target": target, "database_schema": database,
                     "user": user, "port": port, "auth_domain": auth,
                     "password_in_code": "YES" if pwd else "",
                     "line": lineno(code, pos), "detail": detail[:200]})

    # PROC SQL pass-through
    for m in CONNECT_TO.finditer(code):
        eng, alias, opts = m.group(1), m.group(2) or "", m.group(3)
        add("PASS-THROUGH", eng.upper(),
            first(OPT_SERVER, opts), first(OPT_DB, opts), first(OPT_USER, opts),
            first(OPT_PORT, opts), first(OPT_AUTH, opts),
            bool(OPT_PWD.search(opts)), m.start(),
            f"connect to {eng}" + (f" as {alias}" if alias else ""))
    for m in CONNECT_USING.finditer(code):
        add("PASS-THROUGH", "VIA LIBREF", m.group(1), pos=m.start(),
            detail="connect using " + m.group(1))

    # LIBNAME to a database
    for m in LIBNAME_DB.finditer(code):
        ref, eng, opts = m.group(1), m.group(2), m.group(3)
        add("DB LIBNAME", eng.upper(),
            first(OPT_SERVER, opts), first(OPT_DB, opts), first(OPT_USER, opts),
            first(OPT_PORT, opts), first(OPT_AUTH, opts),
            bool(OPT_PWD.search(opts)), m.start(), f"libname {ref} {eng}")
    for m in LIBNAME_META.finditer(code):
        add("METADATA LIBNAME", "META", "", first(OPT_DB, m.group(2)),
            pos=m.start(), detail=f"libname {m.group(1)} meta")

    # FILENAME devices: FTP / SFTP / URL / EMAIL
    for m in FILENAME_DEV.finditer(code):
        ref, dev, opts = m.group(1), m.group(2), m.group(3)
        host = first(OPT_SERVER, opts)
        if not host:
            q = re.search(r"[\"']([^\"']+)[\"']", opts)
            host = unq(q.group(1)) if q else ""
        add("FILE TRANSFER", dev.upper(), host, "", first(OPT_USER, opts),
            first(OPT_PORT, opts), "", bool(OPT_PWD.search(opts)),
            m.start(), f"filename {ref} {dev}")

    # PROC HTTP / other DB procs
    for m in PROC_HTTP.finditer(code):
        add("WEB", "PROC HTTP", m.group(2), pos=m.start(), detail="proc http")
    for m in PROC_DB.finditer(code):
        add("DB UTILITY", "PROC " + m.group(1).upper(), "", pos=m.start(),
            detail=m.group(0))

    return rows


# --------------------------------------------------------------------------
# Project reader
# --------------------------------------------------------------------------
def read_project(egp_path):
    """Return (blocks, flow_names). Each block: flow/label/type/code/source."""
    blocks, flow_names = [], []
    try:
        zf = zipfile.ZipFile(egp_path)
    except zipfile.BadZipFile:
        print(f"  ! {os.path.basename(egp_path)}: not a ZIP "
              "(EG 4.x or password-protected)")
        return blocks, flow_names
    except Exception as e:
        print(f"  ! {os.path.basename(egp_path)}: {e}")
        return blocks, flow_names

    with zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        xmls = [n for n in names if os.path.basename(n).lower()
                in ("project.xml", "egp.xml", "content.xml")]

        stem_meta = {}  # loose-member stem -> (label, flow)

        for xm in xmls:
            text = decode(zf.read(xm))
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                for i, m in enumerate(re.finditer(r"<!\[CDATA\[(.*?)\]\]>", text, re.S), 1):
                    body = m.group(1).strip()
                    if SAS_HINT.search(body) and len(body) > 15:
                        blocks.append({"flow": "Unassigned", "label": f"Block {i}",
                                       "type": "Code", "code": body, "source": xm})
                continue

            pmap = {c: p for p in root.iter() for c in p}

            for e in root.iter():
                if is_flow(e):
                    fn = label_of(e) or "Unnamed flow"
                    if fn not in flow_names:
                        flow_names.append(fn)

                if tag_of(e) in CODE_TAGS and e.text:
                    body = e.text.strip()
                    if len(body) >= 15 and SAS_HINT.search(body):
                        blocks.append({
                            "flow": flow_of(e, pmap) or "Unassigned",
                            "label": ancestor_label(e, pmap) or "Unnamed task",
                            "type": kind_of(e, pmap), "code": body, "source": xm})

                for val in list(e.attrib.values()) + [e.text or ""]:
                    val = (val or "").strip()
                    if not val or len(val) > 200:
                        continue
                    stem = os.path.splitext(os.path.basename(val))[0].lower()
                    if len(stem) >= 6 and re.fullmatch(r"[\w\-]+", stem):
                        lab = ancestor_label(e, pmap)
                        if lab and stem not in stem_meta:
                            stem_meta[stem] = (lab, flow_of(e, pmap) or "Unassigned")

        for n in names:
            if not n.lower().endswith(".sas"):
                continue
            body = decode(zf.read(n)).strip()
            if len(body) < 15 or not SAS_HINT.search(body):
                continue
            stem = os.path.splitext(os.path.basename(n))[0].lower()
            lab, flw = stem_meta.get(stem, (stem, "Unassigned"))
            blocks.append({"flow": flw, "label": lab, "type": "Task",
                           "code": body, "source": n})

    # drop duplicate code
    seen, out = set(), []
    for b in blocks:
        k = re.sub(r"\s+", " ", b["code"]).strip().lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(b)

    for b in out:
        if b["flow"] not in flow_names:
            flow_names.append(b["flow"])
    return out, flow_names


def banner(idx, b, project):
    w = 74
    s = "/*" + "*" * w + "\n"
    s += f" * [{idx:03d}] {b['label']}\n"
    s += f" * Type    : {b['type']}\n"
    s += f" * Flow    : {b['flow']}\n"
    s += f" * Project : {project}\n"
    s += f" * Source  : {b['source']}\n"
    s += " " + "*" * w + "*/\n"
    return s


def write_csv(rows, fields, dest):
    if not rows:
        return
    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  -> {os.path.basename(dest)} ({len(rows)} rows)")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Audit .egp projects: flows, code, paths, DB/FTP connections")
    ap.add_argument("targets", nargs="+", help=".egp file(s), glob, or folder")
    ap.add_argument("-o", "--out", default="egp_audit", help="output folder")
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

    os.makedirs(args.out, exist_ok=True)
    flow_rows, task_rows, path_rows, conn_rows = [], [], [], []
    idx = 0

    all_path = os.path.join(args.out, "ALL_CODE.sas")
    all_fh = open(all_path, "w", encoding="utf-8")
    all_fh.write(f"/* egp_audit.py - {len(files)} project(s) - "
                 f"{datetime.now():%Y-%m-%d %H:%M} */\n")

    for f in files:
        project = os.path.basename(f)
        blocks, flows = read_project(f)
        print(f"\n=== {project}: {len(flows)} flow(s), {len(blocks)} code block(s)")

        flow_dir = os.path.join(args.out, safe(os.path.splitext(project)[0]), "flows")
        os.makedirs(flow_dir, exist_ok=True)

        for flow in flows:
            fb = [b for b in blocks if b["flow"] == flow]
            if not fb:
                flow_rows.append({"project": project, "flow": flow, "tasks": 0,
                                  "code_lines": 0, "file": ""})
                continue

            fname = os.path.join(flow_dir, safe(flow, "flow") + ".sas")
            lines = 0
            with open(fname, "w", encoding="utf-8") as fh:
                fh.write(f"/* FLOW: {flow}  |  PROJECT: {project}  |  "
                         f"{len(fb)} task(s) */\n")
                for b in fb:
                    idx += 1
                    head = banner(idx, b, project)
                    fh.write("\n" + head + "\n" + b["code"].rstrip() + "\n")
                    all_fh.write("\n" + head + "\n" + b["code"].rstrip() + "\n")
                    n = b["code"].count("\n") + 1
                    lines += n

                    ctx = {"project": project, "flow": flow, "task": b["label"],
                           "task_type": b["type"], "source": b["source"]}
                    task_rows.append({**ctx, "code_lines": n, "flow_file":
                                      os.path.basename(fname), "seq": idx})
                    path_rows += scan_paths(b["code"], ctx)
                    conn_rows += scan_connections(b["code"], ctx)

            flow_rows.append({"project": project, "flow": flow, "tasks": len(fb),
                              "code_lines": lines,
                              "file": os.path.relpath(fname, args.out)})
            print(f"    {flow:<34} {len(fb):>3} task(s), {lines:>5} lines")

    all_fh.close()

    print("\nWriting summaries:")
    write_csv(flow_rows, ["project", "flow", "tasks", "code_lines", "file"],
              os.path.join(args.out, "flows.csv"))
    write_csv(task_rows, ["project", "flow", "seq", "task", "task_type",
                          "code_lines", "source", "flow_file"],
              os.path.join(args.out, "tasks.csv"))
    write_csv(path_rows, ["project", "flow", "task", "type", "ref", "path", "line"],
              os.path.join(args.out, "paths.csv"))
    write_csv(conn_rows, ["project", "flow", "task", "category", "technology",
                          "server_or_target", "database_schema", "user", "port",
                          "auth_domain", "password_in_code", "line", "detail"],
              os.path.join(args.out, "connections.csv"))

    if conn_rows:
        techs = {}
        for r in conn_rows:
            techs[r["technology"]] = techs.get(r["technology"], 0) + 1
        print("\nConnections found: " +
              ", ".join(f"{k} ({v})" for k, v in sorted(techs.items())))
        if any(r["password_in_code"] for r in conn_rows):
            print("  ** Hard-coded passwords detected - see connections.csv **")
    print(f"\nOutput folder: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
