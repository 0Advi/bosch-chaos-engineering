"""The real watchdog entry point, with FakeEnvironment instead of Docker."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from controller import watchdog  # noqa: E402
from tests.fake_env import FakeEnvironment  # noqa: E402

argv = sys.argv[1:]
config = Path(argv[argv.index("--config") + 1])
del argv[argv.index("--config"):argv.index("--config") + 2]
sys.exit(watchdog.main(argv, env_factory=lambda: FakeEnvironment.load(config)))
