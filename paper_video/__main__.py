import argparse
from pathlib import Path

from . import papers, store


def main():
    parser = argparse.ArgumentParser(description="Paper Segment Video local studio")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--paper", type=Path, help="Import an explicitly selected local PDF"
    )
    parser.add_argument("--import-only", action="store_true")
    args = parser.parse_args()
    store.init()
    if args.paper:
        result = papers.ingest(args.paper.read_bytes(), args.paper.name)
        print(f"Imported {result['page_count']} pages: {result['id']}", flush=True)
    if not args.import_only:
        import uvicorn

        uvicorn.run(
            "paper_video.server:app", host="127.0.0.1", port=args.port, log_level="info"
        )


if __name__ == "__main__":
    main()
