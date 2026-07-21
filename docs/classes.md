# ivert classes

List the ICESat-2 photon classification codes and their meanings. These are the codes IVERT assigns to photons during classification and that you use when filtering photons — for example with the `--classes` option of [`ivert database download`](database.md#photon-class-options) and [`ivert database export`](database.md#filtering-options).

```
ivert classes
```

This command takes no options. It prints the current code table:

| Code | Description |
|------|-------------|
| `0` | Noise (if enabled) |
| `1` | Ground (ATL08) |
| `2` | Canopy (ATL08) |
| `3` | Top Canopy (ATL08) |
| `6` | Land Ice (ATL06) |
| `7` | Buildings (Dynamic Algo / Bing Mask) |
| `40` | Seafloor (ATL24 / Dynamic Algo) |
| `41` | Nearshore Water Surface (ATL24 / Dynamic Algo) |
| `42` | Inland Water Surface (ATL13 / Dynamic Algo) |
| `44` | Open Ocean Surface (ATL12 / Geoid Fallback) |
| `-1` | Unclassified |

The definitions are read from the [globato](https://github.com/continuous-dems/globato) ICESat-2 reader, so they always match the classifier IVERT uses. Run `ivert classes` to see the authoritative list for your installed version.

---

## Example

```bash
# List the photon class codes, then download only ground and seafloor photons
ivert classes
ivert database download -c 1/40 -- -74.0/-73.0/40.5/41.0
```
