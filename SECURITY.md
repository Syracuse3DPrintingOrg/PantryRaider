# Security Policy

## Supported Versions

Pantry Raider is pre-1.0 software. Only the latest released version receives
security fixes. There are no backported patches for older tags. Please update to
the most recent version before reporting an issue.

## Reporting a Vulnerability

Please do not open a public GitHub issue for security vulnerabilities.

Report privately by email to info@syracuse3dprinting.com. You can expect an
acknowledgement within a reasonable time, and we will keep you informed as the
report is triaged and a fix is prepared. Coordinated disclosure is appreciated:
please give us a chance to ship a fix before any public write-up.

When reporting, include as much of the following as you can:

- The app version (`APP_VERSION` in `service/app/config.py`, or the value shown
  in the app's About page).
- The deployment mode (server, Pi Hosted, or Pi Remote) and which optional
  profiles are enabled (Grocy, Mealie, Ollama).
- Clear reproduction steps, including any request payloads or configuration
  needed to trigger the issue.
- The impact you observed, and any logs or screenshots that help.

## Verifying a Release

Release tags from v0.18.71 on, and the commit on `main`, are signed with the
Pantry Raider release key. Its fingerprint is:

```
SHA256:cS04LyfO51r/AaqHOJhB0GErj6IR4eT8XOJnTvyy1h8
```

The public key ships in this repository as `scripts/release-signers`. To check
a tag from a clone (Git 2.34 or newer):

```bash
git fetch --tags
git -c gpg.ssh.allowedSignersFile=scripts/release-signers verify-tag v0.20.0
```

A good tag prints `Good "git" signature for pantryraider-release` followed by
the fingerprint above. Compare the fingerprint with this page rather than
trusting the file alone. Anything else, including "no signature found", means
the tag did not come from us. To check the key file itself:

```bash
ssh-keygen -lf <(cut -d' ' -f3- scripts/release-signers)
```

The published container image is built only from a tag or commit that passes
this same check.

## Why Security Reports Matter Here

Pantry Raider is built to run on hardware you control, but a couple of features
widen its attack surface and are worth flagging when you assess risk:

- The appliance image runs a host root bridge bound to `127.0.0.1:9299` that can
  perform privileged host operations such as updates and restores. It is
  intended to be reachable only from localhost on the device.
- The app supports public exposure through Pangolin with TOTP-protected access,
  so installations can legitimately be reachable from the internet.

Because of this, we take vulnerability reports seriously, especially anything
touching the host bridge, authentication and session handling, the setup wizard,
or paths that could lead to remote code execution, privilege escalation, or
restore and zip-slip style file writes.
