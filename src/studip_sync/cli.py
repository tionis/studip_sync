import argparse

from .errors import StudipSyncError
from .studip import StudipSync


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize course documents from Stud.IP"
    )
    parser.add_argument("--auth-method", help="authentication method", default="cookie")
    parser.add_argument(
        "--browser",
        choices=("selenium", "firefox", "chrome", "brave"),
        help="browser used for login or cookie extraction",
        default="firefox",
    )
    parser.add_argument(
        "--data-path", "-d", help="local synchronization directory", default="."
    )
    parser.add_argument(
        "--use-git", help="commit and push synchronized files", action="store_true"
    )
    parser.add_argument(
        "--git-commit-message-prefix",
        "--git-commit-message",
        help="prefix for generated git commit messages",
        default="studip-sync: ",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync", help="download new course documents")
    select_parser = subparsers.add_parser(
        "select-semester", help="choose the semester to synchronize"
    )
    select_parser.add_argument("semester", nargs="?")
    subparsers.add_parser("get-cookie", help="print the current session cookie")
    return parser


def app() -> int:
    parser = create_parser()
    args = parser.parse_args()
    try:
        client = StudipSync(vars(args))
        if args.command == "sync":
            client.sync()
        elif args.command == "select-semester":
            client.select_semester(args.semester)
        elif args.command == "get-cookie":
            print(client.get_cookie())
    except StudipSyncError as error:
        parser.exit(1, f"error: {error}\n")
    return 0
