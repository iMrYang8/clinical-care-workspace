# Nightingale distribution notices

These files accompany the default backend image at
`/usr/share/doc/nightingale/`. `THIRD_PARTY_NOTICES.md` is the authoritative
adoption register; this directory preserves the complete texts specifically
required by redistributed or optional release components.

| Component | Locked/distributed version | Packaging state | License file | Validation state |
| --- | --- | --- | --- | --- |
| Nightingale and FastAPI template baseline | template `68adb40d`; Nightingale current commit | Source and backend image notices | `Nightingale-MIT.txt`, `ATTRIBUTION.txt` | Included in image build test |
| Tiptap | `3.30.3` | Production browser bundle | `Tiptap-MIT.txt` | Frontend build tested |
| Serene Comment Extension | `0.2.0` | Production browser bundle | `Serene-Comment-Extension-MIT.txt` | Adapter smoke/unit/build tested |
| idb | `8.0.3` | Production browser bundle | `idb-ISC.txt` | Encrypted queue and blocked-delete tests |
| CTranslate2 | `4.8.1` | Locked optional `local-asr`; omitted from default image | `CTranslate2-MIT.txt` | Dependency lock only; runtime/model smoke not tested |
| PyAV | `18.1.0` | Locked optional `local-asr`; omitted from default image | `PyAV-BSD-3-Clause.txt` | Dependency lock only; runtime/model smoke not tested. Binary-wheel FFmpeg terms must be reviewed for the release platform. |
| FFmpeg | Debian package, exact build varies by rebuilt image | Default backend runtime | image-local `ffmpeg-build.txt`; see `THIRD_PARTY_NOTICES.md` | Release generator records exact image/commit/digest; older evidence is not transferable |

The default Python and browser dependency versions are frozen by `uv.lock` and
`bun.lock`. Their package metadata and the component-level obligations in
`THIRD_PARTY_NOTICES.md` remain part of this notice set. Optional model weights
are not bundled; their licenses and immutable revisions must be added before a
release image can include them.
