# ivert cache

Manage the IVERT local file cache. The cache holds files that IVERT generates or downloads to speed up repeated operations:

- **Vertical datum shift grids** — downloaded on first use when converting between vertical datums
- **Harmony job records** — tracking active/recent ICESat-2 data requests
- **Temporary downloads** — intermediate files during data retrieval

The default cache location is `~/.ivert/cache`. You can change it with `ivert options cache_directory=/your/path` (see [ivert options](options.md)).

---

## ivert cache list

Show the number of files and total size of the cache, broken down by subdirectory.

```
ivert cache list
```

---

## ivert cache delete

Delete all files in the IVERT cache directory.

```
ivert cache delete
ivert cache delete --force
```

| Flag | Description |
|------|-------------|
| `-f, --force` | Skip the confirmation prompt |

The cache is safe to delete at any time. IVERT will re-download or recompute any needed files the next time they are required. The main cost is re-downloading datum shift grids, which may take a few minutes.

---

## Example

```bash
# See how much cache space is being used
ivert cache list

# Free up space
ivert cache delete
```
