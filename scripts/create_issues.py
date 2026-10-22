"""Create one GitHub issue per milestone in docs/BUILD_PLAN.md.

Default is a dry run. Pass --create to run `gh issue create` (needs `gh auth login`).
"""

import re
import subprocess
import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent / "docs" / "BUILD_PLAN.md"
ROW = re.compile(r"^\|\s*(M\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")


def milestones():
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            yield m.groups()


def main() -> None:
    create = "--create" in sys.argv
    for code, title, done in milestones():
        body = (
            f"Milestone {code} from `docs/BUILD_PLAN.md`.\n\n**Done when:** {done}\n\n"
            "Follow the rules in `AGENTS.md`. Add tests; do not call live services in tests."
        )
        print(f"{code}: {title}")
        if create:
            subprocess.run(
                ["gh", "issue", "create", "--title", f"{code}: {title}", "--body", body],
                check=True,
            )


if __name__ == "__main__":
    main()
