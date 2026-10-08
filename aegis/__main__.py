import argparse
import asyncio
import json


def main():
    parser = argparse.ArgumentParser(description="Aegis Agent Harness")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the local control room")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    evaluate = commands.add_parser("eval", help="Run deterministic trajectory evaluations")
    evaluate.add_argument("--output", default="artifacts/eval-report.json")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("aegis.server:create_app", factory=True, host=args.host, port=args.port)
    else:
        from .evals import evaluate

        report = asyncio.run(evaluate(args.output))
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
