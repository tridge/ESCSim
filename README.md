# ESCSim

ESCSim is a standalone, Renode-based simulator for real AM32 ESC firmware.
It is being extracted from AM32's in-tree Renode support so that end users do
not need an AM32 source checkout or embedded compiler toolchain.

The implementation is currently under construction. See [plan.md](plan.md)
for the staged migration and release plan.

## Target definitions

ESCSim obtains AM32 target definitions from:

```text
https://raw.githubusercontent.com/am32-firmware/AM32/refs/heads/main/Inc/targets.h
```

The source can be changed to another HTTPS URL or a local file. A validated
copy is cached by content hash and the selected source persists across runs.

From a source checkout:

```sh
python3 -m pip install -e '.[test]'
escsim targets status
escsim targets refresh
escsim targets list
pytest
```
