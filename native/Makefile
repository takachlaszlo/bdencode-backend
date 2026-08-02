PREFIX ?= /usr/local
DESTDIR ?=
CC ?= cc
PKG_CONFIG ?= pkg-config

TARGET := bdencode-libbluray-scan
SOURCE := libbluray_scan.c

CPPFLAGS += $(shell $(PKG_CONFIG) --cflags libbluray)
CFLAGS ?= -O2 -g
CFLAGS += $(REPRO_CFLAGS) -std=c11 -Wall -Wextra -Wpedantic -Wformat=2 -Wshadow \
	-Wconversion -Wstrict-prototypes -fstack-protector-strong
LDFLAGS += -Wl,-z,relro,-z,now
LDLIBS += $(shell $(PKG_CONFIG) --libs libbluray)

.PHONY: all clean install

all: $(TARGET)

$(TARGET): $(SOURCE)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

install: $(TARGET)
	install -d "$(DESTDIR)$(PREFIX)/bin"
	install -m 0755 $(TARGET) "$(DESTDIR)$(PREFIX)/bin/$(TARGET)"

clean:
	rm -f $(TARGET)
