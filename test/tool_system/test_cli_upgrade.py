#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest
from unittest import mock

from cli.upgrade import (
    DIST_NAME,
    InstallContext,
    PackageVersionInfo,
    _parse_pip_index_versions,
    _version_less_than,
    collect_version_report,
    detect_install_context,
    preferred_upgrade_spec,
    query_latest_version,
    run_upgrade,
)


class TestCliUpgrade(unittest.TestCase):
    def test_parse_pip_index_versions(self):
        stdout = (
            "stability-analysis-agent (1.2.7)\n"
            "Available versions: 1.2.7, 1.2.6, 1.2.5\n"
            "  INSTALLED: 1.2.6\n"
            "  LATEST:    1.2.7\n"
        )
        latest, versions = _parse_pip_index_versions(stdout, DIST_NAME)
        self.assertEqual(latest, "1.2.7")
        self.assertEqual(versions[:2], ["1.2.7", "1.2.6"])

    def test_version_less_than(self):
        self.assertTrue(_version_less_than("1.2.6", "1.2.7"))
        self.assertFalse(_version_less_than("1.2.7", "1.2.7"))

    def test_package_version_info_update_available(self):
        info = PackageVersionInfo(dist_name=DIST_NAME, installed="1.2.6", latest="1.2.7")
        self.assertTrue(info.update_available)

    def test_detect_install_context_editable(self):
        completed = mock.Mock(returncode=0, stdout="Editable project location: /tmp/repo\n")
        with mock.patch("cli.upgrade.subprocess.run", return_value=completed):
            ctx = detect_install_context()
        self.assertEqual(ctx.mode, "editable")
        self.assertEqual(ctx.detail, "/tmp/repo")

    def test_detect_install_context_binary(self):
        with mock.patch("cli.upgrade.sys.frozen", True, create=True):
            with mock.patch("cli.upgrade.sys.executable", "/tmp/sa-agent"):
                ctx = detect_install_context()
        self.assertEqual(ctx.mode, "binary")

    def test_detect_install_context_pipx(self):
        completed = mock.Mock(returncode=0, stdout="Name: stability-analysis-agent\n")
        with mock.patch("cli.upgrade.subprocess.run", return_value=completed):
            with mock.patch(
                "cli.upgrade.sys.executable",
                "/Users/me/.local/pipx/venvs/stability-analysis-agent/bin/python",
            ):
                ctx = detect_install_context()
        self.assertEqual(ctx.mode, "pipx")

    def test_query_latest_version_success(self):
        completed = mock.Mock(
            returncode=0,
            stdout="stability-analysis-agent (1.2.8)\nAvailable versions: 1.2.8, 1.2.7\n",
            stderr="",
        )
        with mock.patch("cli.upgrade.subprocess.run", return_value=completed):
            latest, err = query_latest_version(DIST_NAME, index_url="https://pypi.org/simple/")
        self.assertEqual(latest, "1.2.8")
        self.assertEqual(err, "")

    def test_preferred_upgrade_spec_with_rag(self):
        with mock.patch("cli.upgrade.is_rag_runtime_available", return_value=True):
            self.assertEqual(preferred_upgrade_spec(), f"{DIST_NAME}[rag]")

    def test_run_upgrade_editable_blocked(self):
        ok, err = run_upgrade(InstallContext(mode="editable", detail="/tmp/repo"))
        self.assertFalse(ok)
        self.assertIn("开发模式", err)

    def test_collect_version_report(self):
        with mock.patch("cli.upgrade.get_installed_version", return_value="1.2.6"):
            with mock.patch("cli.upgrade.query_latest_version", return_value=("1.2.7", "")):
                with mock.patch(
                    "cli.upgrade.detect_install_context",
                    return_value=InstallContext(mode="pip"),
                ):
                    with mock.patch("cli.upgrade.preferred_upgrade_spec", return_value=DIST_NAME):
                        pkg, ctx, spec = collect_version_report()
        self.assertTrue(pkg.update_available)
        self.assertEqual(ctx.mode, "pip")
        self.assertEqual(spec, DIST_NAME)


if __name__ == "__main__":
    unittest.main()
