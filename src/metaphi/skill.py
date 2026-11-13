"""The Kennet skill, which teaches a coding agent this CLI: `kennet agents install` puts it where each harness reads
skills (metaphi/agents.py).

The file installed is a stub: when to reach for the CLI, and one instruction, run `kennet skill show`. The guide
itself is printed by the binary, so it is always the guide of the version installed and never goes stale on disk.
Kept as text in this module, not a data file, so the one-file binary carries it."""
from __future__ import annotations

from metaphi import __version__

NAME = "kennet"   # the skill is Kennet's, as the CLI is
MARK = "<!-- written by `kennet skill install`; edits are overwritten -->"

STUB = f"""---
name: {NAME}
description: Kennet, Metaphi AI's RL infrastructure, driven through its CLI, `kennet`: synthetic twinning of agent traces, and the RL task sets (deliveries) an organization holds, and the organization's members and service keys. Use when the user wants to add, remove or change the role of a member of their Metaphi organization (`kennet org members`), or to list, create or revoke the organization's CI or service keys (`kennet keys`), or when the user wants to list, pull, verify or update delivered RL tasks (`kennet delivery`), or when the user wants real people, companies, accounts or secrets in a trace, log or transcript file replaced with consistent synthetic stand-ins (a "synthetic twin") before sharing it or training on it, or when the user is authoring, checking or running RL tasks (Harbor bundles) on Kennet: task CI, `eval_ready`, rollouts, wrong solutions; or mentions Metaphi, Kennet twins, `kennet twin`, or a `mai_sk_` key.
---
{MARK}

# Kennet

Run `kennet skill show` and follow what it prints: it is the guide for the version of the CLI on this machine.

If `kennet` is not found, install it, then run `kennet skill show`:

```sh
curl -fsSL https://github.com/metaphi-labs/kennet-cli/releases/latest/download/install.sh | sh   # macOS, Linux
brew install metaphi-labs/tap/kennet     # or Homebrew
pip install kennet                       # or anywhere with Python 3.10+
```
"""

