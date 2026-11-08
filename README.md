# aws-cli-toolbox

Functions and configuration to streamline AWS CLI and boto3 on macOS and Linux.
It handles SSO login across orgs, role assumption, decoding STS messages,
generating profiles for a whole Organization, and building Netskope CA bundles.
See [DESIGN.md](DESIGN.md).

All boto3 traffic from these commands uses the OS trust store via `truststore`.
In your own boto3 scripts, call `truststore.inject_into_ssl()` (or `import aws_toolbox`)
before importing boto3.

## Getting started

Do these steps once, in order. Steps 5–7 are the day-to-day loop afterwards.

### 1. Check prerequisites

```bash
python3 --version   # needs 3.11+ (Homebrew python3.13 works; macOS system 3.9 does not)
uv --version        # https://docs.astral.sh/uv/
aws --version       # AWS CLI v2, used by aws-sso-refresh
```

`~/.local/bin` must be on your `PATH`, because that's where `uv tool` puts the commands.

### 2. Install

```bash
git clone <repo-url> ~/Code/aws-cli-toolbox   # or wherever you keep it
cd ~/Code/aws-cli-toolbox
./install.sh             # dry-run: prints every step, changes nothing
./install.sh --execute   # do it
```

**What gets installed is `main`, never your working tree.** The installer takes
a `git archive` snapshot of the install ref and installs from that. Checking
out another branch, uncommitted edits or a broken work-in-progress can't touch
the installed copy, so AWS access keeps working during development. To install
a different ref, pass `--ref <branch|tag|sha>`, or set it once with
`git config aws-toolbox.ref <ref>`.

`install.sh` does four things:

1. Installs the commands (`aws-toolbox`, `aws-sso-refresh`, `aws-sts-decode`,
   `aws-sync-profiles`, `ns-cert-refresh`) with a regular, non-editable
   `uv tool install` from the snapshot. uv copies them into its own tool
   environment, so they don't depend on the repo afterwards.
2. Retires older `~/.functions.d/*.functions` files whose functions the toolbox
   supersedes; the names are listed in [shell/replaces.txt](shell/replaces.txt).
   This matters because a shell function takes precedence over a command on
   `PATH`. If a file mixes superseded functions with others, the installer
   lists them and stops without changing anything; move them out by hand and
   re-run.
3. Copies `shell/aws-toolbox.functions` to `~/.functions.d/aws-toolbox.functions`.
   Your shell rc sources every `~/.functions.d/*.functions` file. The file is
   copied, not linked, because the rc loop uses `find -type f`, which skips
   symlinks.
4. Writes a starter `~/.aws/toolbox.toml` if you don't already have one.

**Backups and rollback:** every existing file that's replaced or retired is
first copied to `~/.functions.d/.backup/<name>.<YYYYmmddHHMMSS>.bak`. The
`.bak` suffix keeps the rc loop from sourcing it. The installer prints the
exact `cp` commands to restore each one.

Open a new shell when it finishes.

### Keeping the install in sync

Nothing syncs automatically: there are no git hooks or background jobs. The
installed copy changes only when you run the installer with `--execute`.

```bash
git checkout main && git pull   # or however main gets updated
./install.sh --status           # installed commit vs main; lists new commits
./install.sh                    # dry-run: what a sync would change
./install.sh --execute          # sync
```

`--status` exits 0 when up to date, 3 when a sync is available, and 1 when
nothing is installed. `install.sh` itself runs from whatever is checked out,
but it reads every file it installs from the snapshot of the ref.

### 3. Configure the toolbox

Edit `~/.aws/toolbox.toml`. Every key is documented in
[toolbox.example.toml](src/aws_toolbox/toolbox.example.toml). At minimum:

| Key | Set it to |
|---|---|
| `sso.main_profile` | Your primary SSO profile (default `main-org`). Its login opens the browser. |
| `sync.management_profile` | A profile that can call `organizations:ListAccounts` |
| `sync.sso_session` **or** `sync.sso_start_url` + `sync.sso_region` | How generated profiles authenticate |
| `sync.readonly_role` / `sync.admin_role` | Permission set names for `<name>` and `<name>-adm` profiles |

Set `AWS_TOOLBOX_CONFIG` to keep the file somewhere else.

#### More than one organization

Add one `[orgs.<name>]` table per organization and set `sso.main_org`:

