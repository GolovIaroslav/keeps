from keeps.compare import comparison_payload, diff_command, write_comparison_pair


def test_comparison_payload_uses_a_shared_textual_format():
    left = {"text/plain": b"left", "text/html": b"<b>left</b>"}
    right = {"text/plain": b"right"}

    assert comparison_payload(left, right) == (".txt", b"left", b"right")


def test_comparison_payload_returns_none_without_a_shared_text_format():
    assert comparison_payload({"image/png": b"left"}, {"text/plain": b"right"}) is None


def test_diff_command_prefers_configured_command_then_known_tools():
    assert diff_command("custom-diff --newtab", lambda _name: None) == [
        "custom-diff",
        "--newtab",
    ]
    assert diff_command("", lambda name: "/usr/bin/kdiff3" if name == "kdiff3" else None) == [
        "kdiff3"
    ]
    assert diff_command("", lambda _name: None) is None


def test_write_comparison_pair_creates_two_text_files(tmp_path):
    left_path, right_path = write_comparison_pair(
        tmp_path, ".html", b"<b>left</b>", b"<i>right</i>"
    )

    assert left_path.name == "left.html"
    assert right_path.name == "right.html"
    assert left_path.read_bytes() == b"<b>left</b>"
    assert right_path.read_bytes() == b"<i>right</i>"