GUIDE = f"""# Kennet from the command line (kennet {__version__})

Kennet is Metaphi AI's RL infrastructure. Through this CLI it offers synthetic twinning (steps 3 to 5) and the
task sets your organization holds, its deliveries (step 6). Steps 1 and 2, the sign-in, are the same for both.
Joining an organization that invited the user, and managing its members and service keys, is step 7; authoring
and checking tasks is step 8.

`kennet twin <file>` uploads one trace file (JSON, JSON Lines or text) to Metaphi AI's service and writes its
synthetic twin beside it: the same file, same structure, with every real person, private organization, account
and secret replaced by one consistent synthetic value of the same kind. It is called a twin, never a redaction.
The file leaves this machine to be twinned, on a test run as on a live one: say so if the user's concern is where
their data goes.

## Rules

- Add `--json` to every command. stdout is then one JSON object: the result, or `{{"error": {{...}}}}`. Progress
  is on stderr. Read stdout only: stderr repeats an error in words, for a person. Under `--json` nothing opens a
  browser: `kennet login --json` prints the page and the code for the user to open.
- The exit status says what kind of end it was:

  | Exit | Meaning |
  | --- | --- |
  | 0 | Done. |
  | 1 | Failed. Read `error.code`. |
  | 2 | The command line was wrong (no JSON is printed). |
  | 3 | A sign-in or a key is needed. `error.next_step` is the command, or null when only the user can clear it. |
  | 4 | The same call may work later: the service is unavailable, a limit is reached, or a wait ran out. |
  | 5 | This version of `kennet` is too old for the service (`upgrade_required`). |

- On an error, read `error.code` and find it in the table in step 4. `error.next_step` is always there: the next
  command to run (add `--json` to it), or null when no command clears it. `error.advice`, when there, is what
  the user does that one command does not (sign in as another address, ask an owner): tell them. `error.retry_after_s`, when there, is
  how long the service said to wait before the same call. Never decide from the message text.
- A command retries a failed request for at most 5 seconds by itself. Past that it returns the error, so a
  command that fails does so quickly; waiting longer is yours to do.
- Never ask the user to paste a token, key or code into the chat, never print one, never pass one as an
  argument. The one code they handle is the sign-in's `user_code`, which they check on the page. Keys are saved by
  `kennet login` or come from `METAPHI_API_KEY`.
- An organization has one free test run in all (`--test`): a real twin through the same pipeline, of a file up
  to 2 MiB. Spend it on a representative file, and ask the user which one before you do. Once it is used the
  refusal is `test_runs_used` (exit 1; `next_step` is the same command without `--test`): runs from then on are
  live. A live run draws the organization's credit:
  ask the user before the first one, even after a good test run.
- `kennet` says nothing about new releases under `--json`. What install.sh installed updates itself in the
  background, only to a release signed by Metaphi AI (`KENNET_DISABLE_AUTOUPDATE=1` stops it). `kennet update
  --json` answers `version`, `latest`, `method` and `next_step`, the upgrade command, when this install is not
  the newest.
- When a step needs the user and they are not there, stop and hand back what they have to do. Do not work around it.
- When something fails that you cannot clear, quote `error.request_id` to the user, or `error.run_id` (the twin's
  id) for a run that failed.

## 1. Where things stand

```sh
kennet whoami --json
```

It asks the servers, and asks the twin service whether each key works: `key.status` and `test_key.status` are
`"ok"` or the code a call would meet now. Exit 0 means the key in use works.

- `key.source` is `METAPHI_API_KEY`: the environment holds a key. No sign-in is needed, on this machine or in
  CI; go to step 3. (That key decides live or test by itself: `mai_sk_test_...` is a test key.)
- `error.code` `not_signed_in` (from this or any command): go to step 2.
- `error.code` `no_api_key`: the user is signed in, and the sign-in was handed no key. `reason` beside `error`
  says why:

  | `reason` | Means | What to do |
  | --- | --- | --- |
  | `no_organization` | The user is in no organization. | Ask whether their company already uses Kennet. If it does, an owner or admin there adds them (`kennet org members add`, step 7), then `kennet login --json`. If not, ask what theirs is called and make it: `kennet org create "Acme Robotics" --json`. They own it. `"access": "pending"`: Metaphi AI approves it, and the user is mailed; stop until they say it came, then `kennet login --json`. `"access": "active"`: run that now. |
  | `awaiting_approval` | Their organization waits for Metaphi AI to approve it. | Tell the user they are mailed when it is; then `kennet login --json`. Nothing else clears it. |
  | `no_keys_yet` | The organization was just made or approved. | `kennet login --json` once. |
  | `no_access` | Their organization holds no access to this yet. | Tell the user to ask their Metaphi AI contact, then stop. |
  | `request_pending` | Their organization already has a request waiting. | Say so; they are mailed when it is decided. |
  | `record_not_found` | The DNS TXT record for the domain is not visible yet. | Have the user check the record and retry; DNS can take a while. |
  | `domain_taken` | Another organization holds that domain. | Tell the user to contact Metaphi AI. |
  | `blocked` | Metaphi AI closed requests for that access. | Tell the user to contact their Metaphi AI contact. |
  | `not_offered` | That access cannot be asked for; the message lists what can. | Ask again for one it lists, or tell the user to contact Metaphi AI. |
  | `already_held` | Their organization holds it already. | Nothing to do; run `kennet login` if the key lacks it. |
  | `service_account` | The sign-in is a service account, not a person. | A service account's key is made in the console and set as `METAPHI_API_KEY`; a person signs in as themselves (step 2). |
  | `invitation_pending` | An organization has invited the user and they have not accepted. `invitation` beside `error` names it (`org`, `org_name`, `role`, `expires`). | Ask the user whether to join it; on yes, run `error.next_step`, `kennet org join <org> --json` (step 7). Never make an organization while an invitation waits. |
  | `removed` | An owner or admin took the user out of their organization. | Only an owner of that organization can add them back: tell the user so, with the message, and stop. `error.next_step` is null. |

  Any other `reason`, or none: run `kennet login --json` once (a signed-in machine opens no browser); if it ends in
  `no_api_key` again, tell the user the message as it is, and stop.
- Any other `error.code`: the table in step 4. `email`, `org` and `organizations` are beside `error`.
- `invitations`, when there, are the invitations waiting for the user, as accounts lists them now, each with its
  `id` when accounts sends one: `kennet org join <org> --json` (or the `id`) accepts one.
- When the saved key is refused, `whoami` reads why without changing any key: an invitation waiting is
  `kennet org join <org>`; otherwise the message says whether the key was replaced by a sign-in elsewhere or the
  user is in no organization, and `next_step` is `kennet login`, which says the rest.
- Exit 0: go to step 3. `org` is the organization the keys belong to. `organizations` lists each organization the
  user is in, with their `role` and what it holds (`grants`), `available_usd`, what they may spend now (null: no
  limit; `bound` says what limits it, `resets` when a monthly limit does), and `credits_usd`, the organization's
  balance, when they may see it.

## 2. Sign in (needs the user, once)

```sh
kennet login --json
```

It returns at once with `"status": "pending"`: nobody is signed in yet. With `--json` it never opens a browser
(at a terminal, without `--json`, it opens the page; `--no-browser` only prints it). Tell the user, with the page
and code exactly as printed:

- Open `verification_uri_complete` in any browser: this machine's, or another device's (a phone, a laptop) when
  this one has none, as over SSH or in a sandbox. The code ties the page to this terminal.
- Sign in with Google or GitHub as their work email, the address their organization knows them by. The address
  must be verified with that provider.
- Before approving, check that the page names the Kennet CLI and shows the same code as `user_code`; if either
  is off, do not approve. Approve within `expires_in` seconds.

Running it again while that code stands returns the same code. Then:

```sh
kennet login --complete --json
```

This waits up to 60 seconds for the approval (`--timeout <seconds>` changes that). `error.code`
`authorization_pending` means not approved yet: run it again, or stop and wait for the user to say they have.
`"keys": true` means the organization's live and test keys are saved on this machine: run `kennet whoami --json`
and tell the user the `email` and `org` it names. `error.code` `no_api_key` means the sign-in worked and no key
came with it: `reason` beside `error` says why (step 1's table). Each sign-in replaces the keys the one before it
saved on any machine. On a machine that is signed in already, `kennet login --json` opens no browser and returns
this result at once.

When the page does not get there:

| The user sees | What to do |
| --- | --- |
| Nothing opens, or the sign-in window closes at once | The browser blocked the popup: allow popups for the page, and click sign-in again. |
| "email not verified" | The address is not verified with that provider: verify it there (GitHub: Settings, Emails), or sign in with the other provider. |
| "that code is unknown or expired" (`unknown_or_expired`) | Run `kennet login --json` for a new code, and start again. |
| "that sign-in is too old to approve a terminal" (`sign_in_too_old`) | Sign in again on the page with the button, then approve. |
| `whoami` names the wrong address | Run `kennet logout`, then step 2 again; ask them to sign out of that account in the browser, or use a private window. |

Servers and CI never sign in: an owner or admin makes the organization a service key, with `kennet keys create`
(step 7) or in the console (https://app.metaphi.ai/console), and it is set as `METAPHI_API_KEY`, or saved on the
machine by `kennet login --with-api-key`, which reads it from stdin. You never read the key: `kennet keys
create` pipes it straight into the store.

## 3. Twin a file

```sh
kennet twin trace.json --test --json        # free; file at most 2 MiB; one test run in all per organization
kennet twin trace.json --json               # live; draws credit
```

Result: `{{"output": "trace.twin.json", "id": "twin_..."}}`. The output is `<name>.twin<extension>` beside the input
(`a.jsonl` gives `a.twin.jsonl`, `notes.txt` gives `notes.twin.txt`) and replaces a file already there.
`--out <directory>` writes it there under the same name instead, which keeps twins apart from the originals;
`-o <path>` names the file itself. The input file is never changed.

The result does not list what was replaced. Before telling the user a twin is safe to share, read the output
against the input and say what you checked.

Options that change the result:
- `--confidential <organization>`: public organizations (a well-known company) are left as they are unless named
  here. When the trace names the user's customers or partners, ask the user which to replace. Repeatable.
- `--key-file <path>`: a file holding a consistency key (64 hex characters), which is not an organization key.
  The same one gives the same person the same stand-in in every file, so use one for a set of files that belong
  together. `(umask 077; openssl rand -hex 32 > twin.key)` makes one; the user keeps it, as a secret. The
  `KENNET_CONSISTENCY_KEY` variable is read when no file is named. Without either, each file gets a fresh one.

A twin can take minutes. If your command may time out, start it and come back:

```sh
kennet twin trace.json --no-wait --json     # {{"id": "twin_...", "status": "queued", "next_step": "kennet twins wait twin_..."}}
kennet twins wait twin_... --json           # {{"output", "id"}} once it ends; safe to run again
kennet twins get twin_... --json            # its status now: queued, running, completed, failed
kennet twins list --json                    # the organization's twins, newest first
```

Add `--test` to `twins` commands for twins started with `--test`, and `--out <directory>` to `twins wait` as to
`twin`. A wait that stops (timeout, interrupt) never stops the twin: `error.next_step` is the command that
comes back for it. `--no-wait` returns once the file is uploaded and the twin exists, which takes seconds; a
twin that then fails says so at `twins wait` or `twins get`, with the same error object.

Several files: one `kennet twin` per file, with one `--key-file` for all of them. Start each with `--no-wait`,
then `twins wait` each id. On `rate_limited_concurrent`, wait for one to end before starting the next.

## 4. Errors

| `error.code` | What to do |
| --- | --- |
| `network` | This machine could not reach the service (`retryable`; the message names the host and why). Nothing was changed. Wait a few seconds, run the same command once more, then tell the user to check their connection. |
| `service_unavailable` | Something in front of the service (a gateway, a proxy, a captive portal) answered instead of it (`retryable`). Nothing was changed. Wait `retry_after_s` (30 seconds when absent), run the same command once more, then tell the user. |
| `interrupted` | The command was stopped (Ctrl-C, or the process was signalled) before it ended; exit 130. Run it again if it should finish. |
| `internal` | `kennet` stopped on something it did not expect: a bug. Do not retry in a loop. Tell the user, with `kennet --version` and the `debug_log` path beside `error`, so it can be reported. |
| `upgrade_required` | This `kennet` is too old for the service. `next_step` is the upgrade command for how it was installed (`brew upgrade ...`, `pip install -U ...`, `kennet update`, or the install.sh line): ask the user, run it, then the same command again. |
| `update_unreachable` | `kennet update` could not reach GitHub (`retryable`). Try once more later; nothing changed. |
| `update_unsigned`, `update_mismatch`, `update_refused` | The download was not Metaphi AI's signed release, or went elsewhere; nothing was installed. Tell the user; do not retry or work around it. |
| `update_missing`, `update_broken`, `update_not_permitted` | The update could not be installed here; nothing changed. `next_step` is the install.sh line: ask the user before running it. |
| `not_signed_in`, `login_expired`, `login_refused` | Step 2, from its start. |
| `step_up_required` | A change to the organization needs a recent sign-in. Step 2, from its start, then the same command once more. |
| `authorization_pending` | The user has not approved the code yet. Run `kennet login --complete --json` again, or wait for them. |
| `no_api_key` | Signed in, and no key came with the sign-in. If `next_step` is `kennet org create <name>`, the user is in no organization: make theirs (step 1). If it is `kennet login`, run `kennet login --json` once: it picks the keys up, with no browser, if access was given since. If it comes back `no_api_key` again, tell the user the message and stop. |
| `invalid_api_key`, `missing_api_key` | The key was revoked or replaced, often by a later sign-in elsewhere. Step 2, or the user sets a new `METAPHI_API_KEY`. |
| `insufficient_scope` | The key does not hold access to what the command reaches. Run `kennet login --json` once: a signed-in machine is handed new keys with no browser. If it repeats, tell the user. |
| `auth_unavailable`, `unavailable`, `balance_unavailable`, `unreachable` | The service could not answer; nothing was started. Wait `retry_after_s` (30 seconds when absent), run the same command once more, then stop and tell the user with the `request_id`. |
| `run_unavailable` | The twin started and failed on Metaphi AI's side; nothing was written or charged. "Upload the trace again" means run `next_step`, `kennet twin <file>`, with the options you used before. Do that once. If it fails the same way, stop and give the user both `run_id`s. |
| `git_too_old` | `kennet profile-codebase` needs a newer git. Tell the user; nothing was sent. |
| `no_repository` | `kennet profile-codebase` found no full git clone at the paths given; each path's reason is in the output. Tell the user to clone it in full and run the same command. |
| `build_without_test`, `deep_with_test` | `kennet profile-codebase` was given `--build` without `--test`, or `--test` with `--deep`. Fix the flags and run it again; nothing ran or was sent. |
| `out_of_credit` | Nothing was started. Give the user `error.action_url` to add credit, or run `kennet credits buy --amount <dollars> --no-browser`, show them the Stripe link it prints, and let it wait for the payment. Then run the same command again. Never choose the amount yourself. |
| `rate_limited_concurrent`, `rate_limited_daily` | Too many runs at once, or today. Wait for a run to end, or tell the user. |
| `timed_out` | The twin goes on. Run `next_step`. |
| `twin_unclean`, `session_failed` | The run failed and cost nothing. If `retryable`, run it once more; else tell the user with the `run_id`. |
| `not_text`, `too_large` | The file cannot be twinned as it is. Tell the user. |
| anything else | If `retryable` is true, wait `retry_after_s` (10 seconds when absent) and try once more. Else, or if it fails again, tell the user the message and `request_id`. |

## 5. Balance

```sh
kennet credits --json                       # {{"org", "credits_usd", "can_buy"}}
```

Only owners, admins and billing members see it; anyone else gets an error saying so.

## 6. Task deliveries

A delivery is a versioned set of RL tasks your organization holds: `<name>@<version>`, as `eswe-mini@0.1.0`. A
version never changes; a fix is a new version. Each task is a Harbor bundle (`task.toml`, `instruction.md`,
`environment/`, `tests/`, `solution/`) whose images are pinned by digest.

```sh
kennet delivery list --json                         # {{"data": [{{"id", "delivered", "task_count", "image_count", "bytes", "newer", "registry"}}]}}
kennet delivery show eswe-mini@0.1.0 --json         # the same, and "tasks": [{{"name", "tree", "images", "cpus", "memory_mb", "agent_timeout_s", "verifier_timeout_s", ...}}]
kennet delivery pull eswe-mini --json              # the newest version, into ./eswe-mini-<version> (or --to <dir>); {{"id", "dir", "lock", "fetched", "unchanged", "bytes", "next_step"}}
kennet delivery pull eswe-mini@0.1.0 --json         # that version; "newer" names a newer one when it is out
kennet delivery verify ./eswe-mini-0.1.0 --json     # offline; exit 0 and "ok": true, or exit 1 with "differ" (per file: "delivered" and "here" sha256) and "added"
kennet registry login --json                        # signs docker in to your image repository for an hour; {{"server", "repository", "client", "expires_at"}}
kennet delivery diff eswe-mini@0.1.0 eswe-mini@0.1.1 --json   # {{"added", "removed", "changed": [{{"name", "grading_changed", "files"}}]}}; either side can be a pulled directory
```

From nothing to a set ready to run, in order: `kennet login --json` (and `--complete`), `delivery list`,
`delivery show <id>` to size the machines, `delivery pull <id>`, `delivery verify <dir>`, `registry login`, then
pull each image the lock names. Each answer's `next_step` is the command that follows.

- `pull` is safe to run again: it fetches only the tasks the directory does not already hold as the lock names
  them. A task that was changed is put back whole, as delivered, and anything added inside it goes: keep your
  own files (results, notes, solutions) outside the delivery's directory. `--dry-run` names the tasks it would
  fetch and so replace. A pull that stopped is finished by the same command.
- The pulled directory is `delivery.lock.json`, `SHA256SUMS` and `tasks/<task>/`. The lock is the record: each
  task's `tree` (a SHA-256 over every file's hash, mode and path) and its `images` by digest. `verify` reads that directory
  and nothing else, so it works with no key and no network, on the training machine too. Run it before a run,
  and treat exit 1 as "these are not the tasks that were delivered": pull again before running.
- Size the sandboxes from `show`: a task's `cpus`, `memory_mb`, `storage_mb`, `agent_timeout_s` and
  `verifier_timeout_s` are what it asks. A sandbox smaller than that fails for reasons that are not the model's.
- Images are in `registry`, your organization's own image repository, and a task runs only against the digests
  its lock names: never pull one by tag. `kennet registry login --json` signs docker (or podman, nerdctl) in to
  that repository for an hour; it stores no key, so run it again when a pull is refused or `expires_at` has
  passed. An image is `<registry>/<image>` for each entry of a task's `images`. For anything else that pulls
  (a cluster's pull secret, crane, skopeo), `kennet registry token --json` answers `server`, `username`,
  `token` and `expires_at`: hand the token over on stdin or in memory, never in a file, a log or a command
  line. Fetch every image before a run, since the tasks run with no internet. An organization that gave
  Metaphi AI a service account of its own pulls as that account too, with no token from here.
- Run the tasks with Harbor on your own machines; nothing of Metaphi AI is called at run time. Keep the bundle
  as it is: `tests/` and `solution/` must never be put where the agent being trained can read them.
- Before the first sampling run on a new delivery, check it on those machines with Harbor's two built-in
  agents, which call no model: `oracle` applies each task's `solution/` and must score 1 on every task; `nop`
  changes nothing and must score 0. A task that disagrees does not run here as it was delivered (an image that
  did not pull, a sandbox too small, another Harbor version): leave it out of the run and tell the user the
  task, the delivery id, the task's `tree` and the verifier's output, so it can be reported.
- With every result you keep, record the delivery id and the task's `tree`. Scores compare only within one
  version, and `diff` says, per task, whether its grading changed (`grading_changed`): if it did, scores on
  that task from the older version do not carry over.
- A trial whose verifier output starts with `verifier error:` or `ungradable:` was not graded, although its
  reward reads 0. Do not train on it and do not count it as a wrong answer: run it again, and if it repeats,
  tell the user which task and which delivery.
- `newer` on a delivery names a later version your organization holds. A held-out set, when there is one, is a
  delivery of its own and is never part of a training pull.

Errors: `resource_missing` (no such delivery for your organization; `kennet delivery list`),
`insufficient_scope` (this machine's keys predate deliveries; `kennet login --json` replaces them, with no
browser), `directory_in_use` (the directory holds another delivery; `next_step` names a free one),
`tree_mismatch` (a download was not the locked tree and was thrown away; run the pull again),
`not_a_delivery` (`verify` was pointed at a directory with no lock it can read), `invalid_delivery` (the id is
not `<name>@<version>`), `no_container_client` (nothing to sign in; use `kennet registry token`),
`registry_login_failed` (the client refused; its reason is in the message), `lock_invalid` and `lock_too_new` (tell the user; the second says to upgrade `kennet`).

## 7. Your organization's members and keys

An organization that invited the user (`reason` `invitation_pending`, or `invitation` in `whoami`) is joined with:

```sh
kennet org join acme --json        # or the invitation's id: kennet org join inv_... --json
```

It accepts the invitation, signed in as the address it was sent to, and saves the organization's keys:
`{{"email", "org", "keys": true, "joined": {{"org", "role"}}, "next_step"}}`. Ask the user first: a person is in one
organization. `error.code` `no_invitation`: there is no open invitation from it (expired or taken back); tell the
user to ask an owner of it for a new one. `invitation_for_another_address`: the invitation was mailed to another address than the one signed in;
`next_step` is `kennet logout`, then step 2 signed in as that address (`advice`). `step_up_required`: as below. `no_api_key` after
joining: step 1's table.

Owners and admins manage who is in the organization. Ask the user before every change: whom, and in which role.

```sh
kennet org members --json                             # {{"org", "role", "members": [{{"email", "role"}}], "invitations": [{{"id", "email", "role", "expires"}}]}}
kennet org members add bob@acme.com --role admin --json   # {{"org", "email", "role", "status": "member" or "invited", "invitation" when invited, "members"}}
kennet org members role bob@acme.com billing --json   # the same answer as add
kennet org members remove bob@acme.com --json         # {{"org", "email", "status": "removed" or "uninvited", "members"}}
```

- Roles are exactly these four; `add` defaults to `member`:

  | Role | May |
  | --- | --- |
  | `owner` | everything: members, owners, keys, credit. Only an owner makes, changes or removes an owner, and the organization keeps one. |
  | `admin` | add, change and remove members other than owners; manage keys; see the balance. |
  | `billing` | see the balance and buy credit; nothing else. |
  | `member` | use the organization's keys. |

- `"status": "invited"`: the address is off the organization's domains, so it is not a member yet. Metaphi AI
  mails them an invitation, which they accept signed in as that address, within a week. Tell the user so; there
  is no link to pass on. `remove` on that address takes the invitation back. `error.code` `mail_unavailable`: the
  invitation could not be mailed; the user invites them on the keys page the message names.
- `--org <id>` names the organization when the user manages several; the error lists them.
- `error.code` `not_manager`: the user is in the organization but not an owner or admin, so its members are not
  theirs to see or change. Tell them; there is no command for it.
- `error.code` `step_up_required`: the change needs a recent sign-in. Run `kennet login --json` (and
  `--complete`, step 2) with the user, then the same command once more.
- Any other refusal (only an owner changes an owner; the organization keeps one owner; the address belongs to
  another organization) is the server's sentence in `error.message`: tell the user, and do not retry.

Owners and admins also manage the organization's service keys: the keys CI and servers call with, as
`METAPHI_API_KEY`. A key belongs to the organization, not to whoever made it. Ask the user before you create or
revoke one: its name, and for a revoke, that nothing still uses it.

```sh
kennet keys list --json                     # {{"org", "keys": [{{"id", "name", "hint", "scopes", "livemode", "owner", "created_by", "created", "last_used", "revoked"}}]}}; --all adds revoked ones
kennet keys revoke github-ci --yes --json   # by name or id (key_...); {{"org", "id", ..., "status": "revoked" or "already_revoked"}}
```

Create a key only in the pipe form, straight into the secret store that keeps it, never with `--json`:

    kennet keys create github-ci | gh secret set METAPHI_API_KEY --repo acme/app

- You never read, print or hold the key: it goes from `kennet` into the store and nowhere else, so it is never in
  your output or the transcript. Down a pipe stdout carries the key alone; what was made (name, id, last four) is
  on stderr, which is what you read and tell the user. `--test` makes a test key. Another store works the same
  way, as long as it reads the secret from stdin (`vercel env add METAPHI_API_KEY production`, `fly secrets
  import`); if the user's store cannot, tell them to run `kennet keys create <name>` themselves, at their
  terminal, where it is shown to them once.
- `--json` never carries the key unless `--show-secret` is given, and without it nothing is made:
  `secret_destination_required`, `next_step` the pipe form. Never add `--show-secret` yourself.
- A key holds everything the organization holds unless `--scope <scope>` (repeatable) names less. `owner`
  `"cli"` marks a key `kennet login` handed a terminal; `service` is one made here or in the console.
- `revoke` stops the key within a minute. At a terminal it asks first; away from one, and under `--json`, it needs
  `--yes`, else `confirmation_required` (`next_step` is the same command with `--yes`, by id). Ask the user, then
  run it.
- Errors: `not_manager` (the user is not an owner or admin; tell them, no command clears it), `step_up_required`
  (as above), `no_key` (no live key by that name or id; `kennet keys list`), `ambiguous_key` (several live keys
  share the name, or the word is one key's id and another's name; name it by id from `kennet keys list`),
  `name_taken` (a live key has that name; pick another or revoke it), `scope_not_held` (the organization does not
  hold that scope), `too_many_keys` (it holds the most live keys it may; revoke an unused one first),
  `already_revoked` (it was revoked meanwhile; nothing to do), `awaiting_approval` and `no_access` (it holds
  nothing a key could carry yet: step 1's table). A refusal with `"type": "auth_error"` and another code, or
  none (`not_cli_client`: the sign-in is not this CLI's), is the server's sentence: run `kennet login --json`
  once (step 2), then the same command.

## 8. Author and check tasks

Authoring runs through Kennet's MCP tools, not this CLI. Connect them once, for every coding agent on this
machine, then sign in:

```sh
kennet agents install --json
```

Each row is one harness (`claude`, `codex`, `hum`, `gemini`, `copilot`, `cursor`, `opencode`). `mcp` `added` or
`installed`: the server is registered and `sign_in` is how to sign in to it (a command to run, such as
`codex mcp login kennet`, or where in the agent to do it); tell the user, since the browser page is theirs.
`present`: a server named kennet was there already and is left as it is. `skipped`: `mcp_detail` says why.
`skill_state` `kept`: a skill file there that kennet did not write; `--force` replaces it, only if the user says
so. `next_step` is the first `sign_in` that is a command. `--harness <name>` (repeatable) picks harnesses;
`--project` writes the root of this repository's files instead, to commit (`not_a_repository` outside one).
`no_agent_found`: none is on this machine; name one with `--harness`. `nothing_installed` (exit 1): no harness got
either piece; each row beside the error says why. `kennet agents remove` takes away what install added, and only
that. A tool call that says the server needs a sign-in: the same `sign_in`. Run `kennet agents install` when the
user asks for it, never on your own: it changes their agents' configuration.

The loop: write a Harbor bundle, check it (`run_task_ci_checks`), run models on it (`run_harbor_tasks`), read
the rollouts (`harbor_runs`, `harbor_run_report`, `harbor_rollouts`), fix, repeat. Long calls return a task:
poll `get_task`. While a run runs, `tail_run` (or `kennet tail <owner/name> <run-id> --json`, an event a line)
says what each trial is doing: its phases, the steps setting its sandbox up, and what its commands write. A
person watching a run wants `kennet tail <owner/name> <run-id>` in their own terminal: suggest it.

- A bundle is named by `repo` (owner/name), `ref` and the path to its `task.toml` directory. Kennet reads
  GitHub, never the local checkout: commit and push the ref first. The call needs push on the repository.
- The bundle is a Harbor task: `task.toml`, `instruction.md`, `environment/`, `solution/`, `tests/`. Nothing
  else at its root except `wrong/` and `right/`.
- Wrong solutions (must score below 1.0) go in `wrong/`, right ones (must score 1.0) in `right/`, each a
  `.patch` or a directory laid over the working directory. They sit in the problem directory, never under
  `tests/`: beside the bundle when it is `<problem>/harbor/`, else at the bundle's root. At least one wrong
  solution is needed for `eval_ready`.
- A GPU task names one type: `gpus` and `gpu_types = ["L40S"]` under `[environment]`.
- CI: `eval_ready` true is the verdict. Otherwise `gate.blocking_checks` names what refused, and each finding
  says the fix; `gate.advisory_failed` is advice. Fix the bundle, push, run again.
- Rollouts: `mode` `conduct`, `models`, `harness` and `reasoning_effort` (required; Kennet has no default),
  `k` trials. The models you may run are the `models` field's enum. A refused call says what it needs: tell
  the user, never swap in another model or setting yourself.
- A trial that failed for an infrastructure reason was not graded, whatever its reward reads: run it again,
  and do not count it as the model's failure.
- What no check settles, or a fault in Kennet itself: open an issue on the task's repository with the run id,
  the finding and what was tried, and tell the user.
"""


