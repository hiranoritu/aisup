"""実サンドボックス（Docker）の検証。イメージがない環境では skip（理由を表示）。

    docker build -t aisup-sandbox:1 sandbox/
"""
import json

import pytest

from server.llm import FuncLLM
from server.prompts import load_prompt
from server.sandbox import DockerSandbox, verify_program
from server.settings import abspath, make_server_config
from server.support import SupportGenerator
from server.tasks import load_tasks

SB = DockerSandbox()
pytestmark = pytest.mark.skipif(not SB.available(), reason="Docker か sandbox イメージ（aisup-sandbox:1）がない")


def run1(src, stdin=""):
    out = SB.run(src, [stdin])
    assert out.ok, out.error
    return out


def test_compile_and_run_with_stdin():
    out = run1('#include <stdio.h>\nint main(void){int a; if(scanf("%d",&a)!=1) return 1; printf("%d\\n", a*2); return 0;}', "21")
    assert out.compile["ok"] and out.runs[0]["stdout"] == "42\n" and out.runs[0]["exit_code"] == 0


def test_compile_error_and_warnings_reported():
    out = run1("int main(void){int x}")
    assert not out.compile["ok"] and "error" in out.compile["diagnostics"] and not out.runs
    out = run1('#include <stdio.h>\nint main(void){int x; printf("%d", x); return 0;}')
    assert out.compile["ok"] and "-Wuninitialized" in out.compile["diagnostics"]


def test_sanitizer_detects_out_of_bounds_and_null():
    assert run1("int main(void){int a[2]; volatile int i = 5; return a[i];}").runs[0]["sanitizer"]
    assert run1("int main(void){int *p = 0; return *p;}").runs[0]["exit_code"] != 0


def test_cpu_limit():
    r = run1("int main(void){for(;;);}").runs[0]
    assert r["timed_out"] and r["limit"] == "cpu"


def test_wall_limit_when_blocked():
    r = run1("#include <unistd.h>\nint main(void){sleep(10); return 0;}").runs[0]
    assert r["timed_out"] and r["limit"] == "wall"


def test_memory_limit():
    r = run1("#include <stdlib.h>\n#include <string.h>\nint main(void){char*p=malloc(1u<<30); if(!p) return 3; memset(p,1,1u<<30); return 0;}").runs[0]
    assert r["exit_code"] != 0


def test_output_limit():
    r = run1('#include <stdio.h>\nint main(void){for(;;) puts("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx");}').runs[0]
    assert r["truncated"] and len(r["stdout"]) <= SB.cfg.output_bytes


def test_no_network():
    src = r'''#include <stdio.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
int main(void){ int s = socket(AF_INET, SOCK_STREAM, 0); struct sockaddr_in a = {0};
  a.sin_family = AF_INET; a.sin_port = htons(53); inet_pton(AF_INET, "8.8.8.8", &a.sin_addr);
  printf("%d\n", connect(s, (struct sockaddr*)&a, sizeof a)); return 0; }'''
    assert run1(src).runs[0]["stdout"].strip() == "-1"


def test_filesystem_is_read_only_except_tmp():
    src = r'''#include <stdio.h>
int main(void){ FILE *f = fopen("/etc/x", "w"); FILE *g = fopen("/opt/runner.py", "a");
  printf("%d %d\n", f != NULL, g != NULL); return 0; }'''
    assert run1(src).runs[0]["stdout"].strip() == "0 0"


def test_process_limit_contains_fork_bomb():
    r = run1("#include <unistd.h>\nint main(void){for(;;) fork();}").runs[0]
    assert r["timed_out"] or r["exit_code"] != 0


# ---------------------------------------------------------------- §6 検証
def test_verify_accepts_deterministic_program_and_uses_actual_output():
    v = verify_program(SB, '#include <stdio.h>\nint main(void){int a=1,*p=&a; *p=5; printf("%d\\n",a); return 0;}', [""])
    assert v.ok and v.outputs == {"": "5\n"}


