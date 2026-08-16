"""
multipart.py -- a small multipart/form-data reader, standard library only.

Python 3.13 dropped the cgi module, and pulling in a web framework for one file
upload would break the "no unnecessary dependencies" rule. This parses just
enough: text fields and a single uploaded file. It is deliberately minimal, not a
general RFC implementation.

    fields, files = parse_multipart(content_type_header, raw_body)
    # fields: {name: str}     files: {name: (filename, bytes)}
"""
from __future__ import annotations


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    boundary = _boundary(content_type)
    if not boundary:
        return {}, {}
    sep = b"--" + boundary.encode("latin-1")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in body.split(sep):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if not _:
            continue
        headers = head.decode("latin-1", "replace")
        name = _param(headers, "name")
        if name is None:
            continue
        filename = _param(headers, "filename")
        if filename is not None:
            files[name] = (filename, data)
        else:
            fields[name] = data.decode("utf-8", "replace").strip()
    return fields, files


def _boundary(content_type: str) -> str:
    for token in content_type.split(";"):
        token = token.strip()
        if token.lower().startswith("boundary="):
            return token.split("=", 1)[1].strip().strip('"')
    return ""


def _param(headers: str, key: str) -> str | None:
    marker = f'{key}="'
    i = headers.find(marker)
    if i < 0:
        return None
    i += len(marker)
    j = headers.find('"', i)
    return headers[i:j] if j > i - 1 else None
