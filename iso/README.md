# Building the install ISO

The installer is a bootc-image-builder (`bib`) **anaconda-iso** whose kickstart
wipes the disk, creates user `stream`, and writes the site configuration. All
site values and secrets live in the gitignored `site/` directory and are only
used at ISO build time. **Nothing under `site/` and no rendered
`iso/config.toml` is ever committed** (both are in `.gitignore`).

> **The build needs rootful, privileged podman. It is run by you, on your own
> machine, by hand. Never run it from automation or CI.**

## 1. Fill in the site values

```bash
cp -r site.example site          # from the repo root
$EDITOR site/site.toml           # Facebook IDs, SSN session id, cameras, mic
$EDITOR site/install.env         # password hash, SSH pubkey, Tailscale key
```

- `site/site.toml` becomes `~stream/.config/stream-pilot/site.toml` (mode 0600).
  Camera and mic IDs can only be read from the *installed* system
  (`ls -l /dev/v4l/by-id/ /dev/v4l/by-path/`, `pactl list short sources`), so
  expect to fill them in after the first boot; see the comments in the file for
  how to apply a change (`systemctl --user stop obs && stream-pilot seed-obs
  --reset-scenes && systemctl --user start obs`).
- Password hash for `PASSWORD_HASH=`: `openssl passwd -6` (or
  `mkpasswd -m sha-512`).
- `SSH_PUBKEY=` is the one-line public key allowed to log in as `stream`.
- `TAILSCALE_AUTHKEY=` is a Tailscale auth key (single-use, pre-approved,
  short expiry recommended). It is consumed on first boot and then deleted.

## 2. Render `iso/config.toml`

Run from the repo root. It substitutes the `@PASSWORD_HASH@`, `@SSH_PUBKEY@`,
`@TAILSCALE_AUTHKEY@` and `@SITE_TOML@` placeholders in
`iso/config.toml.example` in a single pass (so a value that happens to contain
`@...@` is never re-expanded), refuses values that would break the kickstart or
the TOML, and writes `iso/config.toml` with mode 0600. `sed` is deliberately
avoided: `site.toml` is multi-line and the hash/key values contain `/`, `$`
and `&`.

```bash
python3 - <<'PY'
import os, re, sys, tomllib
from pathlib import Path

site = Path("site/site.toml").read_text(encoding="utf-8")
tomllib.loads(site)  # must parse

env = {}
for n, line in enumerate(Path("site/install.env").read_text(encoding="utf-8").splitlines(), 1):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    key, sep, val = line.partition("=")
    if not sep:
        sys.exit(f"site/install.env:{n}: expected KEY=VALUE")
    env[key.strip()] = val.strip()

vals = {k: env.get(k, "") for k in ("PASSWORD_HASH", "SSH_PUBKEY", "TAILSCALE_AUTHKEY")}
vals["SITE_TOML"] = site.rstrip("\n")

problems = [f"{k} is empty" for k in ("PASSWORD_HASH", "SSH_PUBKEY", "TAILSCALE_AUTHKEY") if not vals[k]]
if not vals["PASSWORD_HASH"].startswith("$6$"):
    problems.append("PASSWORD_HASH is not a SHA-512 crypt hash ($6$...)")
for k, v in vals.items():
    if "'''" in v:
        problems.append(f"{k} contains ''' (would end the TOML literal string)")
    if k != "SITE_TOML" and "\n" in v:
        problems.append(f"{k} must be a single line")
    if k == "SSH_PUBKEY" and '"' in v:
        problems.append("SSH_PUBKEY must not contain a double quote")
for marker in ("STREAM_PILOT_SITE_TOML_EOF", "\n%end"):
    if marker in "\n" + vals["SITE_TOML"]:
        problems.append(f"site.toml contains kickstart marker {marker!r}")
if problems:
    sys.exit("refusing to render:\n  " + "\n  ".join(problems))

for line in site.splitlines():  # warn only: the install still works, scenes won't
    m = re.match(r'\s*(\w+)\s*=\s*""\s*(#.*)?$', line)
    if m:
        print(f"warning: site.toml: {m.group(1)} is empty", file=sys.stderr)

tpl = Path("iso/config.toml.example").read_text(encoding="utf-8")
out = re.sub(r"@(PASSWORD_HASH|SSH_PUBKEY|TAILSCALE_AUTHKEY|SITE_TOML)@",
             lambda m: vals[m.group(1)], tpl)
tomllib.loads(out)  # rendered config must still be valid TOML

dest = Path("iso/config.toml")
fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(out)
os.chmod(dest, 0o600)
print(f"wrote {dest} (mode 600)")
PY
```

