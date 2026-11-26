class TestTaintSinkCoverage(unittest.TestCase):
    def test_sink_in_assignment_is_detected(self) -> None:
        code = (
            "import subprocess\n"
            "def run(cmd):\n"
            "    out = subprocess.getoutput(cmd)\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertTrue(
            any("污点" in s and "`cmd`" in s for s in report.dangerous_nodes),
            f"赋值语句里的污点汇点被漏报: {report.dangerous_nodes}",
        )

    def test_sink_in_return_is_detected(self) -> None:
        code = (
            "import subprocess\n"
            "def run(code):\n"
            "    return subprocess.check_output(code, shell=True)\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertTrue(
            any("污点" in s and "`code`" in s for s in report.dangerous_nodes),
            f"return 语句里的污点汇点被漏报: {report.dangerous_nodes}",
        )


class TestTaintScopeIsolation(unittest.TestCase):
    def test_same_name_params_do_not_cross_talk(self) -> None:
        code = (
            "import os\n"
            "def reader(path):\n"
            "    data = open(path).read()\n"
            "    return data\n"
            "def processor(data):\n"
            "    os.system(data)\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertFalse(
            any("`path`" in s for s in report.dangerous_nodes),
            f"reader 的 path 参数被跨函数串扰误报为污点: {report.dangerous_nodes}",
        )
        # 反向守卫：processor 的 data 是真阳性，必须仍然被指认
        self.assertTrue(
            any("`data`" in s for s in report.dangerous_nodes),
            f"processor 的 data 污点必须仍被检出: {report.dangerous_nodes}",
        )


class TestTaintGateFailsClosed(unittest.TestCase):
    def test_internal_error_raises(self) -> None:
        with patch.object(CpgBuilder, "build", side_effect=RuntimeError("boom")):
            with self.assertRaises(Exception):
                audit_ast_diff("", "def f():\n    pass\n", "t.py")


class TestGateBlocksWhenAuditErrors(unittest.IsolatedAsyncioTestCase):
    # setUp 构建真实 git 仓库（两个 commit），run 置于 VERIFYING
    async def test_audit_error_escalates_instead_of_passing(self) -> None:
        # ... build_dev_workflow 找到 publish_mr_node ...
        with patch(
            "adk_dev_flow.workflow.audit_ast_diff",
            side_effect=RuntimeError("audit exploded"),
        ):
            res = await publish_node({"worktree": str(self.repo), "attempt": 0})
        self.assertEqual(res.get("status"), "ESCALATED")
        self.assertEqual(self.store.get_run(run_id)["status"], "ESCALATED")