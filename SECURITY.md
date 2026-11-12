# Security

Report a vulnerability in `kennet` or in the Metaphi AI services it calls to security@metaphi.ai. Do not open a public
issue.

Say what is affected, the version (`kennet --version`), and how to reproduce it. We reply within two business days
and tell you when a fix ships. Please give us 90 days, or until a fix ships, before disclosing.

We support the latest release only; `kennet update` installs it. Every release's binaries carry a build provenance
attestation (`gh attestation verify <file> --repo metaphi-labs/kennet-cli`), and its `checksums.txt` is signed
(`checksums.txt.sig`, checked with `ssh-keygen -Y verify`); `install.sh` and `kennet update` check the signature.
