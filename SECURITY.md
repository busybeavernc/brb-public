# BRB (Be Right Back) Security

We take the security of BRB and the people who use it seriously. If you think you've found a security problem in BRB, please tell us. We appreciate reports from users, researchers and IT teams.

## How to report a vulnerability

Email **support@happyhacks.io** with "Security" in the subject line. Please include:

- what you found and where (the BRB app and its version, or the page on brb.happyhacks.io)
- the steps to reproduce it, and what an attacker could do with it
- your name or handle, if you'd like credit

Please don't post details publicly until we've had a reasonable chance to fix the problem.

## What we'll do

- Acknowledge your report, normally within 3 business days.
- Keep you updated while we look into it, and tell you when it's fixed.
- Release fixes through BRB's built-in automatic updates, and credit you in the release notes if you'd like.

We don't currently run a paid bug bounty.

## In scope

- The BRB desktop app for Windows and Mac, including its installer and its virtual camera
- The website brb.happyhacks.io, including the license and account pages
- BRB's release files on [GitHub](https://github.com/busybeavernc/doppleganger-public/releases)

## Out of scope

- Denial-of-service attacks, load testing, spam or social engineering of our staff or users
- Problems in third-party services (such as Square or GitHub) that should be reported to them
- Reports from automated scanners with no demonstrated security impact

## Good-faith research

If you act in good faith, stay within the scope above, avoid accessing or changing other people's data, avoid harming our services, and give us a reasonable chance to fix the problem before sharing it, we won't pursue legal action against you for your research.

## Checking a download

Every BRB release is built automatically from its source code by GitHub Actions, and GitHub records a signed statement of exactly which build produced each file (a "build provenance attestation"). To check that a file you downloaded is a genuine, unmodified BRB release, run this with the [GitHub CLI](https://cli.github.com):

```
gh attestation verify BRBSetup.exe --repo busybeavernc/doppleganger
```

The SHA-256 checksum of each current download is also listed on our [page for IT teams](https://brb.happyhacks.io/it).

## Keeping BRB up to date

BRB checks for updates when it starts and installs them automatically (this can be turned off in Settings). Security fixes are released for the current version of BRB.