## 3. Build the ISO (run by you, as root)

```bash
mkdir -p output
sudo podman run --rm -it --privileged --pull=newer \
  --security-opt label=type:unconfined_t \
  -v ./iso/config.toml:/config.toml:ro \
  -v ./output:/output \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  quay.io/centos-bootc/bootc-image-builder:latest \
  --type anaconda-iso --rootfs btrfs \
  ghcr.io/chasekidder/obs-blue:stable
```

The ISO lands in `output/bootiso/install.iso` (gitignored). The image must be
in root's container storage (the `-v /var/lib/containers/storage` mount); `sudo
podman pull ghcr.io/chasekidder/obs-blue:stable` first if it is not.

Booting it **erases the first disk with no prompt** (`clearpart --all`), then
reboots into the installed system, which logs into Plasma automatically (the
autologin config is part of the image).

## What the kickstart does, and which rules it follows

Source of the rules: the bootc-image-builder README, section "Anaconda ISO
(installer) options" (<https://github.com/osbuild/bootc-image-builder#anaconda-iso-installer-options-installer-mapping>;
the project is being merged into `osbuild/image-builder`, see
<https://osbuild.org/docs/bootc/>, but the kickstart behaviour is documented
in the last bib README revision). Followed:

- Kickstart goes in `[customizations.installer.kickstart] contents`; it
  **replaces** `[customizations.user]`, so the user is created with the
  kickstart `user`/`sshkey` commands, not a `[customizations.user]` block.
- bib adds `ostreecontainer ...` itself, so it is **not** in our kickstart.
- bib adds **no other** commands, so we supply `text --non-interactive`,
  `reboot`, `lang`, `keyboard`, `timezone`, `network`, disk layout, `rootpw`,
  and the user ourselves. Edit `timezone` if the PC is not on US Eastern time.
- `network --hostname=stream-pc` sets a static hostname. Without it the PC is
  `fedora` early in boot and `stream-pc.lan` once DHCP answers, and Chromium
  refuses a profile whose lock names the other hostname ("in use by another
  Chromium process on another computer"), so CDP never comes up.
- `xconfig --startxonboot` is required: in a text install anaconda otherwise
  links `/etc/systemd/system/default.target` to `multi-user.target` ("no
  default systemd target set & in text/remote desktop mode" in
  `/var/log/anaconda/anaconda.log`), which overrides the image's
  `graphical.target`, so Plasma Login Manager never starts and the greenboot
  check fails on every boot.
- `%post` is a normal kickstart `%post ... %end` block inside `contents`. It
  runs chrooted into the installed system, so paths are the installed
  system's (`/var/home/stream`, since `/home` is a symlink to `/var/home` on
  bootc/ostree systems) and `systemctl enable` acts on the installed system.
  `--erroronfail` makes a failed `%post` abort the install rather than leave a
  half-configured machine.
- The `contents` string is a TOML literal string (`'''`), not `"""`, so no
  character in the kickstart or the substituted values needs escaping. The
  heredocs inside `%post` use quoted delimiters so `$` and backticks in the
  TOML are not expanded.

Design notes:

- **Disk:** `zerombr`, `clearpart --all --initlabel`, `autopart`. The brief
  asked for plain `autopart`; the btrfs root comes from passing `--rootfs btrfs`
  to bib. Whether anaconda's `autopart` honours that for the *anaconda-iso*
  type is unverified; check with `findmnt /` / `lsblk -f` in a VM install
  before relying on it. Use `autopart --type=plain` or `--fstype=` in the
  kickstart if you need to force a layout.
- **Tailscale:** `tailscaled.service` and `sshd.service` are *disabled* in the
  image and nothing is running during `%post`, so `%post` enables both, writes
  the auth key to `/etc/stream-pilot/tailscale-authkey` (root, 0600), and
  installs `stream-pilot-tailscale-up.service`, a oneshot that runs
  `tailscale up --auth-key=file:... --hostname=stream-pc --ssh=false` after
  `network-online.target`/`tailscaled.service`, retries every 30 s (up to 10
  times) on failure, and deletes the key on success. `file:` for `--auth-key`
  is supported by the Tailscale CLI in the image (checked with
  `tailscale up --help`).
- **site.toml** is written with `umask 077` into a 0700 `stream-pilot` dir and
  chowned to `stream:stream` (mode 0600), because it holds the SSN session id.
- A best-effort `restorecon` labels the files written in `%post`.
