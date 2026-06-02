PYTHON := python3.12
VENV   := .venv
PIP    := $(VENV)/bin/pip
PYRUN  := $(VENV)/bin/python
SOCKET := /tmp/kokoro-tts.sock
PID    := /tmp/kokoro-tts.pid

.PHONY: setup generate play clean tts-start tts-stop tts-status

setup: $(VENV)/.installed

$(VENV)/.installed: requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PYRUN) -m unidic download
	$(PYRUN) -m spacy download en_core_web_sm
	touch $@

DAY ?= 01
generate: $(VENV)/.installed
	$(PYRUN) readme_to_mp3.py day-$(DAY).md output/day-$(DAY)/ --combined

play: output/day-$(DAY)/combined.mp3
	afplay $<

tts-start: $(VENV)/.installed
	@if [ -S $(SOCKET) ]; then echo "TTS server already running"; exit 0; fi
	$(PYRUN) tts_server.py &
	@echo "TTS server starting..."

tts-stop:
	@if [ -f $(PID) ]; then kill $$(cat $(PID)) 2>/dev/null; rm -f $(PID) $(SOCKET); echo "TTS server stopped"; else echo "No server running"; fi

tts-status:
	@if [ -S $(SOCKET) ]; then echo "TTS server running (PID $$(cat $(PID) 2>/dev/null || echo unknown))"; else echo "TTS server not running"; fi

clean:
	rm -rf $(VENV) output
