.PHONY: check fmt run serve hooks
check:            ## fmt + lint + tests (run before every commit)
	cargo fmt --check
	cargo clippy --all-targets -q -- -D warnings
	cargo test -q
fmt:
	cargo fmt
run:
	cargo run --release -q -- run examples/canoe.toml
serve:
	cargo run --release -q -- serve examples/canoe.toml
hooks:            ## install the pre-commit hook
	git config core.hooksPath .githooks
