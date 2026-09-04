"""Tests for opt in read only mode."""

import asyncio
import subprocess
import sys

import pytest

from monarch_mcp_server import read_only


class TestIsReadOnly:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " y "])
    def test_truthy_values_enable_it(self, monkeypatch, value):
        monkeypatch.setenv(read_only.ENV_VAR, value)
        assert read_only.is_read_only() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_everything_else_leaves_it_off(self, monkeypatch, value):
        monkeypatch.setenv(read_only.ENV_VAR, value)
        assert read_only.is_read_only() is False

    def test_unset_leaves_it_off(self, monkeypatch):
        """Off by default, so existing setups are unaffected."""
        monkeypatch.delenv(read_only.ENV_VAR, raising=False)
        assert read_only.is_read_only() is False


class TestRegistration:
    """Registration is skipped, not just guarded at call time.

    A tool that is never registered cannot be invoked at all, which is
    stronger than a client side approval prompt: the threat in the README's
    approval section is a model influenced by a memo or merchant name it read
    back, and that model cannot call what it cannot see.
    """

    def test_mutating_tools_are_not_registered(self, monkeypatch):
        registered = []

        class FakeMCP:
            def tool(self, *args, **kwargs):
                def decorator(fn):
                    registered.append(fn.__name__)
                    return fn

                return decorator

        monkeypatch.setenv(read_only.ENV_VAR, "1")
        fake = FakeMCP()
        read_only.install(fake)

        def delete_transaction():
            pass

        def get_accounts():
            pass

        assert fake.tool()(delete_transaction) is delete_transaction
        fake.tool()(get_accounts)

        assert "delete_transaction" not in registered
        assert "get_accounts" in registered

    def test_nothing_is_skipped_when_disabled(self, monkeypatch):
        registered = []

        class FakeMCP:
            def tool(self, *args, **kwargs):
                def decorator(fn):
                    registered.append(fn.__name__)
                    return fn

                return decorator

        monkeypatch.delenv(read_only.ENV_VAR, raising=False)
        fake = FakeMCP()
        read_only.install(fake)

        def delete_transaction():
            pass

        fake.tool()(delete_transaction)
        assert registered == ["delete_transaction"]


class TestMutatingToolList:
    async def test_every_named_tool_actually_exists(self):
        """A typo here would silently leave a mutating tool exposed."""
        from monarch_mcp_server.app import mcp

        registered = {t.name for t in await mcp.list_tools()}
        # Only meaningful when read only is off, which is the default in tests.
        if not read_only.is_read_only():
            unknown = read_only.MUTATING_TOOLS - registered
            assert not unknown, f"MUTATING_TOOLS names no such tools: {sorted(unknown)}"

    async def test_it_matches_the_readme_approval_list(self):
        """The two lists answer the same question and must not diverge."""
        from pathlib import Path

        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
        section = readme[readme.index("### Recommended: require approval") :]
        missing = {n for n in read_only.MUTATING_TOOLS if f"`{n}`" not in section}
        assert not missing, f"in MUTATING_TOOLS but not the README list: {sorted(missing)}"
