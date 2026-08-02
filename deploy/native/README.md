# Native libbluray scanner

`bdencode-libbluray-scan` is a headless, read-only metadata helper. It opens a
Blu-ray directory with libbluray, enumerates the physical five-digit MPLS files,
and writes one UTF-8 JSON document to standard output. Diagnostics and unreadable
playlist warnings are written only to standard error.

Build dependencies on Debian 12:

```sh
sudo apt-get install --no-install-recommends build-essential pkg-config libbluray-dev
```

Build and install:

```sh
make -C native
sudo make -C native install PREFIX=/usr/local
```

Usage:

```sh
bdencode-libbluray-scan --json /path/to/disc-root
```

The path may point either to the disc root or directly to its `BDMV` directory.
The helper never opens source files for writing. Playlist duration, chapters,
clip in/out and relative times use libbluray's 90 kHz timeline and are exported
both as ticks and seconds. `selected_angle` and each segment's `angle` are
one-based JSON values even though libbluray's C API uses a zero-based angle
argument.

The public libbluray API does not expose MPLS connection-condition flags or a
separate CLPI-language field. Consequently the helper does not invent a
`seamless` value and labels the language from `BLURAY_STREAM_INFO` only as
`mpls_language`. FFprobe remains responsible for decoded stream properties such
as exact channel layout, bit depth, HDR10 mastering data, and Dolby Vision side
data.