```toml
[sso]
main_org = "acme"          # this portal's logins open the browser

[sync]                       # roles and suffix shared by every org
readonly_role = "ViewOnlyAccess"
admin_role = "AdministratorAccess"

[orgs.acme]
sso_session = "acme"
management_profile = "acme-mgmt-ro"
profile_prefix = "acme-"

[orgs.globex]
sso_session = "globex"
management_profile = "globex-mgmt-ro"
profile_prefix = "globex-"
```

- Each org needs its own `management_profile` and exactly one of `sso_session`
  or `sso_start_url` + `sso_region`. These are never inherited from `[sync]`, so
  a missing value can't point one org's profiles at another org's portal. Two
  orgs can't share a session, start URL or management profile.
- The management profile must be defined in `~/.aws/config` and sign in at the
  org's own portal: an SSO profile, or a role whose `source_profile` chain ends
  at one. Anything else (keys only in `~/.aws/credentials`, `credential_process`)
  is refused, because the toolbox can't confirm which org it belongs to. The same
  check applies to `--profile`.
- `readonly_role`, `admin_role` and `admin_suffix` come from `[sync]` unless the
  org overrides them. Any other `[sync]` key can be set per org.
- Give each org its own `profile_prefix`. If two orgs still produce the same
  name, whichever org's block already has it keeps it, and the other gets an
  account-ID suffix.
- Once any `[orgs.*]` table exists, the rest of `[sync]` is ignored.

#### Assume-role profiles from the account-mapping table

Set `source_profile` on an org to generate role-chained profiles instead of SSO
ones. It follows the same conventions as the switch-role bookmarks generator,
and reads the same `.env` keys, so both tools can share one file:

```toml
[orgs.acme]
sso_session = "acme"
management_profile = "acme-bil-ro"       # reads Organizations
source_profile = "acme-bil-cpa"          # SSO profile every generated profile chains from
env_file = "~/path/to/shared.env"
profile_prefix = "acme-"
```

- **Accounts:** the full OU tree from the root. Suspended, pending-closure and
  closed accounts are skipped unless you pass `--include-inactive` (or set
  `BM_INCLUDE_INACTIVE=true`).
- **Metadata:** the DynamoDB table in `BM_DDB_TABLE_ARN`, matched on
  `accountNumber` (zero-padded to 12 digits). It's read with `BM_DDB_PROFILE`,
  else the Organizations profile if it's in the table's account, else by
  assuming `BM_DDB_ROLE_NAME`. The run stops if those credentials aren't in the
  table's account. An account with more than one row is skipped with a warning.
  The table is only read, never written.
- **Names:** `<prefix><short name><suffix>`, where the short name is
  `accountShortCode` (lowercased), falling back to the account name. Each
  profile has a comment line with the account name, ID, business domain
  (`ou`, or `Legacy`) and environment (`Lab`, `Sbx`, `Dev`, `Tst`, `Prd`, `Bil`).
- **Roles:** one profile per role. The list is `roles` in `[sync]` (or per org),
  else `BM_DEFAULT_ROLES`. `role_suffixes` maps each role to its suffix; a role
  that isn't mapped gets `-<role name>`. Suffixes must be `""` or lowercase like
  `-ro`, and no two roles can share one:

  ```toml
  [sync]
  roles = ["Platform-Admin", "ReadOnly", "Account-Admin"]
  role_suffixes = { "Platform-Admin" = "", "ReadOnly" = "-ro", "Account-Admin" = "-adm" }
  ```

  For an account with short name `prd` this gives `acme-prd`, `acme-prd-ro`
  and `acme-prd-adm`.
- **Only accounts with a short name:** set `require_short_name = true` (in
  `[sync]` or per org; assume-role orgs only) to skip accounts whose short-name
  attribute (default `accountShortCode`) is empty or that have no table row,
  instead of naming them after the account. The table's own account is always
  kept, because later runs may need its profiles to read the table.
- **No table access yet (bootstrap):** if the table can't be read because of
  missing access, and the org has no managed block yet, profiles are named after
  the accounts instead, and `require_short_name` is ignored for that run. On the
  next run, if the configured access still fails, the table is read through the
  org's own generated profiles for the table's account, trying each role in
  `roles` order. That run replaces the account-name profiles with short-name
  ones. So with no other access to the table:

  ```bash
  aws-sync-profiles --org acme --execute   # run 1: account names, incl. the table's account
  aws-sync-profiles --org acme             # run 2 (dry run): short names via that profile
  aws-sync-profiles --org acme --execute
  ```

  Only access errors (access denied, missing credentials or profile, expired
  SSO login) trigger this. A wrong-account credential, a missing table or a
  network error stops the org with exit 1. Once the org has a managed block, an
  unreadable table leaves it unchanged and the run exits 1, so a temporary
  access problem never renames profiles. `--force` rebuilds them with account
  names. Excluding the table's account (`exclude_accounts`), or giving it
  duplicate table rows, removes the profiles that later runs fall back to.
