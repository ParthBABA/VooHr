# Changelog

All notable changes to this project are tracked here going forward.

## Unreleased

- Removed 8 unreferenced `*.png` originals left after the webp image migration
  (`access-bg`, `aes-bg`, `audit-bg`, `privacy-bg`, `product-1`, `product-2`,
  `security-bg`, `totp-background`), reclaiming ~11.7 MB. Their `.webp`
  equivalents remain referenced and unaffected.
- Initial change-tracking convention: no entries yet in this section.

## Convention going forward

- **Track changes via commit messages and PR descriptions**, not patch files.
- Ad-hoc `*.patch` / `*.diff` files are not committed to the repository (see `.gitignore`).
- Describe user-visible behavior and rationale in commit/PR descriptions so history is self-documenting.
- Keep meaningful product/behavior changes summarized under "Unreleased" (or a dated release section) as they land.
