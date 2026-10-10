"""Connected workbench and controller API, loopback only. No credentials needed."""
import argparse
from controller.api import ControllerAPI, serve
from controller.executor import runner
from controller.observer import endpoints
from controller.observer.telemetry import Telemetry, opensearch_log_search
from controller.safety import sandbox
from .bridge import WorkbenchBridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8095)
    args = parser.parse_args()
    api = ControllerAPI(telemetry_factory=lambda: Telemetry(endpoints.jaeger_api(), endpoints.metrics_url(),
                            log_search=opensearch_log_search(sandbox.load_spec()["compose_project"])),
                        log=print)
    api.workbench_bridge = WorkbenchBridge(api)
    server = serve(api, port=args.port)
    print(f'Connected workbench: http://127.0.0.1:{args.port}/; mutations remain subject to policy and sandbox gates.', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
