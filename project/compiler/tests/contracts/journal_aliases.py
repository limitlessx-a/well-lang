"""Journal binding-identity contract tests.

Ported from WellLangV3's ``tool_ai/check_journal_aliases.py`` (see
``document/v3借鉴与v2改进建议.md`` §1.2/§3-P0).

Each case builds a small Well program whose generated C is patched so that an
``Item``'s scalar field is poisoned with -999 immediately before its
destructor runs.  A program that still prints ``42`` (and leaves zero live
objects) kept every binding alive for as long as source-level aliases pointed
at it.  A program that prints ``-999`` proves a binding's journal edge was
confused with another binding's edge and the object was destroyed too early.

Run from the compiler directory:

    python tests/contracts/journal_aliases.py            # all cases
    python tests/contracts/journal_aliases.py parameter  # one case
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPILER = HERE.parent.parent
sys.path.insert(0, str(COMPILER))

from wellc.driver import (  # noqa: E402
    CORE_SOURCES, RUNTIME_DIR, cc_flags, compile_source, find_cc,
)

CASES = {
    # A callee reassigns a borrowed reference parameter; the caller still owns
    # its own binding and must keep seeing the original object.
    'parameter': '''
func replace(x:Item):int {x=new Item();return x.value;}
func work():int {var a:Item=new Item();a.value=42;var unused:int=replace(a);return a.value;}''',
    # A nested local aliases two outer variables, then is assigned another
    # object.  Address lookup used to rewrite the wrong binding's entry.
    'nested_alias': '''
func work():int {
var a:Item=new Item();a.value=42;var b:Item=a;
{var c:Item=a;c=new Item();}
a=new Item(); return b.value;
}''',
    # An outer variable starts null (no journal entry) and is assigned inside a
    # loop.  The new object must outlive the loop iteration scope.
    'outer_null': '''
func work():int {
var a:Item=null;
for(var i:int=0;i<2;i=i+1){a=new Item();a.value=42;}
return a.value;
}''',
    # Mutating the source container while iterating must not free the element
    # the loop variable still points at.
    'foreach_mutation': '''
func work():int {
var a:Array<Item> = [];a.push(new Item());a[0].value=42;
var result:int=0;
for(var x of a){a[0]=new Item();result=x.value;}
return result;
}''',
    # A borrowed parameter must survive a callee that replaces the static that
    # supplied it.
    'parameter_source_mutation': '''
static State {item:Item;}
func change(x:Item):int {State.item=new Item();return x.value;}
func work():int {State.item=new Item();State.item.value=42;
var result:int=change(State.item);State.item=null;return result;}''',
    # for-loop initializer shadowing an outer binding.
    'for_shadow': '''
func work():int {
var x:Item=new Item();x.value=42;
for(var x:Item=new Item();x.value<1;x.value=x.value+1){continue;}
x=x;return x.value;
}''',
    # null shadow in an inner block, assigned in a deeper block.
    'inner_null_shadow': '''
func work():int {
var x:Item=new Item();x.value=42;
{var x:Item=null;{x=new Item();}x.value=17;}
x=x;return x.value;
}''',
    # Early return from inside a loop with an alias.
    'early_alias_return': '''
func work():int {
var a:Item=new Item();a.value=42;var b:Item=a;
while(true){var c:Item=b;c=new Item();return b.value;}
return 0;
}''',
    # foreach loop variable shadowing an outer binding.
    'foreach_shadow': '''
func work():int {
var x:Item=new Item();x.value=42;var a:Array<Item> = [];a.push(new Item());
for(var x of a){x=new Item();}
x=x;return x.value;
}''',
    # for initializer must be released even when the loop breaks immediately.
    'for_init_cleanup': '''
func live():int{return 0;}
func work():int {for(var x:Item=new Item();true;){break;} return live()+42;}''',
    'namespace_parameter': '''
namespace Ops {replace(x:Item):int {x=new Item();return x.value;}}
func work():int {var a:Item=new Item();a.value=42;var unused:int=Ops.replace(a);return a.value;}''',
    'static_parameter': '''
static Ops {replace(x:Item):int {x=new Item();return x.value;}}
func work():int {var a:Item=new Item();a.value=42;var unused:int=Ops.replace(a);return a.value;}''',
    # Hot identity-forward path: binding every ref parameter must not change
    # semantics here (and must not regress the benchmark path).
    'identity_forward': '''
func identity(x:Item):Item {return x;}
func work():int {var a:Item=new Item();a.value=42;
for(var i:int=0;i<2000;i=i+1){a=identity(a);} return a.value;}''',
    # A later argument's evaluation mutates the static that supplied an
    # earlier borrowed argument.
    'later_argument_mutation': '''
static State {item:Item;}
func resetSource():int {State.item=new Item();return 0;}
func readFirst(x:Item,unused:int):int {return x.value;}
func work():int {State.item=new Item();State.item.value=42;
var result:int=readFirst(State.item,resetSource());State.item=null;return result;}''',
}

HEADER = '''module test.journal_alias;
struct Item {value:int;}
'''
FOOTER = '''
controller Main {run(){var result:int=work();print(result);}}
entry Main;
'''


def build_case(name: str, body: str, out_dir: Path) -> tuple[Path, str]:
    src = HEADER + body + FOOTER
    code = compile_source(src, f"journal_alias_{name}.we")
    head, sep, tail = code.partition('well_obj_free(o);')
    if not sep:
        raise RuntimeError(f"{name}: generated C has no well_obj_free(o) to poison")
    code = head + 'o->f_value = -999; ' + sep + tail
    # Report the final live-object count on stderr (the runner checks it).
    code = code.replace('well_print_nl();',
                        'well_print_nl(); fprintf(stderr,"STATE %lld\\n",well_live_count());')
    cpath = out_dir / f"{name}.c"
    cpath.write_text(code, encoding='utf-8')
    exe = cpath.with_suffix('.exe')
    cmd = [find_cc(), *cc_flags(), '-UNDEBUG',
           '-I', str(Path(RUNTIME_DIR) / 'core'), str(cpath),
           *[str(Path(RUNTIME_DIR) / s) for s in CORE_SOURCES], '-o', str(exe)]
    if os.name != 'nt':
        cmd.append('-pthread')
    subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=180)
    return exe, name


def run_case(name: str, body: str, out_dir: Path) -> bool:
    exe, _ = build_case(name, body, out_dir)
    p = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    got = p.stdout.strip()
    state = p.stderr.strip()
    ok = p.returncode == 0 and got == '42' and state == 'STATE 0'
    print(f"{'PASS' if ok else 'FAIL'} {name:28s} exit={p.returncode} "
          f"stdout={got!r} stderr={state!r}", flush=True)
    return ok


def main(argv: list[str]) -> int:
    out_dir = COMPILER / 'build' / 'contracts' / 'journal_aliases'
    out_dir.mkdir(parents=True, exist_ok=True)
    names = argv or list(CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:
        print(f"unknown case(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    failed = 0
    for name in names:
        try:
            if not run_case(name, CASES[name], out_dir):
                failed += 1
        except Exception as e:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {name:28s} {type(e).__name__}: {e}", flush=True)
    print(f"journal_aliases: {len(names) - failed}/{len(names)} passed -> "
          f"{'FAIL' if failed else 'OK'}")
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
