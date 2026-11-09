# aws-cli-toolbox

Functions and configuration to streamline AWS CLI and boto3 on macOS and Linux.
It handles SSO login across orgs, role assumption, decoding STS messages,
generating profiles for a whole Organization, and building Netskope CA bundles.
See [DESIGN.md](DESIGN.md).

All boto3 traffic from these commands uses the OS trust store via `truststore`.
In your own boto3 scripts, call `truststore.inject_into_ssl()` (or `import aws_toolbox`)
before importing boto3.

## Quick start: assume-role profiles

The shortest path from nothing to one working profile per account. It uses
the example names from [Role conventions](#role-conventions-and-how-to-customize-them).
Replace `acme`, the IDs and the URL with yours. [Getting started](#getting-started)
below covers each step in full.

**Before you start**, someone with Identity Center admin needs to have set up:

- a permission set (here `ReadOnly`) assigned to you in the source account
  (here the billing account, `111111111111`), with an inline policy that
  allows `sts:AssumeRole` on the target role;
- a role with the same name in every target account (here `ReadOnly`), whose
  trust policy allows that permission set.

The JSON for both is under [AWS side, per tier](#aws-side-per-tier).

1. **Install** (needs Python 3.11+ and AWS CLI v2; uv is optional, and no admin rights are needed):

   ```bash
   git clone <repo-url> ~/Code/aws-cli-toolbox && cd ~/Code/aws-cli-toolbox
   ./install.sh --execute
   exec $SHELL -l
   ```

2. **Add the SSO session and your first profile** to `~/.aws/config`. This
   first profile is the first hop: an ordinary SSO profile that every
   generated profile chains from. Use a name that can't be an account short
   name, such as `acme-sso-ro`. Find the portal URL and its region in the
   IAM Identity Center console under **Settings**. If you sign in through
   another identity provider, such as Entra ID or Okta, follow its AWS app
   link once and copy the `https://….awsapps.com/start` address it ends on.

   ```ini
   [sso-session acme]
   sso_start_url = https://my-org.awsapps.com/start
   sso_region = us-east-1
   sso_registration_scopes = sso:account:access

   [profile acme-sso-ro]
   sso_session = acme
   sso_account_id = 111111111111
   sso_role_name = ReadOnly
   region = us-east-1
   ```

3. **Log in and check the first hop:**

   ```bash
   aws sso login --sso-session acme
   aws sts get-caller-identity --profile acme-sso-ro
   ```

   The ARN should be
   `arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_ReadOnly_…/you@example.com`.

4. **Configure the org** in `~/.aws/toolbox.toml`, including the ARN of the
   account table. Without it, profiles are named after the accounts instead
   of their short names, with a warning:

   ```toml
   [sso]
   main_org = "acme"

   [sync]
   roles = ["ReadOnly"]
   role_suffixes = { "ReadOnly" = "-ro" }

   [orgs.acme]
   sso_session = "acme"
   management_profile = "acme-sso-ro"   # needs organizations:ListAccounts
   source_profile = "acme-sso-ro"       # the first hop from step 2
   profile_prefix = "acme-"
   ddb_table_arn = "arn:aws:dynamodb:us-east-1:222222222222:table/account-mapping"
   # ddb_profile = "..."                # optional: a profile that can read the table
   ```

5. **Preview, then write:**

   ```bash
   aws-sync-profiles --org acme             # dry run: prints the diff
   aws-sync-profiles --org acme --execute   # writes it, after a backup
   ```

   If the first hop can't read the table yet, the profiles are named after
   the accounts. Run steps 5 and 6 once more and they're renamed to short
   names, using the newly generated profile for the table's account (see
   "No table access yet (bootstrap)" below).

6. **Check a generated profile:**

   ```bash
   aws sts get-caller-identity --profile acme-prd-ro
   ```

   The ARN should be `arn:aws:sts::<prd account>:assumed-role/ReadOnly/…`.
   If it says `AccessDenied` on `AssumeRole`, one of the two policies above is
   missing.

7. **Add more tiers** when you need them, Read-Only-Plus first for day-to-day
   work. For each one:
   - create its permission set and target role;
   - add an SSO profile like step 2 (`acme-sso-rop`);
   - add it to `roles`, `role_suffixes` and `role_source_profiles`;
   - run step 5 again.

   See [Customizing](#customizing).

Afterwards, `aws-sso-refresh` logs in again when the session expires.

## Getting started

Do these steps once, in order. Steps 5–7 are the day-to-day loop afterwards.

### 1. Check prerequisites

```bash
python3 --version   # needs 3.11+ (Homebrew python3.13 works; macOS system 3.9 does not)
uv --version        # optional; https://docs.astral.sh/uv/
aws --version       # AWS CLI v2, used by aws-sso-refresh
```

`~/.local/bin` must be on your `PATH`, because that's where the commands go.

No admin rights are needed, and nothing is installed into the system Python.
Everything goes under your home directory, and the installer refuses to run
as root or under `sudo`. A Python 3.11+ that you can run is enough: uv, a
user-level Homebrew, pyenv or a company-provided build all work.

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
   `aws-sync-profiles`, `ns-cert-refresh`) into `~/.local/bin` from the
   snapshot, so they don't depend on the repo afterwards:
   - **with uv** (the default when `uv` is on `PATH`): a regular, non-editable
     `uv tool install`, into uv's own tool environment;
   - **without uv** (or `--installer pip`): a private venv under
     `~/.local/share/aws-cli-toolbox/venvs/<commit>`, made with your Python
     3.11+ and filled with pip, and links to its commands in `~/.local/bin`.
     A failed install leaves the previous venv in use. The Python needs
     `venv` support (on Debian and Ubuntu, `python3-venv`); without it, use uv.

   Switching between the two (with `--installer` and `--force` at the same
   commit) removes the other one's copy first.
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

**Company networks.** Two problems are common behind a corporate proxy:

- **`invalid peer certificate: UnknownIssuer`:** the proxy inspects TLS
  traffic with its own certificate authority. Add `--system-certs` so uv
  and pip trust the OS certificate store, which has that authority.
- **`403 Forbidden` from `pypi.org`:** public PyPI is blocked, and packages
  must come from a company mirror (Artifactory, Nexus and similar). Pass it
  with `--index-url https://…/simple`. Without the flag, the installer uses
  uv's own setting (`UV_DEFAULT_INDEX` / `UV_INDEX_URL`) if one is set, else
  `PIP_INDEX_URL`, else the `index-url` in your pip configuration. uv doesn't
  read pip's configuration itself. A username or token in the URL is hidden
  in the output.

```bash
./install.sh --execute --system-certs --index-url https://artifactory.example.com/api/pypi/pypi/simple
```

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

That's for SSO profiles. For role-chained profiles, an org also needs
`source_profile`, and usually `ddb_table_arn`, `roles` and `role_suffixes`. See
[Assume-role profiles](#assume-role-profiles-from-the-account-mapping-table)
and the [quick start](#quick-start-assume-role-profiles).

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
- These `[sync]` keys are shared by every org unless the org sets its own:
  - `readonly_role`, `admin_role`, `admin_suffix`;
  - `roles`, `role_suffixes`, `require_short_name`, `account_name_aliases`;
  - `ddb_role_name`, the `attr_*` keys, `include_inactive`.

  Every other key is per org. `ddb_table_arn`, `ddb_profile`, `source_profile`
  and `role_source_profiles` in `[sync]` are an error once `[orgs.*]` tables
  exist; set them in each org. (An org without `source_profile` or
  `role_source_profiles` gets SSO profiles, and the run logs "SSO mode".)
- Give each org its own `profile_prefix`. If two orgs still produce the same
  name, whichever org's block already has it keeps it, and the other gets an
  account-ID suffix.
- Once any `[orgs.*]` table exists, every other `[sync]` key is ignored. That
  includes `management_profile`, `sso_session`, `profile_prefix`,
  `exclude_accounts`, `source_profile` and `role_source_profiles`.

#### Assume-role profiles from the account-mapping table

Set `source_profile` and/or `role_source_profiles` on an org to generate
role-chained profiles instead of SSO ones. It follows the same conventions as the switch-role bookmarks generator.
Everything is set in `toolbox.toml`; no `.env` file is read:

```toml
[orgs.acme]
sso_session = "acme"
management_profile = "acme-sso-ro"       # reads Organizations
source_profile = "acme-sso-cpa"          # SSO profile every generated profile chains from
profile_prefix = "acme-"
ddb_table_arn = "arn:aws:dynamodb:us-east-1:222222222222:table/account-mapping"
```

Table settings (per org unless noted; all optional except `ddb_table_arn`):

| Key | Default | Meaning |
|---|---|---|
| `ddb_table_arn` | — | The account-mapping table. Per org (in `[sync]` only when there are no `[orgs.*]`). |
| `ddb_profile` | — | Profile used to read the table. Per org, like `ddb_table_arn`. |
| `ddb_role_name` | `OrganizationAccountAccessRole` | Role assumed in the table's account when no profile there works. `[sync]` or per org. |
| `attr_account_id` / `attr_short_name` / `attr_ou` / `attr_environment` | `accountNumber` / `accountShortCode` / `ou` / `environment` | Table attribute names. `[sync]` or per org. |
| `include_inactive` | `false` | Include suspended, pending-closure and closed accounts. `[sync]` or per org. |

**No table configured:** without `ddb_table_arn`, the run warns and still
generates every profile, named after the account (`acme-shared-services-ro`),
with no domain or environment in the comment line. `require_short_name` is
ignored. Add `ddb_table_arn` later and run again to switch to short names.

- **Accounts:** the full OU tree from the root. Suspended, pending-closure and
  closed accounts are skipped unless you set `include_inactive = true` or pass
  `--include-inactive`.
- **Metadata:** the DynamoDB table at `ddb_table_arn`, matched on
  `accountNumber` (zero-padded to 12 digits). It's read with `ddb_profile`,
  else the Organizations profile if it's in the table's account, else by
  assuming `ddb_role_name`. The run stops if those credentials aren't in the
  table's account. An account with more than one row is skipped with a warning.
  The table is only read, never written.
- **Names:** `<prefix><short name><suffix>`, where the short name is
  `accountShortCode` (lowercased), falling back to the account name. Each
  profile has a comment line with the account name, ID, business domain
  (`ou`, or `Legacy`) and environment (`Lab`, `Sbx`, `Dev`, `Tst`, `Prd`, `Bil`).
- **Roles:** one profile per role. The list is `roles` in `[sync]` (or per org),
  else `BG_DEFAULT_ROLES`, else the roles in the org's `role_source_profiles`
  (in order), else `ReadOnly,Administrator`. `role_suffixes` maps each role to its suffix; a role
  that isn't mapped gets `-<role name>`. Suffixes must be `""` or lowercase like
  `-ro`, and no two roles can share one:

  ```toml
  [sync]
  roles = ["Platform-Admin", "ReadOnly", "Account-Admin"]
  role_suffixes = { "Platform-Admin" = "", "ReadOnly" = "-ro", "Account-Admin" = "-adm" }
  ```

  For an account with short name `prd` this gives `acme-prd`, `acme-prd-ro`
  and `acme-prd-adm`. See [Role conventions](#role-conventions-and-how-to-customize-them)
  for the suggested tiers and how to change them.
- **Only accounts with a short name:** set `require_short_name = true` (in
  `[sync]` or per org; assume-role orgs only) to skip accounts whose short-name
  attribute (default `accountShortCode`) is empty or that have no table row,
  instead of naming them after the account. The table's own account is always
  kept, because later runs may need its profiles to read the table.
- **Long and short names:** set `account_name_aliases = true` (in `[sync]` or
  per org) to also write each account's profiles under its account name, so
  `acme-ops-ro` and `acme-operations-ro` both work. This only applies to
  accounts whose short name differs from the account name. Short names win:
  an account-name alias that would clash with a short name, another alias or
  a hand-written profile is skipped with a warning. During the bootstrap run
  below, the account names are the only names.
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
  then the org's `toolbox.toml` keys, then the bookmarks generator's
  environment variables (`BG_DDB_TABLE_ARN`, `BG_DDB_PROFILE`,
  `BG_DDB_ROLE_NAME`, `BG_ATTR_*`, `BG_DEFAULT_ROLES`, `BG_INCLUDE_INACTIVE`),
  then defaults. With `[orgs.*]` tables, `BG_DDB_TABLE_ARN` and
  `BG_DDB_PROFILE` are ignored, since a shell variable can't tell orgs apart. An
  empty or whitespace-only toml value counts as unset. With several orgs, the
  table flags need `--org NAME`.
- **`env_file` was removed.** A `toolbox.toml` that still sets it is refused
  with a message saying so. Move `BG_DDB_TABLE_ARN` to `ddb_table_arn`,
  `BG_DDB_PROFILE` to `ddb_profile`, `BG_DEFAULT_ROLES` to `roles`, and so on.
  Variables with the old `BM_` prefix (for example `BM_DDB_TABLE_ARN`) are
  ignored, with a warning naming the `BG_` variable to use instead, matching
  the bookmarks generator.
- `source_profile` must be defined by hand outside the managed block and sign in
  at the org's portal. Each role in the list must trust it.
- **A different source per role:** `role_source_profiles` (per org) chains the
  listed roles from another hand-written profile. For example, admin profiles
  can come from an admin SSO profile while the rest use `source_profile`:

  ```toml
  [orgs.acme]
  source_profile = "acme-sso-cpa"
  role_source_profiles = { "Account-Admin" = "acme-sso-adm" }
  ```

  Each of these profiles has the same requirements as `source_profile`.

**Moving from `[sync]` to `[orgs.*]`:** if `aws-sync-profiles` has already
written profiles from `[sync]`, it refuses to run until you delete that older,
unnamed block. Otherwise the old names would stay taken and every regenerated
profile would get an account-ID suffix. Back up `~/.aws/config`, delete
everything from the `# BEGIN aws-toolbox managed profiles (do not edit…` line
to its `# END` line, and make sure each org's management profile is defined
outside the managed blocks. Then run the dry-run again.

#### Role conventions and how to customize them

Every assume-role profile is two hops:

1. **First hop:** an SSO profile you define by hand. It signs in to a
   permission set in the account that holds `source_profile`, usually the
   billing or management account.
2. **Second hop:** an IAM role, with the same name in every target account. The
   generated profile sets `role_arn` to that role and `source_profile` to the
   first-hop profile.

The suggested convention has four tiers. Use the lowest one that does the job:

| Tier | First hop (SSO permission set → profile) | Second hop (target role) | Suffix | For |
|---|---|---|---|---|
| Read only | `ReadOnly` → `acme-sso-ro` | `ReadOnly` | `-ro` | Looking around; auditors, on-call triage |
| Read only plus | `ReadOnlyPlus` → `acme-sso-rop` | `Read-Only-Plus` | `-rop` | Day-to-day work for power users and platform administrators |
| Platform admin | `PlatformAdmin` → `acme-sso-cpa` | `Platform-Admin` | *(none)* | Platform changes that need broad write access |
| Account admin | `AccountAdmin` → `acme-sso-adm` | `Account-Admin` | `-adm` | Break-glass and IAM changes; full admin |

**Read only plus** is the everyday role for people who would otherwise reach
for an admin role out of habit. Start the target role from `ReadOnlyAccess`
and add only the routine operational actions your team does daily. For example:

- reading secrets and parameters (`secretsmanager:GetSecretValue`, `ssm:GetParameter*`, `kms:Decrypt`) where your policy allows it;
- Session Manager (`ssm:StartSession`, `ssm:TerminateSession`, `ssm:ResumeSession`);
- restarting things (`ec2:RebootInstances`, `ecs:UpdateService` for force-redeploys, `lambda:InvokeFunction`);
- CloudWatch Logs Insights queries and support cases (`logs:StartQuery`, `support:*`).

Leave out IAM writes, Organizations, SSO administration, deleting data and
changing network boundaries. Those stay with the admin tiers. Pair it with a
permissions boundary or SCP if you want a hard ceiling. The toolbox doesn't
create any of these roles or permission sets. It only writes profiles that
point at them.

The toolbox config for that convention:

```toml
[sync]                     # shared by every org; any org may override
roles = ["ReadOnly", "Read-Only-Plus", "Platform-Admin", "Account-Admin"]
role_suffixes = { "ReadOnly" = "-ro", "Read-Only-Plus" = "-rop", "Platform-Admin" = "", "Account-Admin" = "-adm" }

[orgs.acme]                # per org only; never inherited from [sync]
# ...plus the usual org keys (sso_session, management_profile, ...)
source_profile = "acme-sso-cpa"      # first hop for any role not listed below
role_source_profiles = { "Read-Only-Plus" = "acme-sso-rop", "ReadOnly" = "acme-sso-ro", "Account-Admin" = "acme-sso-adm" }
```

For an account with short name `prd` this gives `acme-prd-ro`, `acme-prd-rop`,
`acme-prd` and `acme-prd-adm`. Each one chains from its own tier's first hop.
The read-only roles come first so that, when the table has to be read through
generated profiles, the least-privileged role is tried first.

##### What each setting controls

| Setting | Where | What it does |
|---|---|---|
| `roles` | `[sync]` or per org | The second-hop role names, one profile per role per account. The order sets the order in the file and the order in which roles are tried when the table is read through generated profiles (see "No table access yet (bootstrap)" above). Empty means `BG_DEFAULT_ROLES`, else the roles in `role_source_profiles`. |
| `role_suffixes` | `[sync]` or per org | Role → profile-name suffix. `""` means no suffix (only one role can have it). Otherwise lowercase `-x`, `-x-y`, and so on. Suffixes must be unique. A role with no entry gets `-<role name>`, lowercased and made safe for a profile name. |
| `source_profile` | per org (or `[sync]` when there are no `[orgs.*]`) | The default first hop, used for every role not in `role_source_profiles`. Optional when `role_source_profiles` lists every role. Setting either key switches the org to assume-role mode. |
| `role_source_profiles` | per org (or `[sync]` when there are no `[orgs.*]`); never inherited | Role → first-hop profile. This is how each second-hop role gets its matching first hop. |

An org's own `roles` or `role_suffixes` replaces the `[sync]` value as a
whole; the two are not merged.

##### Customizing

- **Add a tier:** create the permission set and target role in AWS (see
  [IAM permissions](#iam-permissions)), add a hand-written SSO profile for the
  first hop, then add the role to `roles`, `role_suffixes` and
  `role_source_profiles`. Do a dry run, then `--execute`.
- **Rename a role:** change the name in `roles`, `role_suffixes` and
  `role_source_profiles`, and in AWS update the first hop's inline policy and
  the trust policy. A `role_suffixes` or `role_source_profiles` key that names
  no role is ignored when profiles are built, so a missed key quietly falls
  back to `-<role name>` or to `source_profile` (or, with no `source_profile`,
  stops the run). Check the dry-run diff. Every
  `role_source_profiles` value is still checked, though: if a stale key points
  at a profile you've deleted, the run stops. Remove stale keys.
- **Change a suffix:** edit `role_suffixes` and rerun. Profiles with the old
  names are removed from the managed block. Update any scripts or
  `AWS_PROFILE` settings that used them.
- **Drop a tier:** remove the role from `roles`. Its profiles go away on the
  next `--execute`.
- **Different tiers per org:** set `roles`/`role_suffixes` under `[orgs.NAME]`.
- **Keep first-hop names clear of generated names.** Give hand-written
  first hops a stem that can't be an account short name, like `acme-sso-`. If
  one has the same name as a generated profile (say a first hop
  `acme-billing-ro` and an account with short name `billing`), the account's
  ID goes into the base name of every profile for that account:
  `acme-billing-111111111111`, `acme-billing-111111111111-ro`, and so on.

##### AWS side, per tier

- **First hop:** the permission set must allow assuming its target role.
  `ReadOnlyAccess` doesn't include `sts:AssumeRole`, so add an inline policy.
  Admin sets already allow it, but a scoped statement makes the link clear:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["sts:AssumeRole", "sts:TagSession"],
      "Resource": "arn:aws:iam::*:role/Read-Only-Plus"
    }]
  }
  ```

- **Second hop:** the target role's trust policy must trust the first hop's
  SSO role in the source account. The role ARN includes the SSO path and a
  random suffix, so match it with a condition instead of listing it as the
  principal:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::111111111111:root" },
      "Action": ["sts:AssumeRole", "sts:TagSession"],
      "Condition": {
        "ArnLike": {
          "aws:PrincipalArn": "arn:aws:iam::111111111111:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_ReadOnlyPlus_*"
        }
      }
    }]
  }
  ```

  `111111111111` is the account that holds the first-hop permission set. The
  wildcard after `sso.amazonaws.com/` covers both SSO role paths, with or
  without a region segment. Repeat both policies for each tier, with its own
  role and permission-set names.

If `aws sts get-caller-identity --profile acme-prd-rop` returns
`AccessDenied` on `AssumeRole`, check that the error names the target role. If
it does, one of these two policies is missing that role or that first hop.

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

How profiles are named depends on the org:
- **SSO orgs:** each active account gets `<name>` (`readonly_role`) and
  `<name>-adm` (`admin_role`).
- **Assume-role orgs (`source_profile` or `role_source_profiles` set):** each account gets one profile per
  entry in `roles`, named `<prefix><short name><suffix>`. See
  [Role conventions](#role-conventions-and-how-to-customize-them).

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
aws-sts-decode "$ENCODED" 123456789012        # with that account's SSO profile (falls back to -adm)
aws-sts-decode "$ENCODED" --profile acme-prd-adm   # with a specific (e.g. role-chained) profile
pbpaste | aws-sts-decode 123456789012         # message on stdin
```

## Migrating from aws-shell.functions

The installer retires `~/.functions.d/aws-shell.functions` (backup in
`~/.functions.d/.backup/`). Its functions map as follows:

| Old | Now |
|---|---|
| `aws-assume-role` | `aws-assume-role`: same arguments. It also unsets `AWS_PROFILE` (which otherwise overrides the new creds) and doesn't need jq |
| `aws-sso-refresh` | `aws-sso-refresh` command. Same `--force`. Adds the DESIGN.md split: the main org's login opens the browser, and the others print a URL to open by hand (`--main-org`, `--main-profile`) |
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
| `aws-sync-profiles` | Each org's accounts → SSO profiles (`<name>`, `<name>-adm`), or role-chained profiles per role in assume-role orgs; one block per org | Dry-run unless `--execute` |
| `ns-cert-refresh` | Netskope tenant CA + system CAs → bundle | Dry-run unless `--execute` |
| `aws-toolbox init` | Writes the starter `~/.aws/toolbox.toml` | Yes (`--force` to overwrite) |

Command options:

| Command | Options |
|---|---|
| `aws-sso-refresh` | `--force` (log in even if the token is valid), `--main-org ORG`, `--main-profile PROFILE`, `--list` (show targets and token status) |
| `aws-assume-role` | `--profile` (source credentials), `--session-name`, `--duration SECONDS`, `--format sh\|fish\|json` |
| `aws-sts-decode` | `--profile` (decode with this profile), `--raw` (don't reformat the JSON) |
| `aws-sync-profiles` | `--execute`/`--apply`, `--org ORG` (repeatable), `--profile` (override `management_profile`; one org), `--force` (rebuild with account names when the table is unreadable, or write an empty block). Table overrides, one org at a time: `--ddb-table-arn`, `--ddb-profile`, `--ddb-role-name`, `--attr-account-id`, `--attr-short-name`, `--attr-ou`, `--attr-environment`, `--roles A,B`, `--include-inactive` |
| `ns-cert-refresh` | `--execute`/`--apply`, `--output PATH` (override `bundle_path`) |
| `aws-toolbox init` | `--force` |

`aws-sts-decode ACCOUNT` looks the account up among SSO profiles
(`sso_account_id`) only. For a role-chained profile, pass `--profile` instead.

Every command takes `-v`/`--verbose`, `-q`/`--quiet` and `--help`, placed after
the command name. Each one also works as `aws-toolbox <command>`. Logs go to
stderr; stdout carries only results (diffs, JSON, shell exports).

## Platform notes

- **macOS**: `truststore` uses Security.framework. The Netskope certs are read from `/Library/Application Support/Netskope/STAgent/data`.
- **Linux**: `truststore` uses the system CA bundle, so install `ca-certificates`. The Netskope certs are read from `/opt/netskope/stagent/data`.
- **Windows**: not supported. DESIGN.md targets macOS and Linux only.

## IAM permissions

- **sync-profiles, SSO orgs:** `organizations:ListAccounts`, in the management
  account or a delegated administrator account.
- **sync-profiles, assume-role orgs:**
  - for `management_profile`: `organizations:ListRoots`,
    `organizations:ListOrganizationalUnitsForParent`,
    `organizations:ListAccountsForParent`, `organizations:DescribeOrganization`
    and `sts:GetCallerIdentity`;
  - for the credentials that read the table: `dynamodb:Scan` on the table;
  - when the table is read through `ddb_role_name`: `sts:AssumeRole` on that
    role in the table's account.
- **sso-refresh:** `sts:GetCallerIdentity`, to tell whether a login is still
  usable.
- assume-role: `sts:AssumeRole` on the target role, which must also trust the caller
- generated assume-role profiles: each first-hop permission set needs `sts:AssumeRole` on its target role, and each target role must trust that first hop; see [AWS side, per tier](#aws-side-per-tier)
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
