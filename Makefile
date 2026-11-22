# make release: the migratetool wheel and its sha256 (C15, release.sh)
.PHONY: release test parity
release:
	./release.sh
test:
	bash tests/unit/run.sh
parity:
	bash tests/unit/run.sh --parity
