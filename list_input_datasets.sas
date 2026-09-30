/*---------------------------------------------------------------------
  List every dataset READ by a SAS program (comments ignored).
  Runs in SAS Viya Compute via PROC PYTHON.

  Detects inputs from:
    - DATA step:  SET / MERGE / UPDATE / MODIFY  (incl. IF ... THEN SET)
    - Hash objects: declare hash h(dataset:'lib.tbl')
    - Any PROC:   DATA= option  (PROC SORT, MEANS, APPEND, etc.)
    - PROC SQL:   FROM / JOIN clauses (comma lists, aliases, subqueries)
  Ignores:
    - /* block */ comments, * statement comments;  %* macro comments;
    - text inside quoted strings, DATALINES/CARDS blocks
    - outputs (DATA x; OUT=; CREATE TABLE), _NULL_, _LAST_
---------------------------------------------------------------------*/

/* Path of the SAS program to scan (on the Compute server filesystem) */
%let code_path = /path/to/your/program.sas;

proc python;
submit;
import re
import pandas as pd

code_path = SAS.symget('code_path')
with open(code_path, 'r', encoding='utf-8', errors='replace') as f:
    code = f.read()

# ---------------------------------------------------------------
# 1. Remove comments and split into statements (quote-aware)
# ---------------------------------------------------------------
def split_statements(src):
    stmts, buf = [], []
    i, n = 0, len(src)
    at_start = True                        # at start of a statement?
    while i < n:
        c = src[i]
        # block comment /* ... */ (valid anywhere outside quotes)
        if src.startswith('/*', i):
            j = src.find('*/', i + 2)
            i = n if j == -1 else j + 2
            buf.append(' ')
            continue
        # statement comments:  * ... ;   and   %* ... ;
        if at_start and (c == '*' or src.startswith('%*', i)):
            j = src.find(';', i)
            i = n if j == -1 else j + 1
            continue
        # quoted strings - copied as-is so ';' or '/*' inside are ignored
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if src[j] == c:
                    if j + 1 < n and src[j + 1] == c:   # doubled quote ''
                        j += 2
                        continue
                    break
                j += 1
            buf.append(src[i:j + 1])
            i = j + 1
            at_start = False
            continue
        if c == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf, at_start = [], True
            i += 1
            # skip in-stream data after DATALINES / CARDS
            m = re.fullmatch(r'(datalines|cards|lines|parmcards)(4?)', stmt, re.I)
            if m:
                end_pat = r'^\s*;;;;' if m.group(2) else r'^\s*;'
                em = re.search(end_pat, src[i:], re.M)
                i = n if not em else i + em.end()
            continue
        if not c.isspace():
            at_start = False
        buf.append(c)
        i += 1
    tail = ''.join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts

def blank_strings(s):
    """Replace quoted text with a placeholder so keywords inside are ignored."""
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", s)

# ---------------------------------------------------------------
# 2. Helpers
# ---------------------------------------------------------------
NAME = r"[A-Za-z_&%][\w&.%]*"
SKIP = {'_NULL_', '_LAST_', '_DATA_'}

def norm(ds):
    ds = ds.strip().rstrip('.').upper()
    if '.' not in ds:                          # one-level name -> WORK (&lib..&t stays)
        ds = 'WORK.' + ds
    return ds

def strip_parens(s):
    """Remove (...) dataset options, handling nesting."""
    out, depth = [], 0
    for ch in s:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(depth - 1, 0)
        elif depth == 0:
            out.append(ch)
    return ''.join(out)

def names_from_list(text):
    """Dataset names from 'a b(keep=x) c end=eof nobs=n' style lists."""
    text = strip_parens(text)
    text = re.sub(r"\b\w+\s*=\s*\S+", ' ', text)     # drop END=, NOBS=, KEY= ...
    return [t for t in re.findall(NAME + r"(?::|-\w+)?", text)]

SQL_STOP = {'WHERE', 'GROUP', 'ORDER', 'HAVING', 'ON', 'JOIN', 'INNER', 'LEFT',
            'RIGHT', 'FULL', 'CROSS', 'NATURAL', 'UNION', 'EXCEPT', 'INTERSECT',
            'OUTER', 'AS', 'SELECT', 'SET', 'VALUES'}

