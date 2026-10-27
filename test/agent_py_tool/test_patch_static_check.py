#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补丁静态检查（头文件声明、未声明符号、结构问题）的单测。"""

import tempfile
import unittest
from pathlib import Path

from services.patch_static_check import run_patch_static_check
from services.repair_actions import RepairActionDeps
from services.repair_pipeline import _run_patch_static_gate

UTILS_H = """#import <Foundation/Foundation.h>

@interface BMKFileUtils : NSObject

+ (NSArray *)bmk_files;

@end
"""

UTILS_M = """#import "BMKFileUtils.h"

@implementation BMKFileUtils

+ (NSArray *)bmk_files {
    return @[];
}

+ (NSString *)bmk_crashDirectory {
    return @"/tmp";
}

+ (BOOL)bmk_save:(NSString *)info type:(NSInteger)type {
    return YES;
}

@end
"""

HANDLER_ORIGINAL = """#import "BMKFileUtils.h"
#include <signal.h>

#define BMK_SIG_MAX 32

static int bmk_g_count = 0;

void bmk_signal_register(void) {
    bmk_g_count++;
}

void bmk_signal_reset(void) {
    bmk_g_count = 0;
}
"""


class PatchStaticCheckTest(unittest.TestCase):
    """覆盖各条静态检查规则与头文件自动补声明。"""

    def setUp(self):
        """准备一个最小的 BMK 工程目录。"""
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "repo"
        self.lib = self.root / "ios" / "Lib" / "Lib"
        self.lib.mkdir(parents=True)
        (self.lib / "BMKFileUtils.h").write_text(UTILS_H, encoding="utf-8")
        (self.lib / "BMKFileUtils.m").write_text(UTILS_M, encoding="utf-8")
        self.report_dir = Path(self._tmp.name).resolve() / "report"
        self.backup = self.report_dir / "original_sources" / "BMKSignal.m"
        self.backup.parent.mkdir(parents=True)
        self.backup.write_text(HANDLER_ORIGINAL, encoding="utf-8")

    def tearDown(self):
        """清理临时目录。"""
        self._tmp.cleanup()

    def _check(self, patched: str, *, name: str = "BMKSignal.m", original: str = HANDLER_ORIGINAL):
        """写入补丁后的文件并执行静态检查。"""
        path = self.lib / name
        path.write_text(patched, encoding="utf-8")
        self.backup.write_text(original, encoding="utf-8")
        applied = {"success": True, "applied": [
            {"file": str(path), "status": "applied", "backup_path": str(self.backup)},
        ]}
        return run_patch_static_check(applied, code_roots=[str(self.root)], report_dir=self.report_dir), applied

    def _rules(self, report):
        """返回问题规则与符号对。"""
        return {(item["rule"], item["symbol"]) for item in report["issues"]}

    def test_clean_patch_with_system_symbols_passes(self):
        """只用系统符号、局部变量和已声明符号的补丁不应误报。"""
        patched = HANDLER_ORIGINAL.replace(
            "    bmk_g_count++;\n",
            "    struct sigaction sa;\n    memset(&sa, 0, sizeof(sa));\n"
            "    sigaction(SIGSEGV, &sa, NULL);\n    for (int i = 0; i < BMK_SIG_MAX; i++) { bmk_g_count += i; }\n",
        )
        report, _ = self._check(patched)
        self.assertEqual(report["status"], "passed", report["issues"])

    def test_missing_header_declaration_is_added_to_header(self):
        """调用只在 .m 实现的类方法时，自动把声明补进类头文件并记为已应用修改。"""
        patched = HANDLER_ORIGINAL.replace(
            "    bmk_g_count++;\n",
            "    NSString *dir = [BMKFileUtils bmk_crashDirectory];\n"
            "    [BMKFileUtils bmk_save:dir type:1];\n    bmk_g_count++;\n",
        )
        report, _ = self._check(patched)
        self.assertEqual(report["status"], "passed", report["issues"])
        header = (self.lib / "BMKFileUtils.h").read_text(encoding="utf-8")
        self.assertIn("+ (NSString *)bmk_crashDirectory;", header)
        self.assertIn("+ (BOOL)bmk_save:(NSString *)info type:(NSInteger)type;", header)
        self.assertLess(header.index("bmk_crashDirectory;"), header.index("@end"))
        fixes = report["auto_fixes"]
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["status"], "applied")
        self.assertEqual(Path(fixes[0]["backup_path"]).read_text(encoding="utf-8"), UTILS_H)
        again, _ = self._check(patched)
        self.assertEqual(again["auto_fixes"], [])

    def test_unimplemented_or_declared_methods_are_ignored(self):
        """头文件已声明或工程里没有实现（可能继承）的方法不处理。"""
        patched = HANDLER_ORIGINAL.replace(
            "    bmk_g_count++;\n",
            "    [BMKFileUtils bmk_files];\n    [BMKFileUtils alloc];\n    bmk_g_count++;\n",
        )
        report, _ = self._check(patched)
        self.assertEqual(report["status"], "passed", report["issues"])
        self.assertEqual(report["auto_fixes"], [])

    def test_undeclared_project_symbols_fail(self):
        """补丁引入但没有定义的工程前缀宏/变量会被拦截。"""
        patched = HANDLER_ORIGINAL.replace(
            "    bmk_g_count++;\n",
            "    if (bmk_g_ranges[0][0] > BMK_ALT_STACK_SIZE) { bmk_g_count++; }\n",
        )
        report, _ = self._check(patched)
        self.assertEqual(report["status"], "failed")
        rules = self._rules(report)
        self.assertIn(("undeclared_symbol", "bmk_g_ranges"), rules)
        self.assertIn(("undeclared_symbol", "BMK_ALT_STACK_SIZE"), rules)

    def test_declared_multi_dimensional_static_passes(self):
        """补丁一起带上定义（含二维数组和 attribute 函数）时不误报。"""
        patched = HANDLER_ORIGINAL.replace(
            "static int bmk_g_count = 0;\n",
            "static int bmk_g_count = 0;\nstatic unsigned long bmk_g_ranges[3][2];\n"
            "#define BMK_ALT_STACK_SIZE (64 * 1024)\n"
            "static int bmk_g_first = 0, *bmk_g_second = NULL;\n"
            "enum { BMK_STATE_IDLE, BMK_STATE_BUSY = 3 };\n"
            "__attribute__((noinline, used)) static void bmk_anchor(void) {}\n",
        ).replace(
            "    bmk_g_count++;\n",
            "    if (bmk_g_ranges[0][0] > BMK_ALT_STACK_SIZE) { bmk_g_count++; }\n"
            "    bmk_g_first = BMK_STATE_BUSY + (bmk_g_second ? 1 : BMK_STATE_IDLE);\n"
            "    void *p = (void *)&bmk_anchor;\n    (void)p;\n",
        )
        report, _ = self._check(patched)
        self.assertEqual(report["status"], "passed", report["issues"])

    def test_function_used_before_definition_gets_forward_declaration(self):
        """在定义之前取地址的函数，自动在使用它的函数（及其注释）之前插入前置声明。"""
        patched = HANDLER_ORIGINAL.replace(
            "void bmk_signal_register(void) {\n    bmk_g_count++;\n",
            "/// 注册\nvoid bmk_signal_register(void) {\n    void *p = (void *)&bmk_image_anchor;\n    (void)p;\n",
        ) + "\n__attribute__((noinline, used)) static void bmk_image_anchor(void) {}\n"
        report, _ = self._check(patched)
        self.assertEqual(report["status"], "passed", report["issues"])
        fixed = (self.lib / "BMKSignal.m").read_text(encoding="utf-8")
        prototype = "__attribute__((noinline, used)) static void bmk_image_anchor(void);"
        self.assertIn(prototype + "\n\n/// 注册\nvoid bmk_signal_register(void) {", fixed)
        fix = report["auto_fixes"][0]
        self.assertEqual(fix["edit_type"], "forward_declaration")
        self.assertEqual(Path(fix["backup_path"]).read_text(encoding="utf-8"), HANDLER_ORIGINAL)
        again, _ = self._check(fixed)
        self.assertEqual(again["status"], "passed", again["issues"])
        self.assertEqual(again["auto_fixes"], [])

    def test_variable_used_before_declaration_fails(self):
        """在声明之前使用的静态变量无法自动修复，直接拦截。"""
        patched = HANDLER_ORIGINAL.replace(
            "    bmk_g_count++;\n", "    bmk_g_late = 1;\n",
        ) + "\nstatic int bmk_g_late = 0;\n"
        report, _ = self._check(patched)
        self.assertIn(("used_before_declaration", "bmk_g_late"), self._rules(report))

    def test_duplicate_and_nested_definitions_fail(self):
        """重复定义和函数体内嵌套定义会被拦截。"""
        patched = HANDLER_ORIGINAL + "\nvoid bmk_signal_reset(void) {\n    bmk_g_count = 1;\n}\n"
        report, _ = self._check(patched)
        self.assertIn(("duplicate_definition", "bmk_signal_reset"), self._rules(report))
        nested = HANDLER_ORIGINAL.replace(
            "    bmk_g_count = 0;\n",
            "    bmk_g_count = 0;\nstatic void bmk_inner(void) {\n    bmk_g_count = 2;\n}\n",
        )
        report, _ = self._check(nested)
        self.assertIn(("nested_definition", "bmk_inner"), self._rules(report))

    def test_unbalanced_braces_fail(self):
        """花括号不配对会被拦截，原本就不配对的文件不重复报。"""
        report, _ = self._check(HANDLER_ORIGINAL + "\nvoid bmk_tail(void) {\n")
        self.assertIn(("brace_balance", ""), self._rules(report))

    def test_static_conversion_with_external_users_fails(self):
        """函数改成 static 但其他文件仍调用时拦截。"""
        (self.lib / "BMKOther.m").write_text(
            "void bmk_signal_reset(void);\nvoid f(void) { bmk_signal_reset(); }\n", encoding="utf-8",
        )
        patched = HANDLER_ORIGINAL.replace("void bmk_signal_reset(void) {", "static void bmk_signal_reset(void) {")
        report, _ = self._check(patched)
        self.assertIn(("static_visibility_conflict", "bmk_signal_reset"), self._rules(report))

    def test_new_unsafe_call_in_signal_handler_fails(self):
        """信号处理函数里新增 malloc / ObjC 调用会被拦截；原有调用不重复报。"""
        original = (
            '#import "BMKFileUtils.h"\n#include <signal.h>\n\n'
            "void bmk_signal_exception_handler(int signal) {\n"
            "    NSLog(@\"old\");\n"
            "}\n"
            "void bmk_helper(void) {\n"
            "    malloc(8);\n"
            "}\n"
        )
        patched = original.replace(
            "    NSLog(@\"old\");\n",
            "    NSLog(@\"old\");\n    void *p = malloc(16);\n    (void)p;\n    [NSThread callStackSymbols];\n",
        )
        report, _ = self._check(patched, original=original)
        rules = self._rules(report)
        self.assertIn(("signal_unsafe_call", "bmk_signal_exception_handler"), rules)
        symbols = " ".join(item.get("message", "") for item in report["issues"])
        self.assertIn("malloc", symbols)
        self.assertTrue("NSThread" in symbols or "callStackSymbols" in symbols or "[" in symbols)
        keep_old, _ = self._check(
            original.replace(
                "    NSLog(@\"old\");\n",
                "    NSLog(@\"old\");\n    write(1, \"x\", 1);\n",
            ),
            original=original,
        )
        self.assertNotIn(
            ("signal_unsafe_call", "bmk_signal_exception_handler"),
            self._rules(keep_old),
            keep_old,
        )
        register = (
            original
            + "\nvoid bmk_register_signal_handler(void) {\n    [BMKFileUtils bmk_files];\n    malloc(8);\n}\n"
        )
        setup_report, _ = self._check(register, original=original)
        self.assertNotIn(
            ("signal_unsafe_call", "bmk_register_signal_handler"),
            self._rules(setup_report),
            setup_report,
        )

    def test_objc_file_rejects_new_c_bool(self):
        """ObjC 实现文件新增 bool/true/false 会被拦截。"""
        patched = HANDLER_ORIGINAL.replace(
            "    bmk_g_count++;\n",
            "    bool ok = true;\n    if (ok) { bmk_g_count++; }\n",
        )
        report, _ = self._check(patched)
        rules = self._rules(report)
        self.assertIn(("objc_bool_style", "bool"), rules)
        self.assertIn(("objc_bool_style", "true"), rules)

    def test_objc_bool_and_c_bool_are_allowed_in_their_files(self):
        """ObjC 用 BOOL 可通过；.c 和本来就写 bool 的 .m 不拦。"""
        patched = HANDLER_ORIGINAL.replace(
            "    bmk_g_count++;\n",
            "    BOOL ok = YES;\n    if (ok) { bmk_g_count++; }\n",
        )
        report, _ = self._check(patched)
        self.assertNotIn("objc_bool_style", {rule for rule, _symbol in self._rules(report)})
        c_original = "int f(void) { return 1; }\n"
        c_report, _ = self._check(
            "int f(void) { bool ok = true; return ok ? 1 : 0; }\n",
            name="util.c",
            original=c_original,
        )
        self.assertNotIn("objc_bool_style", {rule for rule, _symbol in self._rules(c_report)})
        objc_c_style = (
            '#import "BMKFileUtils.h"\n\nstatic bool bmk_g_ready = true;\n\n'
            "void bmk_signal_register(void) {\n    bool later = false;\n    (void)later;\n}\n"
        )
        keep, _ = self._check(objc_c_style, original=objc_c_style.replace("    bool later = false;\n", ""))
        self.assertNotIn("objc_bool_style", {rule for rule, _symbol in self._rules(keep)})

    def test_non_c_files_are_skipped(self):
        """非 C/ObjC 文件不做检查。"""
        path = self.lib / "Note.swift"
        path.write_text("let x = 1\n", encoding="utf-8")
        report = run_patch_static_check(
            {"applied": [{"file": str(path), "status": "applied"}]}, code_roots=[str(self.root)],
        )
        self.assertEqual(report["status"], "skipped")

    def test_pipeline_gate_declares_header_as_changed_file(self):
        """门禁自动补头文件后，头文件进入 changed_files 与 applied 记录。"""
        patched = HANDLER_ORIGINAL.replace(
            "    bmk_g_count++;\n", "    [BMKFileUtils bmk_crashDirectory];\n    bmk_g_count++;\n",
        )
        _, applied = self._check(patched)
        (self.lib / "BMKFileUtils.h").write_text(UTILS_H, encoding="utf-8")
        deps = RepairActionDeps(
            result={}, code_roots=[str(self.root)], report_dir=self.report_dir, run_id="t",
            request_record={"enable_repair_edit_loop": False}, applied_fix_result=applied,
            changed_files=[applied["applied"][0]["file"]],
        )
        context = {"changed_files": list(deps.changed_files)}
        report = _run_patch_static_gate(
            deps=deps, action_context=context, code_roots=[str(self.root)], report_dir=self.report_dir,
        )
        self.assertEqual(report["status"], "passed")
        header = str(self.lib / "BMKFileUtils.h")
        self.assertIn(header, deps.changed_files)
        self.assertIn(header, context["changed_files"])
        self.assertTrue((self.report_dir / "08_patch_static_check.json").is_file())

    def test_pipeline_gate_reports_failure_without_edit_loop(self):
        """关闭修订轮时，静态问题直接以失败返回。"""
        patched = HANDLER_ORIGINAL.replace("    bmk_g_count++;\n", "    bmk_g_missing++;\n")
        _, applied = self._check(patched)
        deps = RepairActionDeps(
            result={}, code_roots=[str(self.root)], report_dir=self.report_dir, run_id="t",
            request_record={"enable_repair_edit_loop": False}, applied_fix_result=applied,
        )
        report = _run_patch_static_gate(
            deps=deps, action_context={}, code_roots=[str(self.root)], report_dir=self.report_dir,
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["edit_rounds"], [])


if __name__ == "__main__":
    unittest.main()
