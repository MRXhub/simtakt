# Adapter: server session

This example models a long-lived solver service (such as a COMSOL server-style
integration) without starting a process or opening a socket. `FakeServer` is a
pure Python stand-in with an observable license counter.

| Aspect | Server-session model | Batch model |
|---|---|---|
| Session body | Connection plus server-issued token | Disk/process job and its files |
| `resume_session` | Idempotent reconnect; never creates a second session | Recover/rebind a job receipt |
| Termination | Must send explicit disconnect/shutdown | Stop/reap the process and files |
| License recovery | Only explicit disconnect releases checkout | Process exit normally releases it |

A lost connection is `unreachable`, not `absent`: the transport failure says
nothing about whether the server-side session still exists. Treating it as
absent could create a duplicate session, lose results, or incorrectly release
a capacity ledger. This fake server intentionally keeps its license checked out
after transport disconnect; only `disconnect` decrements the counter.

The adapter has three deliberately unresolved design TODOs in code. They are
questions a production integration must answer rather than guessed code:
connection/authentication/session identity; progress and success/divergence
criteria; and result export/materialization as a verifiable artifact.

## Contrast with COMSOL batch mode

For comparison, COMSOL documents batch execution using a command-line shape
such as `comsol batch -inputfile model.mph -outputfile out.mph`:
[COMSOL 6.3 command-line documentation](https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/comsol_ref_runningthemodel.37.08.html).
The exact switches and behavior are version/platform dependent; consult the
server's documentation for the installed COMSOL version. Batch is a separate
example shape: a bounded process/job receipt, not a live connection + token
session, and is intentionally not implemented here.

Run the self-contained demo with:

```sh
python examples/adapter-server-session/run_demo.py
```
