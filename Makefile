# User-level install of the Prism app and GNOME extension (no sudo).
# The driver and permissions are set up separately: sudo system/install.sh
PREFIX    ?= $(HOME)/.local
SHARE     := $(PREFIX)/share
EXT_UUID  := prism-omen@local
EXT_DIR   := $(SHARE)/gnome-shell/extensions/$(EXT_UUID)

.PHONY: test install uninstall run

test:
	python3 -m unittest discover -s tests -t . -v

run:
	python3 -m prism

install:
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
	-update-desktop-database $(SHARE)/applications 2>/dev/null
	-gtk-update-icon-cache -q -t $(SHARE)/icons/hicolor 2>/dev/null
	@echo "Installed. Enable the top-bar tile with: gnome-extensions enable $(EXT_UUID)"
	@echo "(GNOME on Wayland only sees new extensions after you log out and back in.)"

uninstall:
	-gnome-extensions disable $(EXT_UUID) 2>/dev/null
	rm -rf $(SHARE)/prism $(PREFIX)/bin/prism $(EXT_DIR) \
		$(SHARE)/applications/io.github.Insidexa.Prism.desktop \
		$(SHARE)/icons/hicolor/scalable/apps/io.github.Insidexa.Prism.svg
