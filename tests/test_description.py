from habits.description import END, START, strip_block, upsert_block


def test_appends_block_to_user_text():
    out = upsert_block("My notes\n- keep this", "line1\nline2")
    assert out == f"My notes\n- keep this\n\n{START}\nline1\nline2\n{END}"


def test_replaces_only_between_markers_preserving_surrounding_text():
    desc = f"before\n{START}\nold\n{END}\nafter (user wrote this later)"
    out = upsert_block(desc, "new")
    assert out == f"before\n{START}\nnew\n{END}\nafter (user wrote this later)"
    assert upsert_block(out, "new") == out  # idempotent


def test_empty_description_gets_only_block():
    assert upsert_block("", "x") == f"{START}\nx\n{END}"
    assert upsert_block("  \n", "x") == f"{START}\nx\n{END}"


def test_dangling_start_marker_is_repaired():
    assert upsert_block(f"keep\n{START}\ngarbage", "x") == f"keep\n{START}\nx\n{END}"


def test_strip_block_roundtrip():
    assert strip_block(upsert_block("user text", "x")) == "user text"
    assert strip_block("no block") == "no block"
