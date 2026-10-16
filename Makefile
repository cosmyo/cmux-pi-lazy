PYTHON ?= /usr/bin/python3
NODE ?= node

.PHONY: test check

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
	$(NODE) --test tests/test_lifecycle_reporter.mjs

check: test
	$(PYTHON) -m py_compile launcher.py tests/test_launcher.py tests/test_install.py
	$(NODE) --check extensions/lifecycle.mjs
	/bin/sh -n install.sh uninstall.sh bin/pi-lazy
