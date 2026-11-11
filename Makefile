.PHONY: check fmt run serve hooks pwa wasm-test pwa-browser-test
# Cargo's target folder (CARGO_TARGET_DIR or ./target).
TARGET = $(shell cargo metadata --format-version 1 --no-deps | node -e "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>console.log(JSON.parse(s).target_directory.split(String.fromCharCode(92)).join('/')))")
WASM = $(TARGET)/wasm32-wasip1/release/jaguar_wasm.wasm

check:            ## fmt + lint + tests (run before every commit); the phone engine too when its target is installed
	cargo fmt --all --check
	cargo clippy --all-targets -q -- -D warnings
	@if rustup target list --installed 2>/dev/null | grep -q wasm32-wasip1; then \
	  cargo clippy -q -p jaguar-wasm --target wasm32-wasip1 -- -D warnings; \
	else echo "(skipped the phone engine: rustup target add wasm32-wasip1)"; fi
	cargo test -q
fmt:
	cargo fmt --all
run:
	cargo run --release -q -- run examples/canoe.toml
serve:
	cargo run --release -q -- serve examples/canoe.toml
hooks:            ## install the pre-commit hook
	git config core.hooksPath .githooks
pwa:              ## the phone app in target/pwa (needs rustup target add wasm32-wasip1, and Node)
	cargo build --release -q --target wasm32-wasip1 -p jaguar-wasm
	node web/pwa/build.mjs "$(WASM)" target/pwa
wasm-test: pwa    ## the phone engine against the native build (docs/SPEC.md "Phone app")
	cargo build --release -q
	node web/pwa/test.mjs "$(WASM)" "$(TARGET)/release/jaguar"
pwa-browser-test: pwa ## the built phone app in headless Chrome (needs Chrome or Edge)
	node web/pwa/browser-test.mjs target/pwa