# --- the partner skill: for an agent helping a data owner, or a broker, license an asset to Metaphi AI ---

PARTNER_NAME = "kennet-partner"

PARTNER_STUB = f"""---
name: {PARTNER_NAME}
description: Licensing private data to Metaphi AI through the `kennet` CLI, for the people who own it and the brokers who represent them. Use when the user wants to license, sell, value or price a private data asset, or find out what theirs is worth for AI training: a codebase or its git history; exports of company systems (Slack, email, calendars, Notion, Jira, Confluence); or robotics and physical-world data (CAD models and assemblies, ROS bags, MCAP, sensor time series, teleoperation episodes, robot descriptions). Also when they want a mutual NDA with Metaphi AI, or mention metaphi.ai/partners, `kennet profile-codebase` or `kennet mnda`.
---
{MARK}

# Kennet for partners

Run `kennet skill show partner` and follow what it prints: it is the guide for the version of the CLI on this
machine.

If `kennet` is not found, ask the user, install it, then run `kennet skill show partner`:

```sh
brew install metaphi-labs/tap/kennet                 # macOS, Linux with Homebrew
curl -fsSL https://metaphi.ai/kennet.sh | sh         # or the signed install script
pip install kennet                                   # or anywhere with Python 3.10+
```
"""

PARTNER_GUIDE = f"""# Kennet for partners (kennet {__version__})

Metaphi AI licenses private data to build training environments for AI agents: places where an agent learns by
doing real work and is checked on the result. The user owns such data, or is a broker for people who do. Your job:
find out what they have, show them its shape and what makes it valuable, get it profiled or summarised with the
least of their time, and put a mutual NDA in place when they want one. Nothing on this path waits on a person at
Metaphi AI, and nothing leaves their machine that they did not choose to send.

| Asset | Examples | What the CLI does today |
| --- | --- | --- |
| Software | git repositories; mainframe estates; hardware designs (RTL) under git | `kennet profile-codebase` (step 3) |
| Company systems | Slack, Teams, email, calendars, Notion, Confluence, Jira, Linear, CRM exports | a local shape summary (step 4) |
| Robotics and physical | CAD and PLM exports, ROS bags, MCAP, sensor time series, teleoperation episodes (LeRobot, RLDS, HDF5), robot descriptions (URDF, MJCF, SDF) | a local shape summary (step 4) |

## Rules

- Metadata before content. Look at the data through names, sizes, counts, dates, formats and schemas: what
  `ls`, `du`, `find`, `wc -l`, `git` counts and a format's own info tool print. Opening a file's contents puts
  them in your context, which goes to your model's provider: ask the user before you read any message, document,
  email, source file or recording, and read the fewest lines that answer the question. Never paste content into
  the chat or into a summary.
- Nothing is uploaded except by the commands below, and only after you have told the user what they send. Only
  `--deep` sends file contents: never add it without asking in those words.
- Add `--json` to every `kennet` command. stdout is one JSON object, the result or `{{"error": {{...}}}}`; progress
  is on stderr. Decide from `error.code` and `error.next_step`, never from message text. Exit 0 done; 1 failed;
  2 the command line was wrong; 3 a sign-in is needed (`next_step`); 4 try the same call later; 5 `kennet` is too
  old (`next_step` upgrades it: ask, run it, go on).
- Never ask the user to paste a token, key or code into the chat. The one code they handle is the sign-in's
  `user_code`, which they check on the page.
- Respect their time: one question at a time, a default they can accept with "yes", and say what you will run
  before you run it.

## 1. Ask what they have

Ask one question: which data they have the most of, or could hand over soonest, using the three rows above. Then
ask where it is: a path on this machine, a git URL, or a system it still lives in (then: whether they can export
it, and in what format). A broker may have several owners' assets: take them one at a time, and keep each
owner's apart in what you write.

## 2. Survey its shape, locally

Measure; do not judge from a glance. Use only what is installed: if a tool is missing, say what it would add and
go on without it. Put the numbers in a short table for the user as you go.

### Software

```sh
git -C <repo> rev-list --count HEAD                      # commits
git -C <repo> log --reverse --format=%as | sed -n '1p;$p' # first and last commit dates
git -C <repo> shortlog -sn HEAD | wc -l                  # people who committed (a count; do not print names)
git -C <repo> ls-files | sed -n 's/.*\\.//p' | sort | uniq -c | sort -rn | head   # languages, by extension
git -C <repo> ls-files | grep -Eci '(^|/)(tests?|spec|__tests__)/|_test\\.|\\.test\\.|test_'   # test files
ls <repo>/.github/workflows <repo>/.gitlab-ci.yml <repo>/Jenkinsfile 2>/dev/null   # CI
```

`git rev-parse --is-shallow-repository` must print `false`: a shallow clone has lost its history. A mainframe
estate (COBOL, JCL, copybooks) counts as software; `harwell.toml` at its root marks one Kennet can build.

What makes it rich: years of commits by many engineers solving real problems; tests beside the code that pin
behaviour, so a change can be checked; a build that runs; CI; changes small enough to read one at a time; and
work whose correctness or speed can be measured (systems, performance, compilers, mainframe, hardware design).

### Company systems

Know the export's shape before anything else. Common ones:

| System | Export | Count with |
| --- | --- | --- |
| Slack | a zip: `users.json`, `channels.json`, a folder a channel of one JSON file a day | `jq length users.json channels.json`; `ls <channel> \\| wc -l` (days); first and last file names (span) |
| Google Workspace | Takeout: mail as `.mbox`, calendars as `.ics`, Drive files | `grep -c '^From ' <file>.mbox`; `grep -c BEGIN:VEVENT <file>.ics` |
| Microsoft 365 | `.pst` mailboxes, Teams exports | file count and size |
| Notion, Confluence | Markdown or HTML pages with CSV databases, as a zip | page and database counts; folder depth |
| Jira, Linear | CSV or JSON of issues | `wc -l`; the header row's field names |

What makes it rich: many people working together over months, not one channel; threads that end in a decision
or an outcome; links across systems (a ticket named in chat, a doc in a meeting); timestamps and identities kept
consistent across exports; and the actions people took, not only what they said. These exports name real people
and companies: Metaphi AI replaces them with consistent synthetic stand-ins (Kennet's synthetic twinning) before
anything is used. Tell the user so if they ask.

### Robotics and physical

| Data | Formats | Count with |
| --- | --- | --- |
| Recordings | ROS 1 `.bag`, ROS 2 `.db3` or `.mcap`, MCAP | `mcap info <file>` or `ros2 bag info <dir>`: duration, topics, message counts and rates |
| Episodes | LeRobot (Parquet and MP4 with `meta/info.json`), RLDS (TFRecord), HDF5, NPZ | `meta/info.json` fields (fps, features, episode count); `h5ls -r` for HDF5 structure |
| Time series | CSV, Parquet, vendor logs | header row; row count; sampling rate from the time column |
| Video | MP4 and image folders a camera | `ffprobe` duration, resolution and frame rate |
| CAD | native (SolidWorks `.sldprt`/`.sldasm`, Creo, NX, CATIA, Fusion, Onshape), neutral (STEP, IGES), meshes (STL, OBJ) | counts by format; assemblies vs parts; PDM or PLM revision history |
| Robot models | URDF, MJCF, SDF | joints and links (`grep -c '<joint'`) |

What makes it rich: hours across many tasks, places and conditions; actions recorded beside observations (the
commands or teleoperation, not only the sensors); sensors on one clock; episodes cut at task boundaries with
success or failure marked; calibration and a robot description, so a run can be replayed or simulated. For CAD:
native files with their feature history, revisions with the reason for each, assemblies with constraints, and
checks that can be run on a design (simulation, tolerances, manufacturability, test results).

## 3. Profile a codebase

Name the repository: a path to a full git clone or a git URL. A broker names several in one run.

`--json` sends what it measured. Ask the user before running it; at their own terminal, `kennet profile-codebase`
shows the checks first and sends only when they answer yes.

```sh
kennet profile-codebase ./their-repo --json
kennet profile-codebase ./core ./billing https://github.com/acme/ledger.git --json
```

It reads the git history, then builds and tests the latest commit once in a temporary copy on this machine (in
Docker or Podman when one is running). It sends counts and ids: line, size and token counts per commit and file
with numbers in place of names, the repository's name and commit hashes, and the build and test run's exit codes,
timings, test counts and logs. File names, authors, commit messages and source are never sent. Everything sent is
kept in `./kennet-profile/<run>/sent/`: show the user that folder if they ask what left.

- Exit 0: `{{"reference": "cbp_..."}}`. Tell the user it was received, the reference, and that Metaphi AI reviews
  every profile and replies by email to the address they signed in with. There is nothing to poll.
- `not_signed_in` (exit 3): under `--json` it does not sign in by itself. Run `kennet login --json`, give the user
  `verification_uri_complete` and `user_code` exactly as printed (they sign in with their work email and check the
  code matches), then `kennet login --complete --json`, then the same profile command. At a terminal without
  `--json`, `kennet profile-codebase` signs the user in itself.
- `no_api_key` with `reason` `no_organization`: ask what their company is called, `kennet org create "<name>"
  --json`, then `kennet login --json` and the profile again.
- When it found no way to build and test a repository, the output says so. Read its README, CI config and build
  files (ask first: that is content), propose the commands, and on the user's yes run the same command with
  `--test '<command>'`, and `--build '<command>'` when the build is separate. Tests that need a service (a
  database, a cache) run where that service is up.
- `--commits` also builds and tests a sample of past commits: a stronger profile, and slower. Offer it once the
  first one is in.
- A send that failed: run the same command again. It sends what was kept and runs nothing twice.

## 4. A shape summary, for everything else

Write what step 2 measured to `./metaphi-asset-summary.md` in the user's working directory: the asset and its
systems or formats, size, time span, counts, the richness points above each marked present, absent or unknown,
and what the user wants (a licence, a price, a conversation). Counts and formats only: no names of people,
channels, customers, projects or files unless the user asks for them in it. Show it to the user. It leaves the
machine only if they send it: to agent@metaphi.ai, or with the profile command for that asset once this CLI has
one (`kennet --help` lists them; this version profiles codebases alone).

## 5. A mutual NDA

A profile never needs one: it sends counts and ids. Offer it when the user asks about confidentiality, or before
anything beyond a profile or a summary is shared.

```sh
kennet mnda --json
```

`status` is `signed` (`signed_at` says when), `pending` (`url` is where the user signs it) or `none`. For `none`,
on the user's yes:

```sh
kennet mnda sign --json
```

It makes the mutual NDA between their organization and Metaphi AI and answers `pending` with its `url`. Give the
user the link: they sign it in the browser (DocuSign), and Metaphi AI countersigns. `kennet mnda --json` shows
`signed` once they have.

## 6. Errors

| `error.code` | What to do |
| --- | --- |
| `not_signed_in`, `login_expired`, `login_refused` | Step 3's sign-in, from its start. |
| `authorization_pending` | The user has not approved the code yet: `kennet login --complete --json` again, or wait for them. |
| `no_api_key` | Read `reason`: `no_organization` is in step 3; `awaiting_approval` means Metaphi AI approves their organization and mails the user; anything else, run `kennet login --json` once, then tell the user the message. |
| `no_organization` | From `kennet mnda`: make their organization as in step 3, then the same command. |
| `insufficient_scope` | `kennet login --json` once (no browser on a signed-in machine), then the same command. If it repeats, tell the user the message. |
| `git_too_old` | Ask the user to update git; nothing was sent. |
| `no_repository` | No full git clone at a path given; each path's reason is in the output. Ask the user to clone it in full (not shallow), then run the same command. |
| `build_without_test` | `--build` was given without `--test`: add the test command. |
| `deep_with_test` | `--deep` and `--test` do not go together: drop one. |
| `network`, `service_unavailable`, `unavailable` | Nothing changed. Wait `retry_after_s` (30 seconds when absent), run the same command once more, then tell the user with `request_id`. |
| `upgrade_required` | Ask the user, run `next_step`, then the same command. |
| anything else | If `retryable`, wait and try once more. Else tell the user the message and `request_id`. |

For anything beyond licensing (twins, deliveries, members and keys), `kennet skill show` is the full guide.
"""

SKILLS = {NAME: STUB, PARTNER_NAME: PARTNER_STUB}   # each written where each harness reads skills
GUIDES = {"kennet": GUIDE, "partner": PARTNER_GUIDE}   # `kennet skill show [<guide>]`
