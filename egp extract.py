#!/usr/bin/env python3
"""
egp_extract.py - Unpack SAS Enterprise Guide .egp projects with plain Python.

An .egp file is a ZIP archive. This script will:
  1. List every internal entry (the "paths" inside the archive)
  2. Extract all embedded SAS code (loose .sas members + code stored in project.xml)
  3. Scan that code for real filesystem paths (libname, %include, infile/file,
     proc import/export datafile=, and any bare Windows/UNIX path)
  4. Write everything to an output folder + two CSV summaries

Usage:
    python egp_extract.py MyProject.egp
    python egp_extract.py C:\\projects\\*.egp -o C:\\out
    python egp_extract.py C:\\projects --recursive -o C:\\out
"""

import argparse
import csv
import glob
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------
# Path-detection patterns
# --------------------------------------------------------------------------
PATH_PATTERNS = [
    ("LIBNAME",   re.compile(r"""\blibname\s+(\w+)\s+(?:\w+\s+)?["']([^"']+)["']""", re.I)),
    ("FILENAME",  re.compile(r"""\bfilename\s+(\w+)\s+(?:\w+\s+)?["']([^"']+)["']""", re.I)),
    ("INCLUDE",   re.compile(r"""%include\s+["']([^"']+)["']""", re.I)),
    ("INFILE",    re.compile(r"""\binfile\s+["']([^"']+)["']""", re.I)),
    ("FILE_OUT",  re.compile(r"""^\s*file\s+["']([^"']+)["']""", re.I | re.M)),
    ("DATAFILE",  re.compile(r"""\bdatafile\s*=\s*["']([^"']+)["']""", re.I)),
    ("OUTFILE",   re.compile(r"""\boutfile\s*=\s*["']([^"']+)["']""", re.I)),
    ("ODS_FILE",  re.compile(r"""\bods\s+\w+\s+(?:file|path)\s*=\s*["']([^"']+)["']""", re.I)),
]

# Catch-all: quoted strings that look like a real path
BARE_PATH = re.compile(
    r"""["']((?:[A-Za-z]:[\\/]|\\\\|/(?:home|data|sas|opt|mnt|users)/)[^"'\n]{2,})["']""",
    re.I,
)

SAS_EXT = (".sas", ".txt", ".log")


# --------------------------------------------------------------------------
def iter_egp_files(targets, recursive=False):
    """Expand args (files, globs, folders) into a list of .egp paths."""
    found = []
    for t in targets:
        if os.path.isdir(t):
            pat = "**/*.egp" if recursive else "*.egp"
            found += glob.glob(os.path.join(t, pat), recursive=recursive)
        elif any(c in t for c in "*?["):
            found += glob.glob(t, recursive=recursive)
        else:
            found.append(t)
    return sorted({os.path.abspath(f) for f in found if f.lower().endswith(".egp")})


def clean_name(name, fallback):
    """Make a task label safe for use as a filename."""
    if not name:
        return fallback
    safe = re.sub(r"[^\w\-. ]", "_", name).strip().rstrip(".")
    return (safe or fallback)[:80]


def read_zip_member(zf, member):
    try:
        raw = zf.read(member)
    except Exception as e:
        print(f"    ! cannot read {member}: {e}")
        return None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def extract_code_from_xml(text):
    """Pull <Code>/<SASCode>/... elements out of project.xml.

    Returns list of (label, code) tuples.
    """
    blocks = []

    # Try a real XML parse first
    def label_of(el):
        for key in ("Label", "Name", "label", "name"):
            if el.attrib.get(key):
                return el.attrib[key]
        for child in el:
            if child.tag.split("}")[-1].lower() in ("label", "name") and child.text:
                return child.text.strip()
        return ""

    try:
        root = ET.fromstring(text)
        parent = {c: p for p in root.iter() for c in p}
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            if tag in ("code", "sascode", "generatedcode", "text") and elem.text:
                body = elem.text.strip()
                if len(body) < 15:
                    continue
                # Label may live on the element, or on an ancestor Task node
                label, node = "", elem
                for _ in range(4):
                    label = label_of(node)
                    if label:
                        break
                    node = parent.get(node)
                    if node is None:
                        break
                blocks.append((label, body))
    except ET.ParseError:
        pass

    # Fallback / supplement: raw CDATA sweep (catches malformed or nested XML)
    if not blocks:
        for m in re.finditer(r"<!\[CDATA\[(.*?)\]\]>", text, re.S):
            body = m.group(1).strip()
            if re.search(r"\b(proc|data|libname|select|create table)\b", body, re.I):
                blocks.append(("", body))

    return blocks


