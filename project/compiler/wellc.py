"""wellc CLI entry."""
import argparse
import json
import os
import sys

# allow running as `python compiler/wellc.py`
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from wellc.driver import build, run, emit_c, check_file, BuildError, CheckIOError  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wellc", description="Well language compiler (MVP)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="compile .we to an executable")
    b.add_argument("file")
    b.add_argument("-o", "--out", default=None)
    b.add_argument("--release", action="store_true",
                   help="use an optimizing C compiler (-O2) when available")
    b.add_argument("--release-lto", action="store_true",
                   help="release plus -flto (opt-in; WELL_RELEASE_LTO=1 also works)")

    r = sub.add_parser("run", help="compile and run")
    r.add_argument("file")
    r.add_argument("-o", "--out", default=None)
    r.add_argument("--release", action="store_true",
                   help="use an optimizing C compiler (-O2) when available")
    r.add_argument("--release-lto", action="store_true",
                   help="release plus -flto (opt-in; WELL_RELEASE_LTO=1 also works)")

    e = sub.add_parser("emit-c", help="emit generated C")
    e.add_argument("file")
    e.add_argument("-o", "--out", default=None)

    c = sub.add_parser("check", help="parse/type-check a .we file and report diagnostics")
    c.add_argument("file")
    c.add_argument("--json", action="store_true",
                   help="emit one machine-readable JSON object on stdout")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            cfile, exe = build(args.file, args.out, release=args.release,
                               release_lto=args.release_lto)
            print(f"generated: {cfile}")
            print(f"exe:       {exe}")
            return 0
        if args.cmd == "run":
            return run(args.file, args.out, release=args.release,
                       release_lto=args.release_lto)
        if args.cmd == "emit-c":
            code = emit_c(args.file, args.out)
            if not args.out:
                print(code)
            else:
                print(f"generated: {os.path.abspath(args.out)}")
            return 0
        if args.cmd == "check":
            # Argument/IO problems are exit code 2 and go to stderr so that
            # `--json` stdout always stays a single parseable object.
            try:
                result, code = check_file(args.file)
            except CheckIOError as ex:
                print(f"wellc: error: {ex}", file=sys.stderr)
                return 2
            except Exception as ex:  # noqa: BLE001 - --json must stay machine-readable
                result = {"ok": False, "file": os.path.abspath(args.file),
                          "diagnostics": [{
                              "file": os.path.abspath(args.file),
                              "line": 1, "col": 1, "endLine": 1, "endCol": 2,
                              "severity": "error",
                              "message": f"internal compiler error: {type(ex).__name__}: {ex}",
                              "code": "internal"}]}
                code = 1
            if args.json:
                # ensure_ascii keeps Windows console encodings (cp936/cp1252)
                # from mangling paths/messages; exactly one JSON object.
                print(json.dumps(result, ensure_ascii=True))
            else:
                for d in result["diagnostics"]:
                    print(f"{d['file']}:{d['line']}:{d['col']}: "
                          f"{d['severity']}: {d['message']}")
            return code
    except BuildError as ex:
        print(f"wellc: error: {ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
