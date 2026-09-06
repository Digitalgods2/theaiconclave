# Landing site source

Edit `src/*.html` (one tag per line) and `src/styles.css`. Run:

```shell
python tools/build_landing.py
python tools/build_landing.py --check
```

The root HTML/CSS files are deployment outputs. Assets remain in `assets/`.
CI rejects source/output drift. No JavaScript build toolchain is required.

`olld/`, `misc/`, and `ai-conclave-makeover.zip` are historical reference material;
they are not inputs to the build. Update only `src/` for current site changes.
