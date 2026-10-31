# Changelog

## 0.2.1

- No "almost full" for disks mounted under /run/media or /media (USB sticks, disk images being tested): they are not the machine's own.

## 0.2.0

- Every field on its setup form says in plain words what it is, and links to where to get it (addon.yaml `help:`, shown by Hisho's dashboard).
- Installs on a fresh machine: without a site_url its install stopped on its last line; now its address comes from this machine's tailnet name, Hisho keeps it in the settings (HISHO_SET site_url), and before Tailscale it runs on this machine only.
- site_url is optional (filled in by the install).

## 0.1.2

- Publishes on the tailnet without sudo when the owner is Tailscale's operator (a fresh Souko).

## 0.1.1

- README says what you need (addon.yaml you_need) and how to use it; the README check requires both.
- Neutral sample names in tests and comments.

## 0.1.0

- Moved out of Hisho into its own repository (hisho-addon-prometheus); installed from the add-on store.

