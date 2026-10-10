"""Run the real runner against FakeEnvironment in its own process (kill tests)."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from controller.executor import runner  # noqa: E402
from controller.schemas import Plan  # noqa: E402
from controller.store import RunStore  # noqa: E402
from tests.fake_env import FakeEnvironment, fast_limits  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--db", type=Path, required=True)
parser.add_argument("--plan", type=Path, required=True)
args = parser.parse_args()
result = runner.execute(Plan.model_validate_json(args.plan.read_text()), live=True,
                        env=FakeEnvironment.load(args.config), store=RunStore(args.db),
                        limits=fast_limits(), gate_check=lambda plan, cap: [])
print(json.dumps(result, default=str))
