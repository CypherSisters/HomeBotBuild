# Security Policy

PuffPAI (HomeBot) is the free early version you install yourself, running in
real households. This policy describes what we support, how to report a
problem, and what we promise you can go verify yourself rather than take on
faith.

## Supported versions

Only the current build on the `main` branch is supported. There are no
released version tags yet; run against the latest commit on `main` and
report issues against it.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository
(the repo's **Security** tab → **Report a vulnerability**) rather than
opening a public issue. If you don't see the option, use the fallback
below.

There is no security email address. If private reporting is not available
on this repository yet, open a public issue titled **"security contact
request"** containing nothing but that request, no details of the
vulnerability, no reproduction, no affected file. We will reply on the issue
with a private channel to send the details to.

## Scope

In scope:
- The **Vault** (Zone 1): PIN/recovery-code handling, key derivation, and
  what's stored encrypted at rest (`src/puffpai/zone_vault.py`,
  `src/puffpai/security/vault.py`).

  Cipher and KDF, stated exactly, as a vault this build creates
  (`Vault.initialize`, `vault.py:1039-1065`) works:

  * Content is encrypted with **AES-256-GCM** (`cryptography`'s `AESGCM`).
  * The data key (DEK) is **32 random bytes** from `secrets.token_bytes`: it
    is *not* derived from the PIN, so the PIN's entropy is not the encryption
    key's entropy.
  * That DEK is wrapped under a key-encryption key derived from the human PIN
    with **scrypt** (RFC 7914, memory-hard): `N=2**14, r=8, p=1`, 32-byte
    output, 32-byte random salt (`_derive_kek`, `vault.py:644-670`;
    `_SCRYPT_N/R/P`, `vault.py:319-321`). The recovery code is a **second wrap
    of the same DEK**, derived the same way. The wrap records which KDF made
    it, so a wrap written under PBKDF2 by an older build still opens
    (`kdf: "pbkdf2"`, 480,000 iterations); new wraps are always scrypt.
  * Unwrapping *is* the credential check: GCM authentication fails on a wrong
    PIN, so no PIN verifier is stored alongside the wrap.
  * **PBKDF2-HMAC-SHA256, 480,000 iterations** (`_KDF_ITERATIONS`,
    `vault.py:237`) is still used in two places, and neither is the KEK of a
    vault this build creates: the PIN *verification hash* (`_hash_pin`), and
    the legacy `_derive_key` path that a vault predating the wrapped-key model
    falls back to when it has no keyring file.

  The file header also names "LUKS + TPM2.0 full-disk encryption" as a
  *production target*: that is an aspiration for the appliance, not something
  this build configures for you.
- **Egress doors**: the enumerated outbound-connection registry, its
  enforcement, and anything that lets the box reach a host that isn't a
  named, switched-on door (`src/puffpai/security/egress_doors.py`,
  `src/puffpai/offline_guard.py`).
- **Pairing gate**: device-approval flow and anything that lets an unpaired
  device act on the system (`src/puffpai/api_server.py` pairing handling).
- **Consent gates**: consent required before state-changing actions
  (`src/puffpai/security/consent_gate.py`, `src/puffpai/security/consent_requests.py`).
- **Home Assistant control gates**: the untargeted/broadcast/lights gates on
  HA-action requests.
- Anything that would let one profile/household read another's data, or let
  an unpaired party act on the system.

Out of scope: denial-of-service against your own local machine, and issues
that require an attacker to already have full local access to the box the
software runs on (local privilege on an already-compromised host is a
platform problem, not a PuffPAI one).

## Trust boundaries

Stated plainly so you can judge the design rather than infer it.

- **The trust boundary is "can reach the backend on loopback, with the API
  key".** Inside that boundary there is no further authentication of the
  caller. Any local process that can open a socket to the backend can assert
  `X-Puff-Device-Id` and be believed, because the backend believes that header
  from a trusted peer (loopback by default), and that is how the dashboard proxy
  and the on-box voice service identify themselves. What such a process cannot
  do is *land in a particular household member's session*: the id is an
  HMAC-SHA256 of the device id under the pairing secret, truncated to 128 bits,
  so hitting a real vault-chat bucket means guessing 128 bits. An unrecognised
  id simply gets its own empty bucket.
- **No host packet filter ships with this software.** We do not install or
  manage nftables/iptables rules, and you should not assume one is present.
  Every gate described here is enforced in application code. If you want the
  backend port unreachable from the LAN, that is your firewall's job, not
  ours.
- **`PUFFPAI_TRUSTED_PROXY_IPS` must stay unset** (or loopback-only) unless you
  genuinely run the dashboard on a different host from the backend. Every
  address named in it may assert any device identity. There is deliberately no
  wildcard, and a garbage value trusts nobody rather than everybody.
- **`api_server.trusted_proxy_hops` must stay `0`** unless a real reverse proxy
  you control sits in front of the backend. Above `0` the backend starts
  believing `X-Forwarded-For`, which is a client-settable header: that would
  let a LAN caller choose the address its rate limiting and per-peer sessions
  are keyed on, and, because it would let that caller present itself as
  loopback, cross the trust boundary above and assert device identities.
- **A caller the proxy cannot identify gets nothing, not someone else's
  session.** A legacy `v1` pairing cookie, a scoped device token, or an
  unpaired browser at the `knowledge` enforcement stage all reach the backend
  with no device identity. Those requests are marked `X-Puff-Anonymous` by the
  proxy, keyed to a single-use session id, and refused vault answers with a
  "pair this device" message. Sharing one bucket keyed on the proxy's own
  loopback address would let one member's 300-second vault unlock be
  inherited by any other unidentified caller.

## Response expectations

This is a small team building in the open. We aim to acknowledge a report
within a reasonable time and will say so plainly if a fix will take longer
than that, since we are not promising an SLA we can't back.

## Standing security contracts (test these yourself)

These are promises about the software's behavior, stated so you can check
them rather than trust them:

- **Zero telemetry.** Nothing phones home. Run the app with a local network
  monitor or a deny-by-default firewall and confirm no outbound traffic
  occurs beyond what you explicitly configured.
- **Enumerated egress.** The set of hosts the running box may reach is
  finite and named door-by-door in `docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md`,
  generated from the registry, not "the internet." Compare what the
  process actually connects to against that list.
- **Ledger.** Door decisions are recorded so you can audit what left the
  house and when, rather than trust that nothing did.

If any of the above fails to hold on your machine, that's a security bug,
please report it through the channel above.
