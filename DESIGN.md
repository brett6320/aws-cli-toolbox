# Overview

This is a list of fuctions and configurations to streamline use of AWS CLI and boto3 on macOS and Linux systems.

Generate a config file in `~/.aws` as in input to this functionality.

Boto3 should use the system truststore (truststore module on macOS).

# Functions

- aws-sso-refresh - This reviews `~/.aws/config` for SSO profiles that name an account (`sso_account_id`). The "main-org" profile should be used first - when using this profile, open a browser to authenticate automatically. If the URL is different for subsequent profiles, don't auto-open the browser, just present the URL to the user for manual use (alternate browser profile/container is likely).

- aws-assume-role - This allows the user to assume a role, defaulting ot the current account accepting alternate account, combined with role name as args.

- aws-sts-decode - Decodes a denial message from STS, accepting the encoded message and optionally an account number

- aws-sync-profiles - This connects to Organizations, enumerates accounts, and builds profiles based on the list of accounts. The default account is read-only, and profiles with an -adm suffix should use the privileged role specified in config files.

- ns-cert-refresh - Locate the tenant CA certificates for the Netskope agent on the system and build a cert bundle for use by AWS CLI.
