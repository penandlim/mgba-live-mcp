from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "scripts" / "lua_templates"


def _read_template(filename: str) -> str:
    path = TEMPLATES_DIR / filename
    assert path.exists(), f"missing template: {path}"
    return path.read_text()


def test_expected_lua_templates_exist() -> None:
    expected = {
        "kamaitachi_fresh_start_to_help_info_page1.lua",
        "macro_async_with_macro_key.lua",
        "input_one_shot.lua",
        "memory_probe.lua",
    }
    for filename in expected:
        assert (TEMPLATES_DIR / filename).exists(), f"missing template file: {filename}"


def test_kamaitachi_template_has_macro_key_callbacks_and_cleanup() -> None:
    source = _read_template("kamaitachi_fresh_start_to_help_info_page1.lua")
    required_snippets = (
        "local macro_key = ",
        'callbacks:add("frame"',
        'callbacks:add("keysRead"',
        "callbacks:remove(state.frame_cb)",
        "callbacks:remove(state.keys_cb)",
        "_G[macro_key].active = false",
        "macro_key = macro_key",
    )
    for snippet in required_snippets:
        assert snippet in source, f"kamaitachi template is missing snippet: {snippet}"


def test_async_template_returns_macro_key() -> None:
    source = _read_template("macro_async_with_macro_key.lua")
    assert "local macro_key = " in source
    assert "macro_key = macro_key" in source
    assert 'status = "started"' in source
