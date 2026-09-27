from __future__ import annotations

START = "--- habit stats ---"
END = "--- end habit stats ---"


def upsert_block(description: str, body: str) -> str:
    """Replace the text between the markers, or append a block. Everything outside the markers is preserved byte-for-byte."""
    block = f"{START}\n{body}\n{END}"
    start = description.find(START)
    end = description.find(END, start + len(START)) if start != -1 else -1
    if start != -1 and end != -1:
        return description[:start] + block + description[end + len(END):]
    if start != -1:  # dangling start marker: treat the rest as ours
        return description[:start] + block
    if not description.strip():
        return block
    return description.rstrip("\n") + "\n\n" + block


def strip_block(description: str) -> str:
    start = description.find(START)
    end = description.find(END, start + len(START)) if start != -1 else -1
    if start == -1:
        return description
    tail = description[end + len(END):] if end != -1 else ""
    return (description[:start].rstrip("\n") + tail).strip("\n")
