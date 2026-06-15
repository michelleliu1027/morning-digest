"""Start the monitor server (and optionally spawn a demo agent to watch).

    python -m morning_digest.monitor            # just start the dashboard server
    python -m morning_digest.monitor --demo     # also spawn a tiny demo agent
"""

import argparse

from .server import PORT, serve, spawn_agent


def main() -> None:
    parser = argparse.ArgumentParser(description="Local agent monitor dashboard.")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--demo", action="store_true", help="Spawn a small demo agent on startup.")
    args = parser.parse_args()

    if args.demo:
        spawn_agent(
            name="demo",
            prompt="List the Python files in the current directory using the Bash tool, then say how many there are.",
            cwd=".",
        )

    serve(port=args.port)


if __name__ == "__main__":
    main()