def sql_inputs(stmt):
    toks = re.findall(r"[\w&.%]+|\(|\)|,", strip_sql_strings(stmt))
    found, k = [], 0
    while k < len(toks):
        t = toks[k].upper()
        if t in ('FROM', 'JOIN'):
            k += 1
            while k < len(toks):
                if toks[k] == '(' or toks[k].upper() == 'SELECT':  # subquery: scanned separately
                    break
                found.append(toks[k])
                k += 1
                # skip optional alias / dataset options
                if k < len(toks) and toks[k].upper() == 'AS':
                    k += 2
                elif k < len(toks) and toks[k] not in (',', '(', ')') \
                        and toks[k].upper() not in SQL_STOP:
                    k += 1
                if k < len(toks) and toks[k] == ',' and t == 'FROM':
                    k += 1
                    continue
                break
        else:
            k += 1
    return found

def strip_sql_strings(s):
    s = blank_strings(s)
    # drop dataset options like tbl(where=(...)) but keep subqueries (select ...)
    def repl(m):
        return m.group(0) if re.match(r"\(\s*select\b", m.group(0).strip(), re.I) else ' '
    prev = None
    while prev != s:                                   # repeat for nesting
        prev = s
        s = re.sub(r"(?<=[\w&.])\s*\((?:[^()]|\([^()]*\))*\)", repl, s)
    # flatten subquery parens so their FROM/JOIN are scanned too
    return re.sub(r"\(\s*(?=select\b)", ' ', s, flags=re.I)

# ---------------------------------------------------------------
# 3. Walk the statements
# ---------------------------------------------------------------
inputs, created = [], set()
mode = None                                  # 'data', 'sql', 'proc' or None

def add_input(ds, where):
    if ds.upper() in SKIP:
        return
    d = norm(ds)
    inputs.append({'DATASET': d,
                   'SOURCE': where,
                   'CREATED_EARLIER_IN_CODE': 'Y' if d in created else 'N'})

for raw in split_statements(code):
    s = blank_strings(raw)
    first = s.split(None, 1)[0].upper() if s.split() else ''

    if first == 'DATA' and not re.match(r'data\s*=', s, re.I):
        mode = 'data'
        body = s[4:]
        for ds in names_from_list(body.split('/')[0]):
            if ds.upper() not in SKIP:
                created.add(norm(ds))
        continue
    if first == 'PROC':
        pname = (s.split() + ['', ''])[1].upper()
        mode = 'sql' if pname == 'SQL' else 'proc'
    elif first in ('RUN', 'QUIT'):
        if first == 'QUIT' or mode != 'sql':
            mode = None
        continue

    # DATA= / BASE= on any PROC statement or step option
    if mode in ('proc', 'sql') or first == 'PROC':
        for m in re.finditer(r"\bdata\s*=\s*(" + NAME + r")", s, re.I):
            add_input(m.group(1), 'PROC DATA=')
        for m in re.finditer(r"\bout\s*=\s*(" + NAME + r")", s, re.I):
            created.add(norm(m.group(1)))

    if mode == 'data':
        for m in re.finditer(r"(?:^|\bthen\s+|\belse\s+|\bdo\s*;?\s*)"
                             r"(set|merge|update|modify)\b(.*)", s, re.I):
            for ds in names_from_list(m.group(2)):
                add_input(ds, 'DATA step ' + m.group(1).upper())
        for m in re.finditer(r"dataset\s*:\s*['\"]([^'\"]+)['\"]", raw, re.I):
            add_input(m.group(1).split('(')[0], 'Hash object')

    if mode == 'sql':
        for ds in sql_inputs(raw):
            add_input(ds, 'PROC SQL FROM/JOIN')
        m = re.search(r"\bcreate\s+(?:table|view)\s+(" + NAME + r")", s, re.I)
        if m:
            created.add(norm(m.group(1)))

# ---------------------------------------------------------------
# 4. Output
# ---------------------------------------------------------------
df = pd.DataFrame(inputs, columns=['DATASET', 'SOURCE', 'CREATED_EARLIER_IN_CODE'])
df = df.drop_duplicates().reset_index(drop=True)

print('Input datasets found in', code_path)
print(df.to_string(index=False) if len(df) else '  (none)')
print('\nExternal inputs (not created earlier in the code):')
for d in sorted(set(df.loc[df.CREATED_EARLIER_IN_CODE == 'N', 'DATASET'])):
    print('  ', d)

SAS.df2sd(df, 'work.input_datasets')
endsubmit;
run;

title "Input datasets used by &code_path";
proc print data=work.input_datasets noobs; run;
title;
