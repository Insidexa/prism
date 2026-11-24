# User-level install of the Prism app and GNOME extension (no sudo).
# The driver and permissions are set up separately: sudo system/install.sh
PREFIX    ?= $(HOME)/.local
SHARE     := $(PREFIX)/share
EXT_UUID  := prism-omen@local
EXT_DIR   := $(SHARE)/gnome-shell/extensions/$(EXT_UUID)
UNIT_DIR  := $(HOME)/.config/systemd/user

.PHONY: test install uninstall run dsp

LADSPA_DIR := $(PREFIX)/lib/ladspa

dsp: dsp/prism_bass.so

dsp/prism_bass.so: dsp/prism_bass.c
	$(CC) -O2 -Wall -Wextra -fPIC -shared -o $@ $< -lm

test:
	python3 -m unittest discover -s tests -t . -v

run:
	python3 -m prism

install: dsp
	install -d $(LADSPA_DIR)
	install -m 0755 dsp/prism_bass.so $(LADSPA_DIR)/
	rm -rf $(SHARE)/prism
	install -d $(SHARE)/prism $(PREFIX)/bin $(SHARE)/applications $(SHARE)/icons/hicolor/scalable/apps
	cp -r prism data $(SHARE)/prism/
	find $(SHARE)/prism -name __pycache__ -prune -exec rm -rf {} +
	sed 's|@SHAREDIR@|$(SHARE)/prism|' data/prism.in > $(PREFIX)/bin/prism
	chmod +x $(PREFIX)/bin/prism
	install -m 0644 data/io.github.Insidexa.Prism.desktop $(SHARE)/applications/
	install -m 0644 data/icons/hicolor/scalable/apps/io.github.Insidexa.Prism.svg $(SHARE)/icons/hicolor/scalable/apps/
	rm -rf $(EXT_DIR)
	cp -r extension/$(EXT_UUID) $(EXT_DIR)
	install -d $(UNIT_DIR)
	sed 's|@BINDIR@|$(PREFIX)/bin|' data/prism-automation.service.in > $(UNIT_DIR)/prism-automation.service
	-systemctl --user daemon-reload
	-systemctl --user enable prism-automation.service
	-systemctl --user restart prism-automation.service
	-python3 -c 'import sys; sys.path.insert(0, "$(SHARE)/prism"); from prism.desktop import bind_omen_key; bind_omen_key("$(PREFIX)/bin/prism")'
	-update-desktop-database $(SHARE)/applications 2>/dev/null
	-gtk-update-icon-cache -q -t $(SHARE)/icons/hicolor 2>/dev/null
	@echo "Installed. Enable the top-bar tile with: gnome-extensions enable $(EXT_UUID)"
	@echo "(GNOME on Wayland only sees new extensions after you log out and back in.)"

uninstall:
	-gnome-extensions disable $(EXT_UUID) 2>/dev/null
	-systemctl --user disable --now prism-automation.service 2>/dev/null
	-python3 -c 'import sys; sys.path.insert(0, "$(SHARE)/prism"); from prism.desktop import unbind_omen_key; unbind_omen_key()'
	rm -f $(UNIT_DIR)/prism-automation.service
	rm -rf $(SHARE)/prism $(PREFIX)/bin/prism $(EXT_DIR) $(LADSPA_DIR)/prism_bass.so \
		$(SHARE)/applications/io.github.Insidexa.Prism.desktop \
		$(SHARE)/icons/hicolor/scalable/apps/io.github.Insidexa.Prism.svg