- **Precedence:** command-line flags (`--ddb-table-arn`, `--ddb-profile`,
  `--ddb-role-name`, `--attr-*`, `--roles`, `--include-inactive`, `--profile`),
  then the org's `toolbox.toml` keys, then environment variables, then
  `env_file`, then defaults. With several orgs, the table flags need
  `--org NAME`. A relative `env_file` is resolved against `toolbox.toml`'s directory.
- `source_profile` must be defined by hand outside the managed block and sign in
  at the org's portal. Each role in the list must trust it.
- **A different source per role:** `role_source_profiles` (per org) chains the
  listed roles from another hand-written profile. For example, admin profiles
  can come from an admin SSO profile while the rest use `source_profile`:

  ```toml
  [orgs.acme]
  source_profile = "acme-billing-cpa"
  role_source_profiles = { "Account-Admin" = "acme-billing-adm" }
  ```

  Each of these profiles has the same requirements as `source_profile`.

**Moving from `[sync]` to `[orgs.*]`:** if `aws-sync-profiles` has already
written profiles from `[sync]`, it refuses to run until you delete that older,
unnamed block. Otherwise the old names would stay taken and every regenerated
profile would get an account-ID suffix. Back up `~/.aws/config`, delete
everything from the `# BEGIN aws-toolbox managed profiles (do not edit…` line
to its `# END` line, and make sure each org's management profile is defined
outside the managed blocks. Then run the dry-run again.

### 4. Bootstrap `~/.aws/config`

You need at least the main SSO profile defined by hand. If you set `sync.sso_session`,
add a matching `[sso-session]` section too. With several orgs, define an
`[sso-session]` and a management profile for each one:

```ini
[sso-session main]
sso_start_url = https://my-org.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile main-org]
sso_session = main
sso_account_id = 111111111111
sso_role_name = AdministratorAccess
region = us-east-1
```

### 5. (Netskope only) Build the CA bundle

Run this if you're behind the Netskope agent. Run it again after the agent rotates its tenant CA.

```bash
ns-cert-refresh            # dry-run: shows which certs it found
ns-cert-refresh --execute  # writes ~/.aws/ca-bundle.pem
```

The functions file exports `AWS_CA_BUNDLE=~/.aws/ca-bundle.pem` in new shells
whenever that file exists. If you change `netskope.bundle_path`, export
`AWS_CA_BUNDLE` yourself.

### 6. Log in

```bash
aws-sso-refresh --list     # what will be logged in, and token status
aws-sso-refresh            # log in where needed
```

Logins for the main org's (or main profile's) start URL go first and open your
default browser. For any other start URL, the CLI prints a URL and code. Open it
in the browser profile or container for that org. An org that uses
`sso_session` is logged in even before any profile uses it, so it can sign in
before its first sync. An org configured with `sso_start_url` needs at least one
profile first.

A login is skipped when its cached token has at least `sso.min_remaining_minutes`
left. It's also skipped when the profile can still call STS, which covers
tokens that botocore renewed with a refresh token. `--force` logs in anyway.

### 7. Generate profiles for every account

```bash
aws-sync-profiles               # dry-run: prints a diff of ~/.aws/config
aws-sync-profiles --execute     # writes it (timestamped backup first)
aws-sync-profiles --org globex    # just one org (repeatable)
```

With several orgs, each one gets its own `# BEGIN/END aws-toolbox managed
profiles: <org>` block. If one org fails (expired login, access denied), its block
is left as it was, the other orgs are still written, and the command exits 1.

Each active account gets `<name>` (read-only role) and `<name>-adm` (admin role).
The profiles go inside a `# BEGIN/END aws-toolbox managed profiles` block. Anything
outside that block is never touched, and a profile you defined yourself is never
overwritten. Re-run it whenever accounts are added. If accounts were added under
a new start URL, run step 6 again too.

```bash
aws sts get-caller-identity --profile <account-name>
```

