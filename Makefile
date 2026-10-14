PYTHON ?= /usr/bin/python3

.PHONY: test check

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

check: test
	$(PYTHON) -m py_compile launcher.py tests/test_launcher.py tests/test_install.py
	/bin/sh -n install.sh uninstall.sh bin/pi-lazy
