# ivert setup — First-time setup

The `ivert setup` command prepares a machine for running IVERT. Run it once after installing.

```bash
ivert setup
```

It does two things:

1. **Creates IVERT's local data directories** under `~/.ivert` (the granule cache, the ICESat-2 database directory, and other working directories). Existing directories are left untouched.
2. **Checks for NASA Earthdata Login credentials** in your `~/.netrc` file and offers to save them if they are not already present.

---

## Data directories

IVERT stores downloaded ICESat-2 photons, cached datum grids, and other working files under `~/.ivert` by default. `ivert setup` creates these directories up front so they exist before the first download or validation. For each directory it reports whether it was `created` or already `exists`.

The directory locations are configurable via [`ivert options`](options.md); `ivert setup` reads the current configuration, so it honors any custom paths you have set.

---

## NASA Earthdata credentials

Downloading ICESat-2 data from NSIDC requires a free [NASA Earthdata Login](https://urs.earthdata.nasa.gov/) account. IVERT (and its underlying tools) read these credentials from a `machine urs.earthdata.nasa.gov` entry in your `~/.netrc` file.

- **If credentials are already present**, `ivert setup` reports:

  ```
  NASA Earthdata credentials are already set in your .netrc file.
  ```

- **If they are not present**, `ivert setup` offers to save them. If you answer `yes`, it prompts for your Earthdata username and password (the password is not echoed) and appends a new entry to `~/.netrc`. Existing entries in the file are preserved, and the file's permissions are set to owner-only (`600`).

You can run `ivert setup` again at any time; it will skip the credential prompt once valid credentials are in place.

---

## Example

```bash
# Prepare a new machine for IVERT
ivert setup
```