def find_paths(code, source_label):
    """Return list of dicts describing every path reference in a chunk of code."""
    hits = []
    seen = set()

    for kind, pat in PATH_PATTERNS:
        for m in pat.finditer(code):
            groups = m.groups()
            ref, path = (groups[0], groups[1]) if len(groups) == 2 else ("", groups[0])
            key = (kind, ref, path)
            if key in seen:
                continue
            seen.add(key)
            line = code[: m.start()].count("\n") + 1
            hits.append(
                {"source": source_label, "type": kind, "ref": ref,
                 "path": path, "line": line}
            )

    for m in BARE_PATH.finditer(code):
        path = m.group(1)
        if any(path == h["path"] for h in hits):
            continue
        key = ("OTHER", "", path)
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            {"source": source_label, "type": "OTHER", "ref": "",
             "path": path, "line": code[: m.start()].count("\n") + 1}
        )

    return hits


# --------------------------------------------------------------------------
def process_egp(egp_path, out_root):
    name = os.path.splitext(os.path.basename(egp_path))[0]
    out_dir = os.path.join(out_root, clean_name(name, "project"))
    code_dir = os.path.join(out_dir, "code")
    os.makedirs(code_dir, exist_ok=True)

    print(f"\n=== {egp_path}")

    members, path_rows = [], []
    combined = []

    try:
        zf = zipfile.ZipFile(egp_path)
    except zipfile.BadZipFile:
        print("  ! Not a valid ZIP. EG 4.x projects or password-protected "
              "projects can't be opened this way.")
        return [], []
    except Exception as e:
        print(f"  ! {e}")
        return [], []

    with zf:
        # ---- 1. internal archive listing -------------------------------
        for info in zf.infolist():
            members.append(
                {"egp": egp_path, "internal_path": info.filename,
                 "size_bytes": info.file_size, "compressed": info.compress_size,
                 "modified": "%04d-%02d-%02d %02d:%02d" % info.date_time[:5]}
            )
        print(f"  {len(members)} entries in archive")

        # ---- 2a. loose code members ------------------------------------
        n = 0
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(SAS_EXT):
                continue
            text = read_zip_member(zf, info.filename)
            if not text or not text.strip():
                continue
            n += 1
            base = clean_name(os.path.basename(info.filename), f"member_{n}")
            if not base.lower().endswith(".sas"):
                base += ".sas"
            dest = os.path.join(code_dir, f"{n:03d}_{base}")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(text)
            label = info.filename
            combined.append((label, text))
            path_rows += find_paths(text, label)

        # ---- 2b. code embedded in project.xml ---------------------------
        xml_members = [i.filename for i in zf.infolist()
                       if os.path.basename(i.filename).lower()
                       in ("project.xml", "egp.xml", "content.xml")]
        for xm in xml_members:
            text = read_zip_member(zf, xm)
            if not text:
                continue
            for i, (label, code) in enumerate(extract_code_from_xml(text), 1):
                n += 1
                label = label or f"task_{i}"
                dest = os.path.join(code_dir, f"{n:03d}_{clean_name(label, 'task')}.sas")
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write(code)
                src = f"{xm}::{label}"
                combined.append((src, code))
                path_rows += find_paths(code, src)

    print(f"  {len(combined)} code blocks extracted -> {code_dir}")

    # ---- 3. one merged .sas file for easy reading ----------------------
    if combined:
        merged = os.path.join(out_dir, f"{clean_name(name, 'project')}_ALL_CODE.sas")
        with open(merged, "w", encoding="utf-8") as fh:
            for label, code in combined:
                fh.write(f"\n/* {'=' * 68}\n   {label}\n   {'=' * 68} */\n\n")
                fh.write(code.rstrip() + "\n")
        print(f"  merged -> {merged}")

    print(f"  {len(path_rows)} path references found")
    for r in path_rows:
        r["egp"] = egp_path
    return members, path_rows


def write_csv(rows, fields, dest):
    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  -> {dest}")


def main():
    ap = argparse.ArgumentParser(description="Extract SAS code and paths from .egp files")
    ap.add_argument("targets", nargs="+", help=".egp file(s), glob, or folder")
    ap.add_argument("-o", "--out", default="egp_output", help="output folder")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="recurse into subfolders when a folder is given")
    args = ap.parse_args()

    files = iter_egp_files(args.targets, args.recursive)
    if not files:
        print("No .egp files matched.")
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)
    all_members, all_paths = [], []
    for f in files:
        m, p = process_egp(f, args.out)
        all_members += m
        all_paths += p

    print(f"\nProcessed {len(files)} project(s)")
    if all_members:
        write_csv(all_members,
                  ["egp", "internal_path", "size_bytes", "compressed", "modified"],
                  os.path.join(args.out, "archive_contents.csv"))
    if all_paths:
        write_csv(all_paths, ["egp", "source", "type", "ref", "path", "line"],
                  os.path.join(args.out, "paths_found.csv"))
        uniq = sorted({r["path"] for r in all_paths})
        print(f"  {len(uniq)} unique paths")


if __name__ == "__main__":
    main()
