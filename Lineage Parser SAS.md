
`/*===============================================================
  Change this to the location of the SAS program you want to scan
================================================================*/
%let sas_file=/path/to/your/program.sas;


proc python;
submit;

import re
import pandas as pd


# ==============================================================
# SETTINGS
# ==============================================================

SAS_SOURCE_FILE = r"""&sas_file"""

# One-level SAS datasets will be treated as WORK.dataset.
# Change this if your environment uses USER= as the default library.
DEFAULT_ONE_LEVEL_LIB = "WORK"

OUTPUT_SAS_DATASET = "WORK.SAS_DATASET_LINEAGE"


# ==============================================================
# 1. READ SAS PROGRAM
# ==============================================================

with open(SAS_SOURCE_FILE, "r", encoding="utf-8", errors="ignore") as f:
    sas_code = f.read()


# ==============================================================
# 2. REMOVE SAS COMMENTS
#
# Handles:
#     /* block comments */
#
#     * statement comments;
#
#     %* macro comments;
#
# Quoted text is preserved.
# ==============================================================

def strip_sas_comments(text):

    output = []
    i = 0
    n = len(text)

    quote = None

    # True means we are at the beginning of a SAS statement.
    # This is required to correctly recognize:
    #
    # * comment;
    #
    statement_start = True

    while i < n:

        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        # ------------------------------------------------------
        # Inside quoted string
        # ------------------------------------------------------
        if quote:

            output.append(ch)

            if ch == quote:

                # SAS escapes quotes by doubling them:
                # 'John''s'
                if i + 1 < n and text[i + 1] == quote:
                    output.append(text[i + 1])
                    i += 2
                    continue

                quote = None

            i += 1
            continue

        # ------------------------------------------------------
        # Start quoted string
        # ------------------------------------------------------
        if ch in ("'", '"'):
            quote = ch
            output.append(ch)
            statement_start = False
            i += 1
            continue

        # ------------------------------------------------------
        # /* block comment */
        # ------------------------------------------------------
        if ch == "/" and nxt == "*":

            i += 2

            while i < n - 1 and not (
                text[i] == "*" and text[i + 1] == "/"
            ):
                # Preserve newlines so program structure remains
                # reasonably intact.
                if text[i] == "\n":
                    output.append("\n")
                else:
                    output.append(" ")

                i += 1

            if i < n - 1:
                i += 2

            continue

        # ------------------------------------------------------
        # %* macro comment;
        # ------------------------------------------------------
        if ch == "%" and nxt == "*":

            i += 2

            while i < n and text[i] != ";":

                if text[i] == "\n":
                    output.append("\n")
                else:
                    output.append(" ")

                i += 1

            if i < n:
                output.append(";")
                i += 1

            statement_start = True
            continue

        # ------------------------------------------------------
        # * SAS statement comment;
        #
        # Only interpret * as a comment when it is the first
        # non-whitespace character of a statement.
        # ------------------------------------------------------
        if statement_start and ch == "*":

            i += 1

            while i < n and text[i] != ";":

                if text[i] == "\n":
                    output.append("\n")
                else:
                    output.append(" ")

                i += 1

            if i < n:
                output.append(";")
                i += 1

            statement_start = True
            continue

        # ------------------------------------------------------
        # Normal character
        # ------------------------------------------------------
        output.append(ch)

        if ch == ";":
            statement_start = True

        elif not ch.isspace():
            statement_start = False

        i += 1

    return "".join(output)


sas_code = strip_sas_comments(sas_code)


# ==============================================================
# 3. SPLIT SAS CODE INTO STATEMENTS
#
# We do not simply use text.split(";") because semicolons can
# occur inside quoted strings.
# ==============================================================

def split_sas_statements(text):

    statements = []

    buffer = []
    quote = None

    i = 0

    while i < len(text):

        ch = text[i]

        if quote:

            buffer.append(ch)

            if ch == quote:

                if i + 1 < len(text) and text[i + 1] == quote:
                    buffer.append(text[i + 1])
                    i += 2
                    continue

                quote = None

            i += 1
            continue

        if ch in ("'", '"'):

            quote = ch
            buffer.append(ch)

            i += 1
            continue

        if ch == ";":

            statement = "".join(buffer).strip()

            if statement:
                statements.append(statement)

            buffer = []
            i += 1
            continue

        buffer.append(ch)
        i += 1

    statement = "".join(buffer).strip()

    if statement:
        statements.append(statement)

    return statements


statements = split_sas_statements(sas_code)


# ==============================================================
# 4. DATASET NAME REGEX
#
# Supports things such as:
#
#     lib.table
#     table
#     &lib..table
#     &dataset.
#     'table name'n
# ==============================================================

COMPONENT = r"""
(?:
      '(?:''|[^'])*'[nN]
    | "(?:""|[^"])*"[nN]
    | &[A-Za-z_][A-Za-z0-9_]*\.?
    | [A-Za-z_][A-Za-z0-9_$#@]*
)
"""

DATASET_PATTERN = rf"""
(?P<dataset>
    {COMPONENT}
    (?:\.{COMPONENT})?
)
"""

dataset_regex = re.compile(
    DATASET_PATTERN,
    re.IGNORECASE | re.VERBOSE
)


# ==============================================================
# 5. REMOVE DATASET OPTIONS
#
# Example:
#
#     raw.sales
#       (keep=id amount where=(amount > 0))
#
# becomes:
#
#     raw.sales
# ==============================================================

def remove_parenthetical_content(text):

    output = []

    depth = 0
    quote = None
    i = 0

    while i < len(text):

        ch = text[i]

        if quote:

            if depth == 0:
                output.append(ch)
            else:
                output.append(" ")

            if ch == quote:

                if i + 1 < len(text) and text[i + 1] == quote:

                    if depth == 0:
                        output.append(text[i + 1])
                    else:
                        output.append(" ")

                    i += 2
                    continue

                quote = None

            i += 1
            continue

        if ch in ("'", '"'):

            quote = ch
            output.append(ch if depth == 0 else " ")

            i += 1
            continue

        if ch == "(":

            depth += 1
            output.append(" ")

            i += 1
            continue

        if ch == ")" and depth:

            depth -= 1
            output.append(" ")

            i += 1
            continue

        if depth == 0:
            output.append(ch)
        else:
            output.append("\n" if ch == "\n" else " ")

        i += 1

    return "".join(output)


# ==============================================================
# 6. DETERMINE LIBRARY + MEMBER NAME
# ==============================================================

def split_library_dataset(raw_name):

    raw_name = raw_name.strip().rstrip(",")

    quote = None
    dots = []

    i = 0

    while i < len(raw_name):

        ch = raw_name[i]

        if quote:

            if ch == quote:

                if i + 1 < len(raw_name) and raw_name[i + 1] == quote:
                    i += 2
                    continue

                quote = None

            i += 1
            continue

        if ch in ("'", '"'):

            quote = ch
            i += 1
            continue

        if ch == ".":
            dots.append(i)

        i += 1

    separator = None

    # Work backwards so:
    #
    # &lib..table
    #
    # uses the second dot as the lib/member separator.
    for pos in reversed(dots):

        if pos < len(raw_name) - 1:
            separator = pos
            break

    if separator is None:

        library = DEFAULT_ONE_LEVEL_LIB
        dataset = raw_name.rstrip(".")

    else:

        library = raw_name[:separator].rstrip(".")
        dataset = raw_name[separator + 1:].rstrip(".")

    return library, dataset


# ==============================================================
# 7. COLLECT REFERENCES
# ==============================================================

references = []


def add_reference(raw_name, access_type, found_in):

    if not raw_name:
        return

    raw_name = raw_name.strip()

    # Ignore special SAS pseudo-datasets
    if raw_name.upper().rstrip(".") in {
        "_NULL_",
        "_LAST_"
    }:
        return

    # Avoid SQL keywords accidentally being treated as tables
    if raw_name.upper() in {
        "SELECT",
        "CONNECTION",
        "VALUES",
        "TABLE"
    }:
        return

    library, dataset = split_library_dataset(raw_name)

    full_name = f"{library}.{dataset}"

    references.append({
        "ACCESS_TYPE": access_type,
        "LIBRARY": library,
        "DATASET": dataset,
        "FULL_DATASET": full_name,
        "FOUND_IN": found_in
    })


# ==============================================================
# Helper for SET / MERGE / DATA lists
# ==============================================================

def get_dataset_list(statement, keyword, option_words=None):

    option_words = option_words or []

    match = re.match(
        rf"(?is)^\s*{keyword}\b(.*)$",
        statement
    )

    if not match:
        return []

    body = match.group(1)

    # Remove (keep=...), (rename=...), etc.
    body = remove_parenthetical_content(body)

    # DATA statement can contain:
    # data xyz / view=xyz;
    if keyword.lower() == "data" and "/" in body:
        body = body.split("/", 1)[0]

    # Stop before statement-level options such as:
    # end=
    # nobs=
    # indsname=
    if option_words:

        options_pattern = "|".join(
            re.escape(x) for x in option_words
        )

        option_match = re.search(
            rf"(?i)\b(?:{options_pattern})\s*=",
            body
        )

        if option_match:
            body = body[:option_match.start()]

    results = []

    for match in dataset_regex.finditer(body):

        raw = match.group("dataset")

        before = body[:match.start()].rstrip()

        # Avoid option=value being interpreted as a dataset
        if before.endswith("="):
            continue

        results.append(raw)

    return results


# ==============================================================
# 8. PARSE SAS STATEMENTS
# ==============================================================

in_proc_sql = False


for statement in statements:

    lower = statement.strip().lower()

    # ----------------------------------------------------------
    # PROC SQL context
    # ----------------------------------------------------------
    if re.match(r"^\s*proc\s+sql\b", lower):
        in_proc_sql = True
        continue

    if in_proc_sql and re.match(r"^\s*quit\b", lower):
        in_proc_sql = False
        continue

    # ----------------------------------------------------------
    # DATA statement = OUTPUT
    #
    # data mart.final work.audit;
    # ----------------------------------------------------------
    if re.match(r"^\s*data\b", lower):

        datasets = get_dataset_list(
            statement,
            "data"
        )

        for ds in datasets:
            add_reference(
                ds,
                "OUTPUT",
                "DATA statement"
            )

    # ----------------------------------------------------------
    # SET = INPUT
    # ----------------------------------------------------------
    if re.match(r"^\s*set\b", lower):

        datasets = get_dataset_list(
            statement,
            "set",
            [
                "end",
                "indsname",
                "key",
                "point",
                "nobs",
                "open"
            ]
        )

        for ds in datasets:
            add_reference(
                ds,
                "INPUT",
                "SET statement"
            )

    # ----------------------------------------------------------
    # MERGE = INPUT
    # ----------------------------------------------------------
    if re.match(r"^\s*merge\b", lower):

        datasets = get_dataset_list(
            statement,
            "merge",
            [
                "end",
                "indsname"
            ]
        )

        for ds in datasets:
            add_reference(
                ds,
                "INPUT",
                "MERGE statement"
            )

    # ----------------------------------------------------------
    # UPDATE statement = INPUT
    # DATA statement itself determines output table.
    # ----------------------------------------------------------
    if not in_proc_sql and re.match(r"^\s*update\b", lower):

        datasets = get_dataset_list(
            statement,
            "update",
            [
                "updatemode"
            ]
        )

        for ds in datasets:
            add_reference(
                ds,
                "INPUT",
                "UPDATE statement"
            )

    # ----------------------------------------------------------
    # MODIFY modifies an existing dataset in place
    # ----------------------------------------------------------
    if not in_proc_sql and re.match(r"^\s*modify\b", lower):

        datasets = get_dataset_list(
            statement,
            "modify"
        )

        for ds in datasets:

            add_reference(
                ds,
                "INPUT",
                "MODIFY statement"
            )

            add_reference(
                ds,
                "OUTPUT",
                "MODIFY statement"
            )

    # ----------------------------------------------------------
    # Common PROC options
    #
    # proc sort data=RAW.A out=WORK.B;
    #
    # proc means data=RAW.A;
    # output out=WORK.B;
    #
    # proc append base=MART.A data=WORK.B;
    # ----------------------------------------------------------

    statement_without_options = remove_parenthetical_content(
        statement
    )

    # DATA= --> input
    for match in re.finditer(
        rf"(?is)\bdata\s*=\s*{DATASET_PATTERN}",
        statement_without_options
    ):

        add_reference(
            match.group("dataset"),
            "INPUT",
            "DATA= option"
        )

    # OUT= --> output
    for match in re.finditer(
        rf"(?is)\bout\s*=\s*{DATASET_PATTERN}",
        statement_without_options
    ):

        add_reference(
            match.group("dataset"),
            "OUTPUT",
            "OUT= option"
        )

    # BASE= --> append target/output
    for match in re.finditer(
        rf"(?is)\bbase\s*=\s*{DATASET_PATTERN}",
        statement_without_options
    ):

        add_reference(
            match.group("dataset"),
            "OUTPUT",
            "BASE= option"
        )

    # ==========================================================
    # PROC SQL
    # ==========================================================
    if in_proc_sql:

        # ------------------------------------------------------
        # CREATE TABLE / VIEW = OUTPUT
        # ------------------------------------------------------
        match = re.search(
            rf"""(?is)
                \bcreate\s+
                (?:table|view)\s+
                {DATASET_PATTERN}
            """,
            statement,
            re.VERBOSE
        )

        if match:

            add_reference(
                match.group("dataset"),
                "OUTPUT",
                "PROC SQL CREATE"
            )

        # ------------------------------------------------------
        # INSERT INTO = OUTPUT
        # ------------------------------------------------------
        match = re.search(
            rf"""(?is)
                \binsert\s+into\s+
                {DATASET_PATTERN}
            """,
            statement,
            re.VERBOSE
        )

        if match:

            add_reference(
                match.group("dataset"),
                "OUTPUT",
                "PROC SQL INSERT"
            )

        # ------------------------------------------------------
        # SQL UPDATE = INPUT + OUTPUT
        # ------------------------------------------------------
        match = re.match(
            rf"""(?is)
                \s*update\s+
                {DATASET_PATTERN}
            """,
            statement,
            re.VERBOSE
        )

        if match:

            ds = match.group("dataset")

            add_reference(
                ds,
                "INPUT",
                "PROC SQL UPDATE"
            )

            add_reference(
                ds,
                "OUTPUT",
                "PROC SQL UPDATE"
            )

        # ------------------------------------------------------
        # DELETE FROM = INPUT + OUTPUT
        # ------------------------------------------------------
        match = re.match(
            rf"""(?is)
                \s*delete\s+from\s+
                {DATASET_PATTERN}
            """,
            statement,
            re.VERBOSE
        )

        if match:

            ds = match.group("dataset")

            add_reference(
                ds,
                "INPUT",
                "PROC SQL DELETE"
            )

            add_reference(
                ds,
                "OUTPUT",
                "PROC SQL DELETE"
            )

        # ------------------------------------------------------
        # ALTER TABLE = OUTPUT
        # ------------------------------------------------------
        match = re.match(
            rf"""(?is)
                \s*alter\s+table\s+
                {DATASET_PATTERN}
            """,
            statement,
            re.VERBOSE
        )

        if match:

            add_reference(
                match.group("dataset"),
                "OUTPUT",
                "PROC SQL ALTER"
            )

        # ------------------------------------------------------
        # FROM = INPUT
        #
        # This also catches FROM clauses inside subqueries.
        # ------------------------------------------------------
        for match in re.finditer(
            rf"""(?is)
                \bfrom\s+
                {DATASET_PATTERN}
            """,
            statement,
            re.VERBOSE
        ):

            ds = match.group("dataset")

            if ds.upper() != "CONNECTION":

                add_reference(
                    ds,
                    "INPUT",
                    "PROC SQL FROM"
                )

        # ------------------------------------------------------
        # JOIN = INPUT
        # ------------------------------------------------------
        for match in re.finditer(
            rf"""(?is)
                \bjoin\s+
                {DATASET_PATTERN}
            """,
            statement,
            re.VERBOSE
        ):

            add_reference(
                match.group("dataset"),
                "INPUT",
                "PROC SQL JOIN"
            )


# ==============================================================
# 9. CREATE FINAL LINEAGE TABLE
#
# If same dataset occurs as INPUT and OUTPUT, ACCESS_TYPE = BOTH.
# ==============================================================

if references:

    detail = pd.DataFrame(references)

    final_rows = []

    for full_dataset, grp in detail.groupby(
        detail["FULL_DATASET"].str.upper(),
        sort=True
    ):

        first = grp.iloc[0]

        roles = set(
            grp["ACCESS_TYPE"]
            .str.upper()
            .tolist()
        )

        if "INPUT" in roles and "OUTPUT" in roles:
            access = "BOTH"

        elif "OUTPUT" in roles:
            access = "OUTPUT"

        else:
            access = "INPUT"

        contexts = sorted(
            set(grp["FOUND_IN"].tolist())
        )

        final_rows.append({
            "ACCESS_TYPE": access,
            "LIBRARY": first["LIBRARY"],
            "DATASET": first["DATASET"],
            "FULL_DATASET": first["FULL_DATASET"],
            "FOUND_IN": " | ".join(contexts)
        })

    result = pd.DataFrame(final_rows)

    result = result.sort_values(
        ["ACCESS_TYPE", "LIBRARY", "DATASET"]
    ).reset_index(drop=True)

else:

    result = pd.DataFrame(
        columns=[
            "ACCESS_TYPE",
            "LIBRARY",
            "DATASET",
            "FULL_DATASET",
            "FOUND_IN"
        ]
    )


# ==============================================================
# 10. WRITE RESULT BACK TO SAS
# ==============================================================

SAS.df2sd(
    result,
    OUTPUT_SAS_DATASET
)

print(result)

endsubmit;
run;


/* Optional display */
proc print data=work.sas_dataset_lineage noobs;
run;`