@pytest.mark.parametrize("src,reason", [
    ('#include <stdio.h>\nint main(void){int a; printf("%p\\n",(void*)&a); return 0;}', "prints_address"),
    ('#include <stdio.h>\n#include <stdint.h>\nint main(void){int a; printf("%d\\n",(int)((uintptr_t)&a % 997)); return 0;}', "nondeterministic_output"),
    ('#include <stdio.h>\nint main(void){int a[2]; volatile int i=3; printf("%d\\n", a[i]); return 0;}', "sanitizer_error"),
    ("int main(void){for(;;);}", "timeout"),
    ("int main(void){return 1;}", "nonzero_exit"),
    ("int main(void){ return }", "compile_failed"),
])
def test_verify_rejects(src, reason):
    v = verify_program(SB, src, [""])
    assert not v.ok and v.reason == reason


# ---------------------------------------------------------------- 課題メタデータ
def test_task_reference_solutions_verify():
    cfg = make_server_config()
    for t in load_tasks(abspath(cfg["tasks_dir"]), cfg["task_sequence"]):
        v = verify_program(SB, t.reference_solution, t.sample_inputs)
        assert v.ok, (t.task_id, v.reason)
        assert run1(t.starter_code).compile["ok"], t.task_id


# ---------------------------------------------------------------- 生成→検証（実サンドボックス）
def _gen(responses):
    it = iter(responses)
    cfg = make_server_config({"llm_provider": "fake"})
    prompts = {k: load_prompt(abspath(f"prompts/support/{k}.md")) for k in ("predict_verify", "apply_variation")}
    return SupportGenerator(FuncLLM(lambda role, msgs: next(it)), prompts, SB, cfg)


def test_predict_regenerates_once_then_uses_execution_result():
    cfg = make_server_config()
    task = load_tasks(abspath(cfg["tasks_dir"]), cfg["task_sequence"])[1]
    bad = json.dumps({"program": '#include <stdio.h>\nint main(void){int a; printf("%p\\n",(void*)&a);return 0;}',
                      "stdin": "", "question": "?", "distractors": ["1", "2", "3"]})
    good = json.dumps({"program": task.reference_solution, "stdin": "3 5\n", "question": "出力は？",
                       "expected_output": "3 5", "distractors": ["3 5", "5 3", "0 0", "8"]})
    g = _gen([bad, good]).generate("predict_verify", "o9", task, "r1", "```c\n" + task.reference_solution + "```")
    assert g.ok and [a.reason for a in g.attempts] == ["prints_address", ""]
    assert g.key["actual_output"] == "5 3\n"
    assert g.public["choices"][g.key["answer_index"]] == "5 3"
    assert sorted(g.public["choices"]) == sorted(["5 3", "3 5", "0 0", "8"])


def test_apply_variation_expected_from_reference_run():
    cfg = make_server_config()
    task = load_tasks(abspath(cfg["tasks_dir"]), cfg["task_sequence"])[1]
    obj = json.dumps({"task": "t", "starter_code": task.starter_code, "reference_solution": task.reference_solution,
                      "tests": ["1 2\n", "7 -7\n"], "hint": "h"})
    g = _gen([obj]).generate("apply_variation", "o1", task, "r1", "```c\n" + task.reference_solution + "```")
    assert g.ok and g.key["expected"] == {"1 2\n": "2 1\n", "7 -7\n": "-7 7\n"}


def test_material_code_examples_run():
    """教材の完全なプログラム（int main を含むコード）がコンパイル・実行できる。"""
    import glob
    import re
    n = 0
    for path in glob.glob(abspath("materials/*.md")):
        for code in re.findall(r"```c\n(.*?)```", open(path, encoding="utf-8").read(), re.S):
            if "int main" in code:
                v = verify_program(SB, code, [""])
                assert v.ok, (path, v.reason)
                n += 1
    assert n >= 2
