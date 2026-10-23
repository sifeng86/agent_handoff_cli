"""Execute the documented local tutorial with an installed CLI; no external APIs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def run(agentctl: Path) -> dict:
    agentctl = agentctl.absolute()
    if not agentctl.is_file():
        raise ValueError("An installed agentctl executable is required.")
    guide = ROOT / "docs/agent-operations.zh-TW.md"
    text = guide.read_text(encoding="utf-8")
    start, end = "<!-- tutorial-demo:start -->", "<!-- tutorial-demo:end -->"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("Tutorial demo markers must be unique.")
    demo = text.split(start, 1)[1].split(end, 1)[0]
    blocks = re.findall(r"^```bash\n(.*?)^```$", demo, re.MULTILINE | re.DOTALL)
    if not blocks:
        raise ValueError("The tutorial has no executable demo blocks.")
    with tempfile.TemporaryDirectory(prefix="agent-handoff-tutorial-") as temporary:
        root = Path(temporary)
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(PATH=str(agentctl.parent) + os.pathsep + os.environ.get("PATH", ""),
                   TUTORIAL_PARENT=str(root), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file", GIT_OPTIONAL_LOCKS="0")
        verification = r'''
python3 - "$LAB" "$AUTHORITY" "$PUBLICATION_REF" "$BASE" <<'VERIFY'
import json, subprocess, sys
from pathlib import Path
lab, authority, ref, base = sys.argv[1:]
def show(spec):
    return subprocess.check_output(["git", "--git-dir=" + authority, "show", spec], text=True)
assert subprocess.check_output(["git", "--git-dir=" + authority, "rev-parse", "refs/heads/main"], text=True).strip() == base
assert show("main:app.py") == 'print("Hello")\n'
assert show(ref + ":app.py") == 'print("Hello, handoff")\n'
assert json.loads(show(ref + ":.agent/owner.json"))["owner"] is None
state = json.loads(show(ref + ":.agent/state.json"))
assert (state["status"], state["active_plan"], state["current_step"]) == ("idle", None, None)
plan = show(ref + ":plans/P-001-demo.md")
metadata = json.loads(plan.split("<!-- agent-handoff-plan\n", 1)[1].split("\n-->", 1)[0])
assert metadata["status"] == "DONE"
assert (Path(lab) / "source/app.py").read_text() == 'print("Hello")\n'
VERIFY
'''
        program = "set -euo pipefail\n" + "\n".join(blocks) + verification
        completed = subprocess.run(["bash", "-c", program], cwd=root, env=env,
                                   capture_output=True, timeout=90)
        if completed.returncode:
            # The demo has no provider values; retain logs in a private directory.
            evidence = Path(tempfile.mkdtemp(prefix="agent-handoff-tutorial-failed-"))
            evidence.chmod(0o700)
            log = evidence / "output.log"
            log.write_bytes(completed.stdout + completed.stderr)
            log.chmod(0o600)
            raise RuntimeError("Tutorial failed; private diagnostic log: " + str(log))
        return {"passed": True, "bash_blocks": len(blocks),
                "guide_sha256": hashlib.sha256(guide.read_bytes()).hexdigest(),
                "scope": "Installed CLI; real local Git; two sessions; Plan/checkpoint/handoff/done; source/main preserved",
                "external_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentctl", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.agentctl)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"passed": False, "error": str(error)}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
