#!/usr/bin/env python3
"""No shipped Loop Deck program runs a command through a shell.

Up to Loop Deck 1.7.0 the generator's sh() helper ran command strings with subprocess.run(shell=True),
so the checkout path inside its `git -C <path> show origin/master:STATE.md` read was parsed by /bin/sh:
a space split it and a `$(...)` in it ran. Since 1.7.1 sh() takes an argument list. These checks fail
if a shell comes back:
  * static: no call in a shipped program passes shell= with anything but a literal False, and none calls
    os.system, os.popen, subprocess.getoutput or subprocess.getstatusoutput, or starts sh/bash with -c;
  * the generator's sh() gets a list or tuple display at every call site, never a string;
  * behaviour: sh() hands each argument to the program verbatim, and returns "" when the program cannot run;
  * the static check can fail: each planted shell use is reported.
Standard library only, no network; the only process it starts is this interpreter.
"""
import ast
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "generate-loop-viz.py"
SERVER = ROOT / "deck-server.py"
CHECKER = ROOT / "tools/check-contract.py"
PROGRAMS = (GENERATOR, SERVER, CHECKER)
SHELL_FUNCS = {("os", "system"), ("os", "popen"), ("subprocess", "getoutput"), ("subprocess", "getstatusoutput")}
SHELL_NAMES = {"sh", "bash", "dash", "zsh", "ksh"}


def shell_uses(source, name):
    """Every way the source starts a shell: shell= other than a literal False, a shell-running function,
    or a [<shell>, "-c", ...] argument list."""
    found = []
    for node in ast.walk(ast.parse(source, filename=name)):
        if isinstance(node, ast.ImportFrom) and node.module in ("os", "subprocess"):
            found += [f"{name}:{node.lineno}: from {node.module} import {a.name}" for a in node.names
                      if (node.module, a.name) in SHELL_FUNCS]
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                found.append(f"{name}:{node.lineno}: shell={ast.unparse(kw.value)}")
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and (f.value.id, f.attr) in SHELL_FUNCS:
            found.append(f"{name}:{node.lineno}: {f.value.id}.{f.attr}()")
        if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)) and len(node.args[0].elts) >= 2:
            first, second = node.args[0].elts[:2]
            if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                    and os.path.basename(first.value) in SHELL_NAMES
                    and isinstance(second, ast.Constant) and second.value == "-c"):
                found.append(f"{name}:{node.lineno}: {first.value} -c")
    return found


class NoShellInShippedPrograms(unittest.TestCase):
    def test_programs_exist(self):
        for p in PROGRAMS:
            self.assertTrue(p.is_file(), f"shipped program missing: {p}")

    def test_no_program_starts_a_shell(self):
        found = [hit for p in PROGRAMS for hit in shell_uses(p.read_text(encoding="utf-8"), p.name)]
        self.assertEqual(found, [], "a shipped program starts a shell")

    def test_sh_gets_an_argument_list_at_every_call_site(self):
        tree = ast.parse(GENERATOR.read_text(encoding="utf-8"), filename=GENERATOR.name)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "sh"]
        self.assertTrue(calls, "the generator no longer calls sh(): update this test")
        for c in calls:
            self.assertTrue(c.args and isinstance(c.args[0], (ast.List, ast.Tuple)),
                            f"{GENERATOR.name}:{c.lineno}: sh() needs an argument list, got {ast.unparse(c)}")

    def test_the_check_can_fail(self):
        plants = {
            "subprocess.run('crontab -l', shell=True)": "shell=True",
            "subprocess.run(cmd, shell=flag)": "shell=flag",
            "subprocess.Popen(cmd, shell=1)": "shell=1",
            "os.system('ls')": "os.system()",
            "os.popen('ls')": "os.popen()",
            "subprocess.getoutput('ls')": "subprocess.getoutput()",
            "subprocess.getstatusoutput('ls')": "subprocess.getstatusoutput()",
            "from os import system": "from os import system",
            "subprocess.run(['sh', '-c', cmd])": "sh -c",
            "subprocess.run(['/bin/bash', '-c', cmd])": "/bin/bash -c",
        }
        for code, expect in plants.items():
            with self.subTest(code=code):
                found = shell_uses(code + "\n", "plant.py")
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].endswith(expect), found)
        self.assertEqual(shell_uses("subprocess.run(['git', 'status'], shell=False)\n"
                                    "subprocess.run(['bash', gate, 'check-action'])\n", "clean.py"), [])


class ShHelper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("loop_viz_no_shell", GENERATOR)
        cls.gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gen)          # module level defines constants and functions only

    def test_each_argument_reaches_the_program_verbatim(self):
        word = "dir with space; echo $(echo X) `echo Y` | cat > out-file 2>/dev/null"
        out = self.gen.sh([sys.executable, "-I", "-c", "import sys; print(sys.argv[1])", word])
        self.assertEqual(out, word + "\n")

    def test_a_program_that_cannot_run_gives_empty_output(self):
        self.assertEqual(self.gen.sh([str(ROOT / "no-such-program")]), "")


if __name__ == "__main__":
    unittest.main()