## Everyday use

```bash
# Assume a role in the current account, or another one; exports creds into this shell
aws-assume-role OrganizationAccountAccessRole
aws-assume-role OrganizationAccountAccessRole 123456789012
aws-clear-identity                            # drop creds and AWS_PROFILE

# Switch profile (fzf picker with no argument; logs in if expired) — zsh only
aws-profile
aws-profile prod-payments-adm
aws-whoami                                    # profile, account, ARN, region

# Decode an "Encoded authorization failure message"
aws-sts-decode "$ENCODED"                     # with current credentials
aws-sts-decode "$ENCODED" 123456789012        # with that account's profile (falls back to -adm)
pbpaste | aws-sts-decode 123456789012         # message on stdin
```

## Migrating from aws-shell.functions

The installer retires `~/.functions.d/aws-shell.functions` (backup in
`~/.functions.d/.backup/`). Its functions map as follows:

| Old | Now |
|---|---|
| `aws-assume-role` | `aws-assume-role`: same arguments. It also unsets `AWS_PROFILE` (which otherwise overrides the new creds) and doesn't need jq |
| `aws-sso-refresh` | `aws-sso-refresh` command. Same `--force`. Adds the main-profile browser and `--no-browser` split from DESIGN.md |
| `aws-sts-decode-message` | `aws-sts-decode`; the old name still works |
| `aws-clear-identity`, `aws-profile`, `aws-whoami` | Unchanged, now in `aws-toolbox.functions` |
| `aws-sso-sync` | `aws-sync-profiles`, which builds from Organizations with `<name>` / `<name>-adm` names. The old function built profiles from the SSO portal, one per account and role you can reach. Restore it from the backup if you need that |

Profiles that `aws-sso-sync` already added to `~/.aws/config` stay where they
are. `aws-sync-profiles` won't touch them, but it may add a second profile for
the same account under the new naming.

## Command reference

| Command | Does | Writes? |
|---|---|---|
| `aws-sso-refresh` | Logs in once per SSO token cache: each `[sso-session]` a profile or org uses, and each direct start URL | SSO token cache only |
| `aws-assume-role ROLE [ACCOUNT]` | `sts:AssumeRole` and exports the creds (shell function) | Shell env only |
| `aws-clear-identity` | Unsets creds and `AWS_PROFILE` (shell function) | Shell env only |
| `aws-profile [-n] [PROFILE]` | Sets `AWS_PROFILE`; fzf picker; SSO login if expired (zsh) | Shell env only |
| `aws-whoami` | Prints profile, account, ARN, region (zsh) | No |
| `aws-sts-decode [MESSAGE] [ACCOUNT]` | `sts:DecodeAuthorizationMessage`, pretty-printed | No |
| `aws-sync-profiles` | Each org's accounts → `<name>` / `<name>-adm` profiles, one block per org | Dry-run unless `--execute` |
| `ns-cert-refresh` | Netskope tenant CA + system CAs → bundle | Dry-run unless `--execute` |
| `aws-toolbox init` | Writes the starter `~/.aws/toolbox.toml` | Yes (`--force` to overwrite) |

Every command takes `-v`/`--verbose`, `-q`/`--quiet` and `--help`, placed after
the command name. Each one also works as `aws-toolbox <command>`. Logs go to
stderr; stdout carries only results (diffs, JSON, shell exports).

## Platform notes

- **macOS**: `truststore` uses Security.framework. The Netskope certs are read from `/Library/Application Support/Netskope/STAgent/data`.
- **Linux**: `truststore` uses the system CA bundle, so install `ca-certificates`. The Netskope certs are read from `/opt/netskope/stagent/data`.
- **Windows**: not supported. DESIGN.md targets macOS and Linux only.

## IAM permissions

- sync-profiles: `organizations:ListAccounts`, in the management account or a delegated administrator account
- assume-role: `sts:AssumeRole` on the target role, which must also trust the caller
- sts-decode: `sts:DecodeAuthorizationMessage`, in the account that produced the message

## Development

Work in `./.venv`; it's separate from the installed copy, so nothing you do
here affects your AWS access until it's merged to `main` and you sync.

```bash
uv sync                                 # creates ./.venv with dev deps
.venv/bin/python -m pytest -q
.venv/bin/aws-sync-profiles             # run the in-development version
```

Tests use `moto` and botocore's `Stubber`. They never touch real AWS or your
real `~/.aws`.
